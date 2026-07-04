"""llm_call: tier resolution, fence stripping, audit logging, error paths."""

import json

import pytest

from tiro.llm import LLMNotConfigured, llm_call, resolve_tier, strip_json_fences


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
