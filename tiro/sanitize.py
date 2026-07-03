"""Content sanitization: HTML at ingestion time, markdown for AI output.

Sanitize HTML while it IS HTML (before markdown conversion) — running a
sanitizer over markdown syntax mangles legitimate formatting. For content
that arrives as markdown (Opus digests), only surgical patterns are removed.
"""

import re

import nh3

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del",
    "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p",
    "pre", "q", "s", "small", "span", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}

ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height", "loading"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

_MD_BLOCK_RE = re.compile(
    r"<(script|iframe|object|embed)\b[^>]*>.*?</\1\s*>|<(script|iframe|object|embed)\b[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)
_MD_JS_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*javascript:[^)]*\)", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """Sanitize raw HTML before markdown conversion. Keeps article structure
    and images; removes scripts, frames, event handlers, javascript: URLs."""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
    )


def sanitize_markdown(md: str) -> str:
    """Surgically remove dangerous raw-HTML islands and javascript: links
    from markdown without touching markdown syntax itself."""
    md = _MD_BLOCK_RE.sub("", md)
    md = _MD_JS_LINK_RE.sub(r"[\1](#)", md)
    return md
