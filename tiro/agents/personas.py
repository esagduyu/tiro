"""Personas (Phase 6 K3) -- untrusted by construction (spec §5, FROZEN).

A persona is a markdown file at {library}/personas/{slug}.md: YAML
frontmatter (name/version/scope/schedule/tier/output) + a prompt-template
body over a CLOSED placeholder set. Persona files are community-shareable
and therefore UNTRUSTED INPUT; so is the library content interpolated into
them. The boundary is structural, never prompt-level:
  * scope-derived read-only context (ScopedContext, Task 4),
  * ctx.suggest(...) as the ONLY write path,
  * no network tool on any context,
  * fenced interpolation + a fixed preamble the persona cannot alter,
  * outputs stored as pending suggestions, applied only on user accept.
Unknown placeholder or any schema violation = PersonaLoadError; there is
no partial render and no registration of a broken persona.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from tiro.config import TiroConfig
from tiro.intelligence.prompts import load_template

logger = logging.getLogger(__name__)

SCOPES = ("article", "day", "query", "library")
SCHEDULES = ("on-ingest", "cron", "manual")
TIERS = ("heavy", "light")
OUTPUTS = ("note", "digest_section", "wiki_page", "tier_suggestion")

# OPEN decision 2: the concrete scope tables. The placeholder SET is frozen
# (spec §5); this maps each scope to its inputs, placeholders, and reads.
SCOPE_INPUTS: dict[str, dict[str, type]] = {
    "article": {"article_id": int},
    "day": {},
    "query": {"query": str},
    "library": {"wiki_slug": str},
}
SCOPE_PLACEHOLDERS: dict[str, set[str]] = {
    "article": {"article", "highlights"},
    "day": {"day_articles", "highlights"},
    "query": {"query"},
    "library": {"wiki_page"},
}
SCOPE_READS: dict[str, set[str]] = {
    "article": {"get_article", "get_highlights", "similar_articles"},
    "day": {"get_highlights", "list_recent_articles"},
    "query": {"search"},
    "library": {"get_wiki_page"},
}
OUTPUT_SCOPES: dict[str, set[str]] = {
    "note": {"article"},
    "tier_suggestion": {"article"},
    "digest_section": {"day", "query"},
    "wiki_page": {"library"},
}
ALL_PLACEHOLDERS = {"article", "highlights", "query", "day_articles", "wiki_page"}

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*\}\}")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

DEFAULT_PERSONAS = ("devils-advocate", "daily-themes", "research-brief")


class PersonaLoadError(ValueError):
    """Any persona-file schema/placeholder violation. Load error means the
    persona is listed as broken and never registered -- no partial render."""


@dataclass
class Persona:
    slug: str
    name: str
    version: str
    scope: str
    schedule: str
    tier: str
    output: str
    body: str
    path: Path


def personas_dir(config: TiroConfig) -> Path:
    return config.library / "personas"


def _require_enum(fm: dict, key: str, allowed: tuple, default=None) -> str:
    value = fm.get(key, default)
    if value is None:
        raise PersonaLoadError(f"missing required frontmatter key: {key}")
    value = str(value)
    if value not in allowed:
        raise PersonaLoadError(
            f"invalid {key}: {value!r} (expected one of {', '.join(allowed)})")
    return value


def parse_persona(path: Path) -> Persona:
    slug = path.stem
    if not SLUG_RE.match(slug):
        raise PersonaLoadError(
            f"invalid slug {slug!r} (lowercase letters/digits/hyphens only)")
    try:
        post = frontmatter.load(str(path))
    except Exception as e:
        raise PersonaLoadError(f"unparseable persona file: {e}") from e
    fm = dict(post.metadata)
    name = fm.get("name")
    if not name or not str(name).strip():
        raise PersonaLoadError("missing required frontmatter key: name")
    scope = _require_enum(fm, "scope", SCOPES)
    output = _require_enum(fm, "output", OUTPUTS)
    schedule = _require_enum(fm, "schedule", SCHEDULES, default="manual")
    tier = _require_enum(fm, "tier", TIERS, default="light")
    version = str(fm.get("version", "1"))
    if scope not in OUTPUT_SCOPES[output]:
        raise PersonaLoadError(
            f"output {output!r} is not valid for scope {scope!r} "
            f"(allowed scopes: {sorted(OUTPUT_SCOPES[output])})")
    body = post.content.strip()
    if not body:
        raise PersonaLoadError("persona body is empty")
    allowed = SCOPE_PLACEHOLDERS[scope]
    for match in PLACEHOLDER_RE.finditer(body):
        ph = match.group(1)
        if ph not in ALL_PLACEHOLDERS:
            raise PersonaLoadError(f"unknown placeholder {{{{{ph}}}}}")
        if ph not in allowed:
            raise PersonaLoadError(
                f"placeholder {{{{{ph}}}}} not available in scope {scope!r} "
                f"(allowed: {sorted(allowed)})")
    return Persona(slug=slug, name=str(name).strip(), version=version,
                   scope=scope, schedule=schedule, tier=tier, output=output,
                   body=body, path=path)


def load_personas(config: TiroConfig) -> tuple[list[Persona], dict[str, str]]:
    """All personas on disk: (valid, {slug: error}). Broken files are
    reported, never partially loaded (spec §5)."""
    pdir = personas_dir(config)
    if not pdir.exists():
        return [], {}
    personas, errors = [], {}
    for path in sorted(pdir.glob("*.md")):
        try:
            personas.append(parse_persona(path))
        except PersonaLoadError as e:
            errors[path.stem] = str(e)
            logger.warning("Persona %s failed to load: %s", path.name, e)
    return personas, errors


def ensure_personas(config: TiroConfig) -> Path:
    """Copy the packaged default personas on first use. Never overwrites --
    once a file exists it is user-owned (the _schema.md convention)."""
    pdir = personas_dir(config)
    pdir.mkdir(parents=True, exist_ok=True)
    for slug in DEFAULT_PERSONAS:
        target = pdir / f"{slug}.md"
        if target.exists():
            continue
        target.write_text(
            load_template(f"persona_{slug.replace('-', '_')}", ext="md"))
    return pdir
