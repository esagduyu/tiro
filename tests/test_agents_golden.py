"""Golden behavior locks for the four migrated agents (spec §4).

Each test pins the EXACT llm_call transcript (tier, purpose, max_tokens,
prompt BYTES) plus the writeback side effects. Written against the
pre-runtime code first (must pass on the old orchestration), kept green
across the refactor. Recorders monkeypatch BOTH seams: the module-bound
name in the legacy module (pre-refactor) and tiro.llm.llm_call (which
context.py reads via module attribute, post-refactor).
"""

import json

import pytest

from tiro.llm import LLMResult


class _Recorder:
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, config, tier, prompt, *, purpose,
                 max_tokens=1024, system=None):
        self.calls.append({"tier": tier, "prompt": prompt,
                           "purpose": purpose, "max_tokens": max_tokens})
        return LLMResult(text=self.responses.pop(0), provider="fake",
                         model="golden", tokens_in=10, tokens_out=20)


@pytest.fixture
def record_llm(monkeypatch):
    """Patch every llm_call seam with a shared recorder factory."""

    def _install(*responses):
        rec = _Recorder(*responses)
        # pre-refactor bound-name seams (harmless no-ops once refactored,
        # guarded because the attribute disappears after each task). These
        # go FIRST, before the tiro.llm.llm_call patch below: a target
        # module here may not have been imported by anything yet in this
        # test session, and monkeypatch's string-target resolution imports
        # it on first use — if tiro.llm.llm_call were already patched to
        # `rec` at that moment, the module's own `from tiro.llm import
        # llm_call` would bind directly to `rec` as a normal import side
        # effect, and monkeypatch would then capture *that* as the
        # "original" value to restore on teardown, permanently leaking the
        # recorder into every later test that imports the same module.
        for target in (
            "tiro.ingestion.extractors.llm_call",
            "tiro.intelligence.preferences.llm_call",
            "tiro.intelligence.digest.llm_call",
            "tiro.intelligence.analysis.llm_call",
        ):
            try:
                monkeypatch.setattr(target, rec)
            except AttributeError:
                pass
        # post-refactor seam (context.py: module-attribute access) — last.
        monkeypatch.setattr("tiro.llm.llm_call", rec)
        return rec

    return _install


# --- MetadataExtractor (Task 6) -------------------------------------------

GOLDEN_TITLE = "The Golden Article"
GOLDEN_BODY = ("Sentence about testing pipelines. " * 500)  # > 12000 chars
GOLDEN_EXTRACT_RESPONSE = json.dumps({
    "tags": ["AI ", "Testing", "t3", "t4", "t5", "t6", "t7", "t8", "t9"],
    "entities": [
        {"name": " Anthropic ", "type": " Company "},
        {"bogus": "no name key"},
        "not-a-dict",
    ],
    "summary": "A golden summary.",
})


def test_metadata_extractor_transcript_golden(initialized_library, record_llm):
    from tiro.ingestion.extractors import EXTRACT_CONTENT_CHARS, extract_metadata
    from tiro.intelligence.prompts import extract_metadata_prompt

    rec = record_llm(GOLDEN_EXTRACT_RESPONSE)
    out = extract_metadata(GOLDEN_TITLE, GOLDEN_BODY, initialized_library)

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["tier"] == "light"
    assert call["purpose"] == "extract_metadata"
    assert call["max_tokens"] == 1024
    # THE byte-identity gate: exact prompt bytes, incl. the 12000-char cap.
    assert call["prompt"] == extract_metadata_prompt(
        GOLDEN_TITLE, GOLDEN_BODY[:EXTRACT_CONTENT_CHARS]
    )

    # Normalization pinned: lowercase/strip/max-8 tags; entity validation.
    assert out["tags"] == ["ai", "testing", "t3", "t4", "t5", "t6", "t7", "t8"]
    assert out["entities"] == [{"name": "Anthropic", "type": "company"}]
    assert out["summary"] == "A golden summary."


def test_metadata_extractor_empty_defaults_on_not_configured(initialized_library):
    # No API key in test env (conftest deletes it) and provider=anthropic:
    # must return empty defaults, never raise (processor.py relies on this).
    from tiro.ingestion.extractors import extract_metadata

    out = extract_metadata("T", "body", initialized_library)
    assert out == {"tags": [], "entities": [], "summary": ""}


def test_metadata_extractor_empty_defaults_on_garbage_json(
    initialized_library, record_llm,
):
    from tiro.ingestion.extractors import extract_metadata

    record_llm("this is not json at all")
    out = extract_metadata("T", "body", initialized_library)
    assert out == {"tags": [], "entities": [], "summary": ""}
