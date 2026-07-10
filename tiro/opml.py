"""OPML 2.0 parse/build for feed subscriptions (Phase 4 M4.1).

Pure functions — bytes <-> a list of feed dicts `{url, title, site_url,
folder}`. No I/O, no DB. The `/api/feeds/import` + `/api/feeds/export` routes
in `routes_feeds.py` wrap these.

- `parse_opml` walks `<outline>` elements ITERATIVELY (a deeply nested folder
  tree must never blow the Python recursion limit). An outline carrying an
  `xmlUrl` is a feed; its `folder` is the "/"-joined `text`/`title` of every
  ancestor *folder* outline (an outline WITHOUT an xmlUrl is structure, not a
  feed). Unparseable input raises `ValueError` (the route turns that into 400).
- `build_opml` nests feeds ONE level under a `<outline text="{folder}">` per
  distinct folder string (a multi-segment "Tech/Startups" folder is written as
  a single wrapper — so a build->parse round-trip is stable, since parse joins
  the one ancestor back into the same "Tech/Startups"). Feeds without a folder
  sit at the top level. Every feed outline gets `type="rss"`, `text`, `title`,
  `xmlUrl`, and (when known) `htmlUrl`.
"""

import xml.etree.ElementTree as ET

# Guard against a pathological upload building an unbounded tree; the route
# also caps the raw upload size, this is a belt-and-suspenders node cap.
_MAX_OUTLINES = 100_000


def parse_opml(data: bytes) -> list[dict]:
    """Parse OPML bytes into a flat list of feed dicts.

    Returns `[{url, title, site_url, folder}]`. Raises `ValueError` on
    unparseable XML.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"Malformed OPML: {e}") from e

    body = root.find("body")
    if body is None:
        # Some exports put outlines directly under the root; be lenient.
        body = root

    feeds: list[dict] = []
    # Iterative DFS: stack of (outline_element, ancestor_folder_segments).
    stack: list[tuple[ET.Element, tuple[str, ...]]] = [
        (child, ()) for child in reversed(list(body)) if _localname(child.tag) == "outline"
    ]
    seen = 0
    while stack:
        el, ancestors = stack.pop()
        seen += 1
        if seen > _MAX_OUTLINES:
            break
        xml_url = el.get("xmlUrl") or el.get("xmlurl")
        label = (el.get("text") or el.get("title") or "").strip()
        if xml_url:
            feeds.append({
                "url": xml_url.strip(),
                "title": label or xml_url.strip(),
                "site_url": (el.get("htmlUrl") or el.get("htmlurl") or "").strip(),
                "folder": "/".join(ancestors) if ancestors else None,
            })
            # A feed outline may (rarely) also nest children; treat them as
            # under the same ancestor folders, not under the feed itself.
            child_folder = ancestors
        else:
            child_folder = (*ancestors, label) if label else ancestors
        for child in reversed(list(el)):
            if _localname(child.tag) == "outline":
                stack.append((child, child_folder))

    return feeds


def build_opml(feeds: list[dict], *, title: str = "Tiro subscriptions") -> str:
    """Build an OPML 2.0 document (as a string) from feed dicts.

    Feeds are grouped one level deep by their `folder` string; feeds without a
    folder are emitted at the top level.
    """
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(opml, "body")

    # Preserve first-seen folder order; None-folder feeds go straight on body.
    folder_nodes: dict[str, ET.Element] = {}
    for f in feeds:
        folder = f.get("folder")
        parent = body
        if folder:
            node = folder_nodes.get(folder)
            if node is None:
                node = ET.SubElement(body, "outline", {"text": folder, "title": folder})
                folder_nodes[folder] = node
            parent = node
        attrs = {
            "type": "rss",
            "text": f.get("title") or f.get("url") or "",
            "title": f.get("title") or f.get("url") or "",
            "xmlUrl": f.get("url") or "",
        }
        if f.get("site_url"):
            attrs["htmlUrl"] = f["site_url"]
        ET.SubElement(parent, "outline", attrs)

    return ET.tostring(opml, encoding="unicode", xml_declaration=True)


def _localname(tag) -> str:
    """Strip any XML namespace so `{ns}outline` matches `outline` (OPML is
    namespace-free in practice, but be defensive)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]
