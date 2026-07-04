"""llm_call: tier resolution, fence stripping, audit logging, error paths."""

import json

import pytest

from tiro.llm import LLMNotConfigured, llm_call, resolve_tier, strip_json_fences


def test_templates_load_and_have_placeholders():
    from tiro.intelligence.prompts import load_template

    for name, needle in [
        ("extract_metadata", "{title}"),
        ("daily_digest", "{"),
        ("ingenuity_analysis", "{"),
        ("learned_preferences", "{"),
        ("connection_notes", "{"),
    ]:
        tpl = load_template(name)
        assert needle in tpl, name


def test_strip_json_fences():
    fenced = '```json\n{"a": 1}\n```'
    assert json.loads(strip_json_fences(fenced)) == {"a": 1}
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'
    assert strip_json_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_resolve_tier_defaults_to_legacy_model_fields(test_config):
    assert resolve_tier(test_config, "heavy") == ("anthropic", test_config.opus_model)
    assert resolve_tier(test_config, "light") == ("anthropic", test_config.haiku_model)
    test_config.ai_light_model = "claude-haiku-9"
    assert resolve_tier(test_config, "light")[1] == "claude-haiku-9"


def test_anthropic_backend_without_key_raises_not_configured(test_config):
    # autouse _no_external_apis fixture has removed ANTHROPIC_API_KEY
    test_config.anthropic_api_key = None
    with pytest.raises(LLMNotConfigured):
        llm_call(test_config, "light", "hi", purpose="test")


def test_failed_call_writes_failure_audit_line(test_config):
    test_config.anthropic_api_key = None
    try:
        llm_call(test_config, "light", "hi", purpose="audit_test")
    except LLMNotConfigured:
        pass
    audit_files = list((test_config.library / "audit").glob("*.jsonl"))
    assert audit_files
    lines = [json.loads(line) for line in audit_files[0].read_text().splitlines()]
    entry = [e for e in lines if e["endpoint"] == "audit_test"][-1]
    assert entry["success"] is False and entry["service"] == "anthropic"


def test_anthropic_backend_success_path(test_config, monkeypatch):
    """Happy-path coverage for _call_anthropic itself (not just the
    error path): a realistic fake response shape must round-trip through
    llm_call into an LLMResult and a success audit line."""
    from types import SimpleNamespace

    import anthropic

    fake_response = SimpleNamespace(
        content=[SimpleNamespace(text="hello back")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )

    class _FakeSuccessClient:
        class messages:  # noqa: N801 — mimics anthropic client shape
            @staticmethod
            def create(**kwargs):
                return fake_response

    # autouse _no_external_apis fixture deletes ANTHROPIC_API_KEY; setenv
    # after that (within this test) wins.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", lambda: _FakeSuccessClient())

    result = llm_call(test_config, "light", "hi", purpose="happy")

    assert result.text == "hello back"
    assert result.provider == "anthropic"
    assert result.tokens_in == 10
    assert result.tokens_out == 5

    audit_files = list((test_config.library / "audit").glob("*.jsonl"))
    assert audit_files
    lines = [json.loads(line) for line in audit_files[0].read_text().splitlines()]
    entry = [e for e in lines if e["endpoint"] == "happy"][-1]
    assert entry["success"] is True
    assert entry["service"] == "anthropic"


def test_fake_backend_round_trip(test_config, fake_llm):
    fake_llm('{"tags": ["ai"], "entities": [], "summary": "s"}')
    result = llm_call(test_config, "light", "whatever", purpose="test")
    assert json.loads(result.text)["tags"] == ["ai"]
    assert result.provider == "fake"


def test_fake_backend_empty_queue_raises(test_config, fake_llm):
    with pytest.raises(RuntimeError, match="queue empty"):
        llm_call(test_config, "light", "x", purpose="test")


def test_extract_metadata_through_fake(test_config, fake_llm):
    from tiro.ingestion.extractors import extract_metadata

    fake_llm('{"tags": ["AI ", "ml"], "entities": [{"name": "OpenAI", "type": "Company"}], "summary": "sum"}')
    data = extract_metadata("Title", "Body text", test_config)
    assert data["tags"] == ["ai", "ml"]
    assert data["entities"] == [{"name": "OpenAI", "type": "company"}]
    assert data["summary"] == "sum"


def test_extraction_reads_beyond_2000_chars(test_config, fake_llm, monkeypatch):
    """The old 2,000-char truncation silently degraded every extraction
    (tags/entities/summary), and everything downstream that consumes them
    (digest ranking, classification, the future wiki). EXTRACT_CONTENT_CHARS
    must be >= 12000, and the composed prompt must actually carry content
    from past the old 2,000-char cutoff.

    Capture seam: extractors.py does the truncation, then calls
    extract_metadata_prompt(title, content_truncated) (imported from
    tiro.intelligence.prompts) to compose the final prompt string. We
    monkeypatch that imported name *in the extractors module* to capture
    the already-truncated `content` argument and delegate to the real
    implementation. This is the exact seam Task 12 introduced between
    "truncate" (extractors.py's job) and "compose prompt" (prompts.py's
    job) — cleaner than parsing the marker back out of the fully composed
    prompt string, and doesn't require reaching into tiro.llm internals.
    """
    from tiro.ingestion import extractors

    assert extractors.EXTRACT_CONTENT_CHARS >= 12000

    real_prompt_fn = extractors.extract_metadata_prompt
    captured = {}

    def capture_prompt(title, content):
        captured["content"] = content
        return real_prompt_fn(title, content)

    monkeypatch.setattr(extractors, "extract_metadata_prompt", capture_prompt)

    # Marker sits well past the old 2,000-char cap but before the new one;
    # body is longer than EXTRACT_CONTENT_CHARS so truncation actually fires.
    body = "start " + "x" * 5000 + " MIDDLE_MARKER " + "y" * 10000
    assert "MIDDLE_MARKER" not in body[:2000]  # sanity: old cap would have missed it

    fake_llm('{"tags": [], "entities": [], "summary": ""}')
    extractors.extract_metadata("T", body, test_config)

    assert "content" in captured, "extract_metadata_prompt was never called"
    assert "MIDDLE_MARKER" in captured["content"]
    assert len(captured["content"]) == extractors.EXTRACT_CONTENT_CHARS


def test_llm_chokepoint_is_the_only_anthropic_caller():
    """No module besides tiro/llm.py may construct an Anthropic client or
    import audited_anthropic_call — the chokepoint owns provider access."""
    from pathlib import Path

    tiro_dir = Path(__file__).parent.parent / "tiro"
    offenders = []
    for py in tiro_dir.rglob("*.py"):
        if py.name == "llm.py":
            continue
        text = py.read_text()
        if "anthropic.Anthropic(" in text or "audited_anthropic_call" in text:
            offenders.append(str(py))
    assert not offenders, offenders


def test_digest_generation_through_fake(initialized_library, fake_llm):
    from tiro.database import get_connection
    from tiro.intelligence.digest import generate_digest

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path, summary)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 'T', 'sl', 'f.md', 'a summary')"
    )
    conn.commit()
    conn.close()

    fake_llm(
        "## 1. Ranked by Importance\n1. T\n\n"
        "## 2. Grouped by Topic\n- T\n\n"
        "## 3. Grouped by Entity\n- T"
    )
    result = generate_digest(config)
    assert set(result.keys()) == {"ranked", "by_topic", "by_entity"}
    assert "T" in result["ranked"]["content"]


def test_analyze_article_through_fake(initialized_library, fake_llm):
    from tiro.database import get_connection
    from tiro.intelligence.analysis import analyze_article

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 'T', 'sl', 'a.md')"
    )
    conn.commit()
    conn.close()
    (config.articles_dir / "a.md").write_text("---\ntitle: T\n---\nBody text")

    fake_llm(json.dumps({
        "bias": {"score": 5, "notes": "x"},
        "factual_confidence": {"score": 7, "notes": "y"},
        "novelty": {"score": 3, "notes": "z"},
    }))
    result = analyze_article(config, 1)
    assert result["bias"]["score"] == 5.0
    assert result["factual_confidence"]["score"] == 7.0
    assert "analyzed_at" in result


def test_classify_articles_through_fake(initialized_library, fake_llm):
    from tiro.database import get_connection
    from tiro.intelligence.preferences import classify_articles

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    for i in range(5):
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, rating)"
            " VALUES (?, 1, ?, ?, ?, 1)",
            (f"01AAAAAAAAAAAAAAAAAAAAAA{i:02d}", f"T{i}", f"sl{i}", f"f{i}.md"),
        )
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES ('01BBBBBBBBBBBBBBBBBBBBBB', 1, 'U', 'slu', 'fu.md')"
    )
    conn.commit()
    conn.close()

    fake_llm(json.dumps(
        {"classifications": [{"article_id": 6, "tier": "must-read", "reason": "r"}]}
    ))
    result = classify_articles(config)
    assert result == [{"article_id": 6, "tier": "must-read", "reason": "r"}]


def test_generate_connection_notes_through_fake(initialized_library, fake_llm):
    from tiro.database import get_connection
    from tiro.search.semantic import generate_connection_notes

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path, summary)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 'Related', 'sl', 'f.md', 'sum')"
    )
    conn.commit()
    conn.close()

    fake_llm('{"notes": [{"article_id": 1, "note": "Builds on the source article."}]}')
    related = [{"related_article_id": 1, "similarity_score": 0.9}]
    result = generate_connection_notes("summary", "Title", related, config)
    assert result[0]["connection_note"] == "Builds on the source article."


def test_generate_connection_notes_graceful_when_not_configured(initialized_library):
    """LLMNotConfigured must be swallowed and related_articles returned
    unchanged — no exception, no connection_note annotation."""
    from tiro.search.semantic import generate_connection_notes

    related = [{"related_article_id": 1, "similarity_score": 0.9}]
    result = generate_connection_notes("summary", "Title", related, initialized_library)
    assert result == related
    assert "connection_note" not in result[0]
