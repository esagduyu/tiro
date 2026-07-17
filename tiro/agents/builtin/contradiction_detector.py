"""ContradictionDetector (Phase 6 K4) — the one new kernel agent (spec §6).

A CODE agent that deliberately writes through ctx.suggest: contradiction
claims are probabilistic judgments, so they surface as PENDING suggestions
("challenges something you trusted"), never as direct writes — the user's
accept/dismiss is the mitigation for both model error and prompt injection
via hostile article content (spec §9).

FROZEN flow: similar_articles(k=8) -> trusted-set filter (rating > 0 OR
ai_tier == 'must-read') -> ONE light-tier verdict JSON per candidate ->
suggestion kind "contradiction" gated on contradicts && confidence != low.
Budget posture: k capped at 8, <=1 light call per candidate, EMPTY TRUSTED
SET = ZERO LLM CALLS (test-asserted). Malformed verdicts are skipped and
counted, never a run failure.
"""

import json
import logging

from pydantic import BaseModel

from tiro.agents.base import AgentContext, AgentResult
from tiro.intelligence.prompts import contradiction_check_prompt
from tiro.llm import strip_json_fences

logger = logging.getLogger(__name__)

SIMILAR_K = 8              # spec §6: k capped at 8
EXCERPT_CHARS = 6000       # per-article prompt budget (K4 OPEN decision 2)
ACCEPTED_CONFIDENCE = {"med", "high"}
_CONFIDENCE_ALIASES = {"low": "low", "med": "med",
                       "medium": "med", "high": "high"}


class ContradictionOutput(BaseModel):
    article_id: int
    candidates_considered: int
    trusted_candidates: int
    contradictions_found: int
    verdict_errors: int
    suggestion_uids: list[str]


def _excerpt(text: str | None) -> str:
    return (text or "")[:EXCERPT_CHARS]


def _is_trusted(candidate: dict) -> bool:
    return ((candidate.get("rating") or 0) > 0
            or candidate.get("ai_tier") == "must-read")


def _parse_verdict(raw: str) -> dict | None:
    """Normalize one verdict; None = malformed (caller skips + counts)."""
    try:
        data = json.loads(strip_json_fences(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("contradicts"), bool):
        return None
    confidence = _CONFIDENCE_ALIASES.get(
        str(data.get("confidence", "")).strip().lower())
    if confidence is None:
        return None
    return {"contradicts": data["contradicts"], "confidence": confidence,
            "claim": str(data.get("claim") or ""),
            "counter_claim": str(data.get("counter_claim") or "")}


def _suggestion_markdown(candidate_title: str, confidence: str,
                         claim: str, counter_claim: str) -> str:
    """The chip/panel copy. Travels as suggestion payload DATA and renders
    only through the client's renderMarkdown -> DOMPurify path."""
    return (
        "**Challenges something you trusted** — this article appears to "
        f'contradict "{candidate_title}" ({confidence} confidence).\n\n'
        f"> **Its claim:** {claim}\n>\n"
        f"> **Your trusted article's claim:** {counter_claim}"
    )


class ContradictionDetector:
    # Hyphenated ON PURPOSE: the frozen CLI verb is
    # `tiro agent run contradiction-detector` (skeleton K4).
    name = "contradiction-detector"
    version = "1.0"
    inputs = {"article_id": int}
    tier = "light"
    output_model = ContradictionOutput

    def run(self, ctx: AgentContext, *, article_id: int) -> AgentResult:
        art = ctx.get_article(article_id)
        similar = ctx.similar_articles(art["uid"], k=SIMILAR_K)
        trusted = [c for c in similar if _is_trusted(c)]
        found, errors = 0, 0
        suggestion_uids: list[str] = []
        if trusted:
            new_text = _excerpt(art["content"])
            for cand in trusted:
                cand_full = ctx.get_article(cand["uid"])
                prompt = contradiction_check_prompt(
                    art["title"], new_text,
                    cand_full["title"], _excerpt(cand_full["content"]))
                raw = ctx.llm("light", prompt, purpose="contradiction",
                              max_tokens=512)
                verdict = _parse_verdict(raw)
                if verdict is None:
                    errors += 1
                    logger.warning(
                        "contradiction-detector: malformed verdict for "
                        "article %d vs %s — skipped", article_id, cand["uid"])
                    continue
                if not (verdict["contradicts"]
                        and verdict["confidence"] in ACCEPTED_CONFIDENCE):
                    continue
                payload = {
                    "article_id": article_id,
                    "article_uid": art["uid"],
                    "article_title": art["title"],
                    "candidate_id": cand_full["id"],
                    "candidate_uid": cand_full["uid"],
                    "candidate_title": cand_full["title"],
                    "claim": verdict["claim"],
                    "counter_claim": verdict["counter_claim"],
                    "confidence": verdict["confidence"],
                    "markdown": _suggestion_markdown(
                        cand_full["title"], verdict["confidence"],
                        verdict["claim"], verdict["counter_claim"]),
                }
                suggestion_uids.append(ctx.suggest(
                    "contradiction", payload,
                    citations=[art["uid"], cand_full["uid"]]))
                found += 1
        return ctx.result(ContradictionOutput(
            article_id=article_id,
            candidates_considered=len(similar),
            trusted_candidates=len(trusted),
            contradictions_found=found,
            verdict_errors=errors,
            suggestion_uids=suggestion_uids,
        ))
