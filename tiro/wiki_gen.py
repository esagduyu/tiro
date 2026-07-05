"""On-demand wiki page generation (Phase 1b, wave W1): the trust boundary
between the library's data and a written wiki page.

Every page is generated from exactly three ingredients: the gathered
articles' summaries/trust-signals, the (user-editable) `_schema.md`
maintenance instructions, and the page's OWN prior body (never another
page's). This module never reads another wiki page's content -- cross-page
context is out of scope for W1 by design.

Citations are mandatory, not cosmetic: the model's output is scanned for
`[[stem|label]]` links, and every target is resolved against the stems of
the articles that were actually gathered for this node. If NONE resolve,
the whole generation is discarded -- no file, no derived rows, no log.md
line, no index.md update. This is the one hard trust invariant: a wiki
page can never exist that cites nothing real. A page with SOME unresolved
citations alongside resolved ones is allowed (mixed generations still
count what did resolve); `write_page`'s `article_uids` gets only the
resolved subset, so the derived index and `source_count` never overcount.
"""

import re

from tiro.audit import estimate_cost
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.intelligence.digest import RATING_LABELS
from tiro.intelligence.prompts import wiki_page_prompt
from tiro.llm import llm_call
from tiro.wiki import ensure_schema_file, read_page, wiki_slugify, write_page

# [[stem]] or [[stem|label]] -- the label (if present) is display-only and
# never participates in resolution.
CITATION_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

_KIND_SLUG_PREFIX = {"entity": "entities", "concept": "concepts"}
_SLUG_PREFIX_NODE_TYPE = {"entities": "entity", "concepts": "tag"}


class WikiGenerationError(RuntimeError):
    """Generation failed in a way that must leave the library untouched --
    most commonly zero resolvable citations in the model's output."""


def _strip_md_fences(text: str) -> str:
    """Remove a wrapping ``` ... ``` fence if the model added one despite
    being told the response is a bare markdown body (models wrap anyway --
    same defensive posture as `tiro.llm.strip_json_fences`, just not
    JSON-shaped, so it can't reuse that helper directly)."""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    parts = cleaned.split("\n", 1)
    cleaned = parts[1] if len(parts) == 2 else ""
    cleaned = cleaned.rstrip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def gather_node_articles(config: TiroConfig, node_type: str, node_id: int) -> list[dict]:
    """Gather every article linked to an entity or tag node, shaped for
    `wiki_page_prompt`'s `articles` param.

    node_type: "entity" (via `article_entities`) or "tag" (via
    `article_tags`). Returns [] if the node has no linked articles (or
    doesn't exist) -- this function doesn't validate node existence, that's
    `generate_wiki_page`'s job (it needs the node's name/entity_type too).

    Each dict: {stem, title, summary, rating_label, is_vip,
    relevance_weight, uid}. `stem` is `markdown_path` minus `.md` -- the
    citation target the model is instructed to use. Summaries may be empty
    strings (no AI key configured at ingest time) -- still usable, the
    prompt template already handles a missing summary.
    """
    if node_type == "entity":
        junction, fk = "article_entities", "entity_id"
    elif node_type == "tag":
        junction, fk = "article_tags", "tag_id"
    else:
        raise ValueError(f"invalid node_type: {node_type!r} (expected 'entity' or 'tag')")

    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            f"""SELECT a.uid, a.title, a.summary, a.rating, a.relevance_weight,
                       a.markdown_path, s.is_vip AS source_is_vip
                FROM {junction} j
                JOIN articles a ON a.id = j.article_id
                LEFT JOIN sources s ON a.source_id = s.id
                WHERE j.{fk} = ?
                ORDER BY a.id""",
            (node_id,),
        ).fetchall()
    finally:
        conn.close()

    articles = []
    for row in rows:
        markdown_path = row["markdown_path"] or ""
        stem = markdown_path[:-3] if markdown_path.endswith(".md") else markdown_path
        weight = row["relevance_weight"]
        articles.append(
            {
                "stem": stem,
                "title": row["title"],
                "summary": row["summary"] or "",
                "rating_label": RATING_LABELS.get(row["rating"]),
                "is_vip": bool(row["source_is_vip"]),
                "relevance_weight": weight if weight is not None else 1.0,
                "uid": row["uid"],
            }
        )
    return articles


def _resolve_node(config: TiroConfig, node_type: str, node_id: int) -> tuple[str, str, str | None]:
    """Look up a node's (name, kind, entity_type). Raises ValueError (the
    404-equivalent) if the node doesn't exist."""
    conn = get_connection(config.db_path)
    try:
        if node_type == "entity":
            row = conn.execute(
                "SELECT name, entity_type FROM entities WHERE id = ?", (node_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"unknown entity id: {node_id}")
            return row["name"], "entity", row["entity_type"]
        elif node_type == "tag":
            row = conn.execute("SELECT name FROM tags WHERE id = ?", (node_id,)).fetchone()
            if not row:
                raise ValueError(f"unknown tag id: {node_id}")
            return row["name"], "concept", None
        else:
            raise ValueError(f"invalid node_type: {node_type!r} (expected 'entity' or 'tag')")
    finally:
        conn.close()


def _find_node_id(config: TiroConfig, node_type: str, title: str) -> int | None:
    """Recover a node's id from its (raw) name for `regenerate_wiki_page`,
    which only has a slug + the page's stored title to work from. Tags are
    unique by name. Entities are not (W1: distinct entity_types can share a
    name and collapse onto the same page) -- picks the lowest id
    deterministically; disambiguating collisions is W3 lint's job, not
    this one."""
    conn = get_connection(config.db_path)
    try:
        if node_type == "entity":
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ? ORDER BY id LIMIT 1", (title,)
            ).fetchone()
        else:
            row = conn.execute("SELECT id FROM tags WHERE name = ?", (title,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _generate(config: TiroConfig, node_type: str, node_id: int, *, from_scratch: bool) -> dict:
    """Shared body for `generate_wiki_page`/`regenerate_wiki_page`.

    `from_scratch=True` (regenerate) forces `prior_body=None` in the
    prompt while still preserving the prior page's uid + pinned note (if a
    page already exists) -- a fresh take on the content, not a fresh page.
    """
    title, kind, entity_type = _resolve_node(config, node_type, node_id)
    name_slug = wiki_slugify(title)
    if not name_slug:
        # wiki_slugify() strips every non a-z0-9 run, so a name written
        # entirely in a non-Latin script (e.g. "中文实体") collapses to "".
        # Left unguarded, every such name would resolve to the SAME
        # unresolvable page file (wiki_dir/{kind}/.md) -- fail fast here,
        # before any gather/LLM call, rather than let it through to
        # write_page()'s page_path() guard (defense in depth, W3).
        raise WikiGenerationError(
            f"{node_type} {node_id} ({title!r}) yields an empty slug -- "
            "non-Latin names are not yet supported (W3)"
        )
    slug = f"{_KIND_SLUG_PREFIX[kind]}/{name_slug}"

    articles = gather_node_articles(config, node_type, node_id)
    if not articles:
        raise WikiGenerationError(
            f"no articles linked to {node_type} {node_id} ({title!r}); "
            "cannot generate a page with zero possible citations"
        )

    prior = read_page(config, slug)
    prior_body = None if from_scratch else (prior["body"] if prior else None)
    prior_uid = prior["uid"] if prior else None
    pinned_note = (prior["user_pinned_note"] if prior else "") or ""

    schema_instructions = ensure_schema_file(config).read_text()

    prompt = wiki_page_prompt(
        schema_instructions=schema_instructions,
        kind=kind,
        title=title,
        entity_type=entity_type,
        prior_body=prior_body,
        articles=articles,
        pinned_note=pinned_note or None,
    )

    result = llm_call(config, "light", prompt, purpose="wiki_page", max_tokens=2048)
    body = _strip_md_fences(result.text)

    stems = {a["stem"] for a in articles}
    # dict.fromkeys: de-dupe while preserving first-seen order (informational
    # only -- resolution below is order-independent).
    cited_targets = list(dict.fromkeys(CITATION_RE.findall(body)))
    resolved = [t for t in cited_targets if t in stems]
    if not resolved:
        raise WikiGenerationError(
            f"generated page for {slug!r} cited zero resolvable articles "
            "out of the gathered set; refusing to write"
        )

    resolved_set = set(resolved)
    cited_uids = [a["uid"] for a in articles if a["stem"] in resolved_set]

    page = write_page(
        config,
        slug=slug,
        kind=kind,
        title=title,
        entity_type=entity_type,
        article_uids=cited_uids,
        body=body,
        generated_by={"model": result.model, "tier": "light"},
        user_pinned_note=pinned_note,
        status="fresh",
        uid=prior_uid,
    )

    cost_estimate = estimate_cost(
        result.provider, result.model, result.tokens_in, result.tokens_out, None
    )

    return {
        "slug": page["slug"],
        "title": page["title"],
        "created": prior is None,
        "cited_articles": len(cited_uids),
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_estimate": cost_estimate,
    }


def generate_wiki_page(config: TiroConfig, node_type: str, node_id: int) -> dict:
    """Generate (create) or update the wiki page for an entity/tag node.

    node_type: "entity" -> kind "entity"; "tag" -> kind "concept". If a
    page already exists at the derived slug, this is an UPDATE: the prior
    page's body is fed back into the prompt (merge-in-place, per the
    template's compression instructions) and its uid + pinned note are
    preserved. Raises ValueError if the node doesn't exist (404-equivalent)
    or WikiGenerationError if the model's output cites zero resolvable
    articles -- in the error case nothing is written: no file, no derived
    rows, no log.md line.
    """
    return _generate(config, node_type, node_id, from_scratch=False)


def regenerate_wiki_page(config: TiroConfig, slug: str) -> dict:
    """Regenerate an existing wiki page from scratch (prior_body=None),
    preserving its uid and user_pinned_note. Raises ValueError if the slug
    is unknown or its node can no longer be resolved (both 404-equivalents
    -- the route maps either to 404)."""
    prior = read_page(config, slug)
    if prior is None:
        raise ValueError(f"unknown wiki page slug: {slug!r}")

    prefix = slug.split("/", 1)[0]
    node_type = _SLUG_PREFIX_NODE_TYPE.get(prefix)
    if node_type is None:
        raise ValueError(f"unrecognized wiki slug: {slug!r}")

    node_id = _find_node_id(config, node_type, prior["title"])
    if node_id is None:
        raise ValueError(f"no {node_type} matches wiki page title {prior['title']!r}")

    return _generate(config, node_type, node_id, from_scratch=True)
