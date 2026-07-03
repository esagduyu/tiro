"""M6: external-API audit log — writer, cost estimates, readers, wrapper."""

import json
from datetime import date

import pytest

from tiro.audit import (
    audited_anthropic_call,
    estimate_cost,
    log_api_call,
    read_audit_entries,
    summarize,
)


def _today_file(config):
    return config.library / "audit" / f"{date.today().isoformat()}.jsonl"


def test_log_api_call_writes_jsonl(initialized_library):
    log_api_call(
        initialized_library, "anthropic", endpoint="digest",
        model="claude-opus-4-6", tokens_in=1000, tokens_out=500, duration_ms=1234,
    )
    lines = _today_file(initialized_library).read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["service"] == "anthropic"
    assert entry["endpoint"] == "digest"
    assert entry["tokens_in"] == 1000
    assert entry["success"] is True
    assert "timestamp" in entry
    # opus 4.6: 1000/1M * $5 + 500/1M * $25 = 0.005 + 0.0125
    assert entry["cost_estimate"] == pytest.approx(0.0175)


def test_estimate_cost_prefix_match_and_unknown():
    assert estimate_cost("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 0, None) == pytest.approx(1.0)
    assert estimate_cost("openai_tts", "tts-1", None, None, 1_000_000) == pytest.approx(15.0)
    assert estimate_cost("openai_tts", "tts-1-hd", None, None, 100_000) == pytest.approx(3.0)
    assert estimate_cost("anthropic", "some-future-model", 1000, 1000, None) is None
    assert estimate_cost("imap", None, None, None, None) is None


def test_log_api_call_never_raises(initialized_library, monkeypatch):
    # Point the library at an unwritable location — the call must not raise
    monkeypatch.setattr(type(initialized_library), "library",
                        property(lambda self: __import__("pathlib").Path("/dev/null/nope")))
    log_api_call(initialized_library, "anthropic", endpoint="digest")  # must not raise


def test_read_and_summarize(initialized_library):
    log_api_call(initialized_library, "anthropic", endpoint="digest",
                 model="claude-opus-4-6", tokens_in=100, tokens_out=50)
    log_api_call(initialized_library, "imap", endpoint="check", count=3)
    log_api_call(initialized_library, "anthropic", endpoint="analysis",
                 model="claude-opus-4-6", tokens_in=200, tokens_out=10, success=False, error="boom")

    entries = read_audit_entries(initialized_library)
    assert len(entries) == 3
    only_anthropic = read_audit_entries(initialized_library, service="anthropic")
    assert len(only_anthropic) == 2

    month = date.today().isoformat()[:7]
    assert len(read_audit_entries(initialized_library, month=month)) == 3

    rollup = summarize(entries)
    assert rollup["anthropic"]["calls"] == 2
    assert rollup["anthropic"]["failures"] == 1
    assert rollup["anthropic"]["tokens_in"] == 300
    assert rollup["imap"]["calls"] == 1


class _FakeUsage:
    input_tokens = 111
    output_tokens = 22


class _FakeResponse:
    usage = _FakeUsage()


class _FakeClient:
    class messages:  # noqa: N801 — mimics anthropic client shape
        @staticmethod
        def create(**kwargs):
            if kwargs.get("model") == "explode":
                raise RuntimeError("api down")
            return _FakeResponse()


def test_audited_anthropic_call_logs_usage(initialized_library):
    resp = audited_anthropic_call(
        initialized_library, _FakeClient(), endpoint="extract_metadata",
        model="claude-haiku-4-5-20251001", max_tokens=10,
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.usage.input_tokens == 111
    entry = json.loads(_today_file(initialized_library).read_text().splitlines()[-1])
    assert entry["tokens_in"] == 111 and entry["tokens_out"] == 22
    assert entry["endpoint"] == "extract_metadata"
    assert entry["cost_estimate"] is not None


def test_audited_anthropic_call_logs_failure_and_reraises(initialized_library):
    with pytest.raises(RuntimeError):
        audited_anthropic_call(initialized_library, _FakeClient(), endpoint="digest",
                               model="explode", messages=[])
    entry = json.loads(_today_file(initialized_library).read_text().splitlines()[-1])
    assert entry["success"] is False
    assert "api down" in entry["error"]
