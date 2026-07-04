"""Centralized prompt templates for Tiro's intelligence layer.

Static prompt skeletons live as data files under `tiro/intelligence/templates/`
so they can be treated as user-visible content (Phase 1b wiki docs, Phase 6
personas). Dynamic composition (formatting lists of articles/ratings into
lines) stays in Python; each `*_prompt` function loads its template and fills
in the placeholders.
"""

from importlib.resources import files


def load_template(name: str) -> str:
    """Read a prompt template's raw text by name (without the .txt suffix)."""
    return (files("tiro.intelligence") / "templates" / f"{name}.txt").read_text()


def extract_metadata_prompt(title: str, content: str) -> str:
    """Build the metadata-extraction prompt for Claude Haiku.

    Args:
        title: Article title.
        content: Article content (already truncated by the caller).
    """
    return load_template("extract_metadata").format(title=title, content=content)


def daily_digest_prompt(
    vip_sources: list[str],
    recent_ratings: list[dict],
    articles: list[dict],
) -> str:
    """Build the daily digest prompt for Opus 4.6.

    Args:
        vip_sources: Names of VIP sources (e.g., ["Stratechery", "Matt Levine"])
        recent_ratings: List of dicts with keys: title, source, rating_label, summary
        articles: List of dicts with keys: id, title, source, is_vip, tags, entities, summary, published_date
    """
    # Format VIP sources
    vip_str = ", ".join(vip_sources) if vip_sources else "None set"

    # Format recent ratings
    if recent_ratings:
        ratings_lines = []
        for r in recent_ratings:
            ratings_lines.append(
                f"- [{r['rating_label']}] \"{r['title']}\" ({r['source']}): {r['summary']}"
            )
        ratings_str = "\n".join(ratings_lines)
    else:
        ratings_str = "No ratings yet."

    # Format articles
    article_lines = []
    for a in articles:
        vip_marker = " [VIP]" if a["is_vip"] else ""
        tags = ", ".join(a["tags"]) if a["tags"] else "none"
        entities = ", ".join(a["entities"]) if a["entities"] else "none"
        weight = a.get("relevance_weight", 1.0)
        weight_note = f" | Relevance: {weight:.2f}" if weight < 1.0 else ""
        article_lines.append(
            f"- ID: {a['id']} | Title: \"{a['title']}\" | Source: {a['source']}{vip_marker}{weight_note}\n"
            f"  Tags: {tags}\n"
            f"  Entities: {entities}\n"
            f"  Published: {a['published_date'] or 'unknown'}\n"
            f"  Summary: {a['summary'] or 'No summary available.'}"
        )
    articles_str = "\n\n".join(article_lines)

    return load_template("daily_digest").format(
        vip_str=vip_str,
        ratings_str=ratings_str,
        articles_str=articles_str,
    )


def ingenuity_analysis_prompt(full_article_text: str, source_name: str) -> str:
    """Build the ingenuity/trust analysis prompt for Opus 4.6.

    Args:
        full_article_text: The full markdown text of the article.
        source_name: The name of the source (e.g., "Stratechery").
    """
    return load_template("ingenuity_analysis").format(
        full_article_text=full_article_text,
        source_name=source_name,
    )


def learned_preferences_prompt(
    loved_articles: list[dict],
    liked_articles: list[dict],
    disliked_articles: list[dict],
    vip_sources: list[str],
    unrated_articles: list[dict],
) -> str:
    """Build the learned-preferences classification prompt for Opus 4.6.

    Args:
        loved_articles: Dicts with keys: title, source, summary (rating 2)
        liked_articles: Dicts with keys: title, source, summary (rating 1)
        disliked_articles: Dicts with keys: title, source, summary (rating -1)
        vip_sources: Names of VIP sources
        unrated_articles: Dicts with keys: id, title, source, summary (to classify)
    """

    def _format_rated(articles: list[dict]) -> str:
        if not articles:
            return "None yet."
        lines = []
        for a in articles:
            lines.append(
                f"- \"{a['title']}\" ({a['source']}): {a['summary'] or 'No summary.'}"
            )
        return "\n".join(lines)

    def _format_unrated(articles: list[dict]) -> str:
        lines = []
        for a in articles:
            lines.append(
                f"- ID: {a['id']} | \"{a['title']}\" ({a['source']}): "
                f"{a['summary'] or 'No summary.'}"
            )
        return "\n".join(lines)

    vip_str = ", ".join(vip_sources) if vip_sources else "None set"

    return load_template("learned_preferences").format(
        loved_str=_format_rated(loved_articles),
        liked_str=_format_rated(liked_articles),
        disliked_str=_format_rated(disliked_articles),
        vip_str=vip_str,
        unrated_str=_format_unrated(unrated_articles),
    )


def connection_notes_prompt(
    article_title: str,
    article_summary: str,
    related_context: str,
) -> str:
    """Build the connection-notes prompt for Claude Haiku.

    Args:
        article_title: Title of the source article.
        article_summary: Summary of the source article.
        related_context: Pre-formatted lines describing the related articles
            (see `tiro.search.semantic.generate_connection_notes`).
    """
    return load_template("connection_notes").format(
        article_title=article_title,
        article_summary=article_summary,
        related_context=related_context,
    )
