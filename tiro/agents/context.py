"""The real AgentContext (spec §3): wraps queries/search/annotations/wiki/llm.

Structural provenance lives here — every read tool traces its call and
auto-appends returned article uids to the run's citations; ctx.llm goes
through tiro/llm.py's llm_call chokepoint (audit logging included) via
MODULE ATTRIBUTE access so tests can monkeypatch tiro.llm.llm_call.
"""

import copy
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frontmatter
from pydantic import BaseModel

from tiro import llm as llm_module
from tiro.agents.base import AgentResult
from tiro.agents.runtime import TraceWriter
from tiro.audit import estimate_cost
from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)


class RunContext:
    """Context handed to code agents. K2 appends gather + write tools."""

    def __init__(self, config: TiroConfig, *, trace: TraceWriter,
                 run_uid: str, model_override: dict | None = None,
                 agent_name: str = ""):
        self.config = config
        self._trace = trace
        self.run_uid = run_uid
        self._override = model_override
        self.agent_name = agent_name
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self._citations: list[str] = []          # ordered, deduped

    # -- internals --------------------------------------------------------

    def _effective_config(self) -> TiroConfig:
        if not self._override:
            return self.config
        cfg = copy.copy(self.config)
        cfg.ai_heavy_provider = self._override["provider"]
        cfg.ai_heavy_model = self._override["model"]
        cfg.ai_light_provider = self._override["provider"]
        cfg.ai_light_model = self._override["model"]
        return cfg

    def _cite(self, uids) -> None:
        for uid in uids:
            if uid and uid not in self._citations:
                self._citations.append(uid)

    def _tool(self, name: str, args: dict, result) -> None:
        self._trace.event("tool", name, args, result=result)

    @property
    def citations(self) -> list[str]:
        return list(self._citations)

    # -- model access -------------------------------------------------------

    def llm(self, tier: str, prompt: str, *, purpose: str,
            max_tokens: int = 4096) -> str:
        res = llm_module.llm_call(
            self._effective_config(), tier, prompt,
            purpose=purpose, max_tokens=max_tokens,
        )
        cost = res.cost_usd
        if cost is None:
            cost = estimate_cost(res.provider, res.model,
                                 res.tokens_in, res.tokens_out, None)
        self.tokens_in += res.tokens_in or 0
        self.tokens_out += res.tokens_out or 0
        self.cost_usd += cost or 0.0
        self._trace.event(
            "llm", purpose,
            {"tier": tier, "prompt": prompt, "max_tokens": max_tokens},
            result=res.text, tokens_in=res.tokens_in,
            tokens_out=res.tokens_out, cost_usd=cost,
        )
        return res.text

    # -- read tools (MCP mirror) ---------------------------------------------

    def search(self, q: str, *, limit: int = 10) -> list[dict]:
        from tiro.search.semantic import search_articles

        results = search_articles(q, self.config, limit=limit)
        ids = [r["id"] for r in results if r.get("id") is not None]
        if ids:
            conn = get_connection(self.config.db_path)
            try:
                ph = ",".join("?" * len(ids))
                uid_by_id = {
                    row["id"]: row["uid"] for row in conn.execute(
                        f"SELECT id, uid FROM articles WHERE id IN ({ph})", ids
                    )
                }
            finally:
                conn.close()
            for r in results:
                r["uid"] = uid_by_id.get(r["id"])
            self._cite(r["uid"] for r in results if r.get("uid"))
        self._tool("search", {"q": q, "limit": limit}, results)
        return results

    def get_article(self, uid_or_id) -> dict:
        field = "uid" if isinstance(uid_or_id, str) else "id"
        conn = get_connection(self.config.db_path)
        try:
            row = conn.execute(
                f"""SELECT a.id, a.uid, a.title, a.author, a.url, a.summary,
                           a.markdown_path, s.name AS source
                    FROM articles a LEFT JOIN sources s ON a.source_id = s.id
                    WHERE a.{field} = ?""",
                (uid_or_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            # Message format matches the pre-runtime analysis path exactly
            # (behavior lock: _load_article_for_analysis's ValueError).
            raise ValueError(f"Article {uid_or_id} not found")
        md_path = Path(row["markdown_path"])
        if not md_path.is_absolute():
            md_path = self.config.articles_dir / md_path
        if not md_path.exists():
            raise ValueError(f"Markdown file not found: {md_path}")
        post = frontmatter.load(str(md_path))
        art = {
            "id": row["id"], "uid": row["uid"], "title": row["title"],
            "author": row["author"], "url": row["url"],
            "summary": row["summary"], "source": row["source"],
            "markdown_path": row["markdown_path"], "content": post.content,
        }
        self._cite([row["uid"]])
        self._tool("get_article", {"uid_or_id": uid_or_id}, art)
        return art

    def get_highlights(self, article_uid: str | None = None, *,
                       days: int | None = None, limit: int = 50) -> list[dict]:
        """Highlights joined with their anchored note + article title.
        `days=7, limit=50` reproduces the digest recap gather byte-for-byte
        (the SQL relocated here from digest._gather_highlights in K2.2 keeps
        the same WHERE/ORDER/LIMIT semantics)."""
        where, params = [], []
        if article_uid is not None:
            where.append("a.uid = ?")
            params.append(article_uid)
        if days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            where.append("h.created_at >= ?")
            params.append(cutoff)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        conn = get_connection(self.config.db_path)
        try:
            rows = conn.execute(
                f"""SELECT h.uid AS highlight_uid, h.article_id, h.color,
                           h.created_at, h.quote_text AS quote,
                           a.title AS article_title, a.uid AS article_uid,
                           n.body_markdown AS note
                    FROM highlights h
                    JOIN articles a ON a.id = h.article_id
                    LEFT JOIN notes n ON n.highlight_id = h.id
                    {where_sql}
                    ORDER BY h.created_at DESC
                    LIMIT ?""",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        out = [dict(r) for r in rows]
        self._cite(r["article_uid"] for r in out)
        self._tool("get_highlights",
                   {"article_uid": article_uid, "days": days, "limit": limit},
                   out)
        return out

    def get_wiki_page(self, slug: str) -> dict | None:
        from tiro.wiki import read_page

        page = read_page(self.config, slug)
        if page:
            self._cite(page.get("article_uids") or [])
        self._tool("get_wiki_page", {"slug": slug}, page)
        return page

    def similar_articles(self, article_uid: str, k: int = 5) -> list[dict]:
        from tiro.search.semantic import find_related_articles

        art = self.get_article(article_uid)   # cites the anchor article too
        relations = find_related_articles(art["id"], self.config, limit=k)
        ids = [r["related_article_id"] for r in relations]
        out = []
        if ids:
            conn = get_connection(self.config.db_path)
            try:
                ph = ",".join("?" * len(ids))
                rows = {
                    row["id"]: row for row in conn.execute(
                        f"SELECT id, uid, title, summary FROM articles "
                        f"WHERE id IN ({ph})", ids
                    )
                }
            finally:
                conn.close()
            for rel in relations:
                row = rows.get(rel["related_article_id"])
                if row:
                    out.append({"id": row["id"], "uid": row["uid"],
                                "title": row["title"], "summary": row["summary"],
                                "similarity": rel.get("similarity_score")})
        self._cite(o["uid"] for o in out)
        self._tool("similar_articles", {"article_uid": article_uid, "k": k}, out)
        return out

    def list_recent_articles(self, *, hours: int = 24,
                             limit: int = 50) -> list[dict]:
        """Articles ingested in the trailing window (day-scope gather)."""
        conn = get_connection(self.config.db_path)
        try:
            rows = conn.execute(
                """SELECT a.id, a.uid, a.title, a.summary, s.name AS source
                    FROM articles a LEFT JOIN sources s ON a.source_id = s.id
                    WHERE a.ingested_at >= datetime('now', ?)
                    ORDER BY a.ingested_at DESC LIMIT ?""",
                (f"-{int(hours)} hours", limit),
            ).fetchall()
        finally:
            conn.close()
        out = [dict(r) for r in rows]
        self._cite(r["uid"] for r in out)
        self._tool("list_recent_articles", {"hours": hours, "limit": limit}, out)
        return out

    # -- the ONLY persona write path (spec §5) ------------------------------

    def suggest(self, kind: str, payload: dict, citations: list[str]) -> str:
        """Record a pending suggestion. Citations are prune-only against the
        accumulated read set (mirrors ctx.result -- fabrication is
        structurally impossible, not merely an error)."""
        from tiro.suggestions import create_suggestion

        final, extras = [], []
        for uid in citations or []:
            (final if uid in self._citations else extras).append(uid)
        if extras:
            logger.warning(
                "suggest() tried to cite %d uid(s) never read — stripped: %s",
                len(extras), extras)
        row = create_suggestion(
            self.config, persona=self.agent_name or "unknown", kind=kind,
            payload=payload, citations=final)
        self._trace.event("tool", "suggest",
                          {"kind": kind, "payload": payload,
                           "citations": final},
                          result={"uid": row["uid"]})
        return row["uid"]

    # -- kernel gather tools (K2: relocated gather SQL, + uid for citations) --

    def list_rated_articles(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Rated articles grouped by rating (loved, liked, disliked) — the
        relocated preferences._gather_rated_articles SQL. Entry dicts keep the
        exact historical keys (title/source/summary) so prompt bytes match."""
        conn = get_connection(self.config.db_path)
        try:
            rows = conn.execute("""
                SELECT a.uid, a.title, a.summary, a.rating,
                       s.name AS source_name
                FROM articles a
                LEFT JOIN sources s ON a.source_id = s.id
                WHERE a.rating IS NOT NULL
                ORDER BY a.ingested_at DESC
            """).fetchall()
        finally:
            conn.close()
        loved, liked, disliked = [], [], []
        for row in rows:
            entry = {"title": row["title"],
                     "source": row["source_name"] or "Unknown",
                     "summary": row["summary"] or ""}
            if row["rating"] == 2:
                loved.append(entry)
            elif row["rating"] == 1:
                liked.append(entry)
            elif row["rating"] == -1:
                disliked.append(entry)
        self._cite(row["uid"] for row in rows)
        self._tool("list_rated_articles", {},
                   {"loved": loved, "liked": liked, "disliked": disliked})
        return loved, liked, disliked

    def list_unrated_articles(self, *, limit: int) -> list[dict]:
        """Unclassified articles (ai_tier IS NULL) — relocated
        preferences._gather_unrated_articles SQL, capped by the caller."""
        conn = get_connection(self.config.db_path)
        try:
            rows = conn.execute("""
                SELECT a.id, a.uid, a.title, a.summary,
                       s.name AS source_name
                FROM articles a
                LEFT JOIN sources s ON a.source_id = s.id
                WHERE a.ai_tier IS NULL
                ORDER BY a.ingested_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
        finally:
            conn.close()
        out = [{"id": r["id"], "title": r["title"],
                "source": r["source_name"] or "Unknown",
                "summary": r["summary"] or ""} for r in rows]
        self._cite(r["uid"] for r in rows)
        self._tool("list_unrated_articles", {"limit": limit}, out)
        return out

    def get_vip_names(self) -> dict:
        """VIP source + author names (digest/classifier ranking context)."""
        conn = get_connection(self.config.db_path)
        try:
            sources = [r["name"] for r in conn.execute(
                "SELECT name FROM sources WHERE is_vip = 1").fetchall()]
            authors = [r["name"] for r in conn.execute(
                "SELECT name FROM authors WHERE is_vip = 1").fetchall()]
        finally:
            conn.close()
        out = {"sources": sources, "authors": authors}
        self._tool("get_vip_names", {}, out)
        return out

    # -- write tools (code agents only; personas never see these) -----------

    def set_tier(self, article_id: int, tier: str) -> None:
        """Direct ai_tier writeback (spec §4: code agent -> allowed)."""
        conn = get_connection(self.config.db_path)
        try:
            conn.execute("UPDATE articles SET ai_tier = ? WHERE id = ?",
                         (tier, article_id))
            conn.commit()
        finally:
            conn.close()
        self._tool("set_tier", {"article_id": article_id, "tier": tier}, None)

    def gather_digest_articles(self, *, unread_only: bool = False):
        """The relocated digest._gather_articles (M4-era body verbatim, plus
        a.uid in the SELECT for citation capture). Returns
        (articles, vip_sources, vip_authors, recent_ratings)."""
        from tiro.intelligence.digest import MAX_ARTICLES_FOR_DIGEST, RATING_LABELS

        conn = get_connection(self.config.db_path)
        try:
            where_clause = "WHERE a.is_read = 0" if unread_only else ""
            rows = conn.execute(f"""
                SELECT
                    a.id, a.uid, a.title, a.summary, a.published_at,
                    a.ingested_at, a.is_read, a.rating, a.relevance_weight,
                    s.name AS source_name, s.is_vip
                FROM articles a
                LEFT JOIN sources s ON a.source_id = s.id
                {where_clause}
                ORDER BY a.ingested_at DESC
                LIMIT ?
            """, (MAX_ARTICLES_FOR_DIGEST,)).fetchall()

            if not rows:
                self._tool("gather_digest_articles",
                           {"unread_only": unread_only}, [])
                return [], [], [], []

            article_ids = [row["id"] for row in rows]
            placeholders = ",".join("?" * len(article_ids))
            tag_rows = conn.execute(f"""
                SELECT at.article_id, t.name
                FROM article_tags at JOIN tags t ON t.id = at.tag_id
                WHERE at.article_id IN ({placeholders})
            """, article_ids).fetchall()
            tags_by_article: dict[int, list[str]] = {}
            for row in tag_rows:
                tags_by_article.setdefault(row["article_id"], []).append(row["name"])

            entity_rows = conn.execute(f"""
                SELECT ae.article_id, e.name
                FROM article_entities ae JOIN entities e ON e.id = ae.entity_id
                WHERE ae.article_id IN ({placeholders})
            """, article_ids).fetchall()
            entities_by_article: dict[int, list[str]] = {}
            for row in entity_rows:
                entities_by_article.setdefault(row["article_id"], []).append(row["name"])

            author_vip_rows = conn.execute(f"""
                SELECT DISTINCT aa.article_id
                FROM article_authors aa JOIN authors au ON au.id = aa.author_id
                WHERE aa.article_id IN ({placeholders}) AND au.is_vip = 1
            """, article_ids).fetchall()
            vip_author_article_ids = {row["article_id"] for row in author_vip_rows}

            articles, recent_ratings = [], []
            for row in rows:
                aid = row["id"]
                is_vip = bool(row["is_vip"]) or aid in vip_author_article_ids
                articles.append({
                    "id": aid,
                    "title": row["title"],
                    "source": row["source_name"] or "Unknown",
                    "is_vip": is_vip,
                    "tags": tags_by_article.get(aid, []),
                    "entities": entities_by_article.get(aid, []),
                    "summary": row["summary"] or "",
                    "published_date": row["published_at"] or row["ingested_at"],
                    "relevance_weight": row["relevance_weight"] or 1.0,
                })
                if row["rating"] is not None:
                    recent_ratings.append({
                        "title": row["title"],
                        "source": row["source_name"] or "Unknown",
                        "rating_label": RATING_LABELS.get(row["rating"], "Unknown"),
                        "summary": row["summary"] or "",
                    })

            vip_sources = [r["name"] for r in conn.execute(
                "SELECT name FROM sources WHERE is_vip = 1").fetchall()]
            vip_authors = [r["name"] for r in conn.execute(
                "SELECT name FROM authors WHERE is_vip = 1").fetchall()]
        finally:
            conn.close()

        self._cite(row["uid"] for row in rows)
        self._tool("gather_digest_articles", {"unread_only": unread_only},
                   {"articles": articles, "vip_sources": vip_sources,
                    "vip_authors": vip_authors,
                    "recent_ratings": recent_ratings})
        return articles, vip_sources, vip_authors, recent_ratings

    def cache_analysis(self, article_id: int, analysis: dict) -> None:
        """Write tool: cache an ingenuity analysis blob on the article row
        (delegates to analysis._cache_analysis — single write owner)."""
        from tiro.intelligence.analysis import _cache_analysis

        _cache_analysis(self.config, article_id, analysis)
        self._tool("cache_analysis", {"article_id": article_id}, None)

    def create_digest(self, date_str: str, sections: dict, article_ids: list[int]) -> None:
        """Write tool: cache digest sections (delegates to digest._cache_digest,
        which keeps sole ownership of the digests-table write)."""
        from tiro.intelligence.digest import _cache_digest

        _cache_digest(self.config, date_str, sections, article_ids)
        self._tool("create_digest",
                   {"date": date_str, "types": sorted(sections),
                    "article_count": len(article_ids)}, None)

    # -- result assembly (OPEN decision 3) ---------------------------------

    def result(self, outputs: BaseModel,
               citations: list[str] | None = None) -> AgentResult:
        if citations is None:
            final = list(self._citations)
        else:
            final, extras = [], []
            for uid in citations:
                (final if uid in self._citations else extras).append(uid)
            if extras:
                logger.warning(
                    "Agent tried to cite %d uid(s) it never read — stripped: %s",
                    len(extras), extras,
                )
        return AgentResult(
            outputs=outputs, citations=final,
            tokens_in=self.tokens_in, tokens_out=self.tokens_out,
            cost_usd=self.cost_usd, run_uid=self.run_uid,
        )
