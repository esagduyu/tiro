"""Web page ingestion — fetch, extract, convert to markdown."""

import logging
import re

import httpx
from lxml import etree
from lxml.html import fromstring, tostring
from markdownify import markdownify as md
from readability import Document

from tiro.sanitize import sanitize_html

logger = logging.getLogger(__name__)

# Tags used for layout tables — strip these so markdownify doesn't
# render them as markdown tables (common on old sites like paulgraham.com)
_LAYOUT_TAGS = {"table", "tbody", "thead", "tfoot", "tr", "td", "th"}

# Streamed byte cap for a single page fetch (Fold-in 1a, T2 fable review): the
# RSS pipeline re-fetches the full page for EVERY entry, so an unbounded body
# was a memory-exhaustion vector. Both fetchers stream and raise PageTooLarge
# the moment the accumulated body crosses this, mirroring `_fetch_feed`'s
# 10 MB feed cap. 5 MB comfortably covers real articles.
MAX_PAGE_BYTES = 5 * 1024 * 1024


class PageTooLarge(Exception):
    """A page body exceeded MAX_PAGE_BYTES mid-stream."""


def _decode_capped_sync(response) -> str:
    """Stream a sync httpx response under MAX_PAGE_BYTES, returning decoded text."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_PAGE_BYTES:
            raise PageTooLarge(f"page body exceeded {MAX_PAGE_BYTES} bytes")
        chunks.append(chunk)
    return _decode(b"".join(chunks), response)


async def _decode_capped_async(response) -> str:
    """Stream an async httpx response under MAX_PAGE_BYTES, returning decoded text."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_PAGE_BYTES:
            raise PageTooLarge(f"page body exceeded {MAX_PAGE_BYTES} bytes")
        chunks.append(chunk)
    return _decode(b"".join(chunks), response)


def _decode(body: bytes, response) -> str:
    """Decode fetched bytes using the response's declared charset, falling back
    to a lenient UTF-8 (readability/lxml tolerate imperfect input)."""
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _collect_content_images(html: str) -> list[dict]:
    """Extract content images with their surrounding text context.

    Walks the content container looking for <figure> elements (or divs wrapping
    them) and records the text of the element immediately *before* each image.
    This anchor text is later used to place images in the correct position after
    readability strips them.
    """
    try:
        tree = fromstring(html)
    except etree.ParserError:
        return []

    # Find the main article content container
    container = None
    for selector in [
        '//*[contains(@class, "body markup")]',       # Substack
        '//*[contains(@class, "post-content")]',      # WordPress / generic
        '//*[contains(@class, "article-content")]',   # generic
        '//*[contains(@class, "entry-content")]',     # WordPress
    ]:
        hits = tree.xpath(selector)
        if hits:
            container = hits[0]
            break
    if container is None:
        body = tree.find(".//body")
        container = body if body is not None else tree

    images = []
    prev_text = ""

    for child in container:
        # Skip non-element nodes (comments, processing instructions)
        if not isinstance(child.tag, str):
            continue
        tag = child.tag
        cls = child.get("class", "")

        # Check if this element contains a figure/image
        is_image_container = tag == "figure" or (
            tag == "div" and "image-container" in cls
        )
        if not is_image_container:
            # Also check for a nested figure
            fig = child.find(".//figure")
            if fig is not None:
                is_image_container = True
                child = fig  # use the figure for image extraction

        if is_image_container:
            img = child.find(".//img")
            if img is not None:
                src = img.get("src", "")
                if src:
                    alt = img.get("alt", "")
                    caption_el = child.find(".//figcaption")
                    caption = caption_el.text_content().strip() if caption_el is not None else ""
                    # Use last ~80 chars of preceding text as anchor
                    anchor = prev_text.strip()[-80:] if prev_text.strip() else ""
                    images.append({
                        "src": src, "alt": alt,
                        "caption": caption, "anchor": anchor,
                    })
        else:
            text = child.text_content() or ""
            if text.strip():
                prev_text = text

    return images


def _reinject_images(content_html: str, images: list[dict]) -> str:
    """Re-inject content images at their correct positions using text anchors.

    For each image, finds the paragraph in the readability output whose text
    ends with the anchor text recorded from the original HTML, and inserts the
    image immediately after that paragraph.
    """
    if not images:
        return content_html

    try:
        tree = fromstring(content_html)
    except etree.ParserError:
        return content_html

    # Check which images are already present
    existing_srcs = {img.get("src", "") for img in tree.iter("img")}
    missing = [img for img in images if img["src"] not in existing_srcs]

    if not missing:
        return content_html

    # Find the main content container
    body = tree.find(".//body")
    container = body if body is not None else tree

    # Build list of leaf-level block elements (p, blockquote, headings)
    # Exclude div since it often wraps everything and matches too broadly
    leaf_tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"}
    blocks = []
    for el in container.iter():
        if el.tag in leaf_tags:
            text = (el.text_content() or "").strip()
            if text:
                blocks.append((el, text))

    for img_data in missing:
        img_el = etree.Element("img")
        img_el.set("src", img_data["src"])
        if img_data["alt"]:
            img_el.set("alt", img_data["alt"])

        wrapper = etree.Element("p")
        wrapper.append(img_el)
        if img_data.get("caption"):
            etree.SubElement(wrapper, "br")
            em = etree.SubElement(wrapper, "em")
            em.text = img_data["caption"]

        anchor = img_data.get("anchor", "")
        inserted = False

        if anchor:
            # Find the most specific block element whose text ends with the anchor
            for el, text in blocks:
                if text.endswith(anchor) or anchor in text:
                    parent = el.getparent()
                    if parent is not None:
                        idx = list(parent).index(el) + 1
                        parent.insert(idx, wrapper)
                        inserted = True
                        break

        if not inserted:
            # Fallback: append at end of container
            container.append(wrapper)

    return tostring(tree, encoding="unicode")


def _strip_layout_tables(html: str) -> str:
    """Unwrap layout tables, keeping their inner content intact."""
    try:
        tree = fromstring(html)
    except etree.ParserError:
        return html

    for tag in _LAYOUT_TAGS:
        for el in tree.iter(tag):
            el.drop_tag()  # removes the tag but keeps children and text

    # Remove spacer/nav images (1x1 pixels, image maps, tiny icons)
    for img in list(tree.iter("img")):
        usemap = img.get("usemap")
        ismap = img.get("ismap")
        if usemap is not None or ismap is not None:
            img.drop_tree()
            continue
        src = img.get("src", "")
        # Remove 1x1 spacer gifs (by attribute or filename)
        w = img.get("width", "")
        h = img.get("height", "")
        if w == "1" or h == "1" or "trans_1x1" in src or "spacer" in src:
            img.drop_tree()

    # Remove image map definitions
    for m in tree.iter("map"):
        m.drop_tree()

    return tostring(tree, encoding="unicode")


def _extract_author(html: str) -> str | None:
    """Extract author name from HTML meta tags or JSON-LD."""
    # Try <meta name="author" content="...">
    m = re.search(r'<meta[^>]*name=["\']author["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        # Try reversed attribute order: content before name
        m = re.search(r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']author["\']', html, re.I)
    if m:
        return m.group(1).strip()

    # Try <meta property="article:author" content="...">
    m = re.search(r'<meta[^>]*property=["\']article:author["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1).strip()

    return None


def extract_from_html(html: str, final_url: str) -> dict:
    """Shared extraction+sanitize core for both the async and sync fetchers.

    Given raw page HTML and the final (post-redirect) URL, produce the clean
    markdown dict. This is the SINGLE place the sanitize invariant
    (`sanitize_html` before markdownify) lives for web pages — both
    `fetch_and_extract` (async) and `fetch_and_extract_sync` (sync, used by
    the RSS pipeline from its worker thread) call it, so the sanitize call
    chain is never duplicated (Phase 4 M4.0 refactor).

    Returns dict with keys: title, author, content_md, url
    """
    # Collect content images before readability strips them
    content_images = _collect_content_images(html)

    doc = Document(html)
    title = doc.title()
    content_html = doc.summary()

    # Re-inject any content images that readability removed
    content_html = _reinject_images(content_html, content_images)

    # Extract author from meta tags
    author = _extract_author(html)

    # Strip layout tables (common on old-school sites) before markdown conversion
    content_html = _strip_layout_tables(content_html)

    # Sanitize HTML (strip scripts, event handlers, javascript: URLs) — this
    # is the last point content is still HTML, right before markdownify, so
    # it covers both readability's output and the re-injected <figure> images.
    content_html = sanitize_html(content_html)

    # Convert to clean markdown — preserves links and code blocks by default
    content_md = md(
        content_html,
        heading_style="ATX",
        bullets="-",
        wrap=False,
    )

    # Collapse runs of 3+ blank lines into 2
    content_md = re.sub(r"\n{3,}", "\n\n", content_md).strip()

    return {
        "title": title,
        "author": author,
        "content_md": content_md,
        "url": final_url,
    }


async def fetch_and_extract(url: str) -> dict:
    """Fetch a web page and extract its main content as clean markdown.

    Returns dict with keys: title, author, content_md, url
    """
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": "Tiro/0.1 (reading assistant)"},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            html = await _decode_capped_async(response)
            # Use the final URL after redirects (e.g. substack.com/home/post/...
            # -> author.substack.com/p/...)
            final_url = str(response.url)

    return extract_from_html(html, final_url)


def fetch_and_extract_sync(url: str) -> dict:
    """Synchronous twin of `fetch_and_extract` (Phase 4 M4.0).

    The RSS pipeline (`tiro/ingestion/rss.py::check_feeds`) runs entirely on a
    worker thread (via `asyncio.to_thread`), so it needs a blocking fetch. This
    shares the exact extraction/sanitize core (`extract_from_html`) with the
    async path — only the HTTP client differs (sync `httpx.Client`). Same 30s
    timeout, redirect following, and User-Agent as the async twin.

    Returns dict with keys: title, author, content_md, url
    """
    with httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": "Tiro/0.1 (reading assistant)"},
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            html = _decode_capped_sync(response)
            final_url = str(response.url)

    return extract_from_html(html, final_url)
