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


def test_read_audit_entries_date_exact_match(initialized_library):
    # A real entry logged "today", plus a second file for an unrelated date
    # written directly (no log_api_call involved) to prove date= is an exact
    # filename match, not a prefix/substring match.
    log_api_call(initialized_library, "anthropic", endpoint="digest",
                 model="claude-opus-4-6", tokens_in=10, tokens_out=5)

    old_file = initialized_library.library / "audit" / "2020-01-01.jsonl"
    old_entry = {"service": "anthropic", "endpoint": "old", "success": True}
    old_file.write_text(json.dumps(old_entry) + "\n")

    today = date.today().isoformat()

    old_results = read_audit_entries(initialized_library, date="2020-01-01")
    assert len(old_results) == 1
    assert old_results[0]["endpoint"] == "old"

    today_results = read_audit_entries(initialized_library, date=today)
    assert len(today_results) == 1
    assert today_results[0]["endpoint"] == "digest"


def test_read_audit_entries_skips_corrupt_lines(initialized_library, caplog):
    log_api_call(initialized_library, "anthropic", endpoint="digest",
                 model="claude-opus-4-6", tokens_in=10, tokens_out=5)

    today_file = _today_file(initialized_library)
    with today_file.open("a") as f:
        f.write("{not json\n")
        f.write(json.dumps({"service": "anthropic", "endpoint": "second", "success": True}) + "\n")

    with caplog.at_level("WARNING"):
        entries = read_audit_entries(initialized_library)

    assert len(entries) == 2
    assert {e["endpoint"] for e in entries} == {"digest", "second"}
    assert any("corrupt audit line" in record.message for record in caplog.records)


def test_extract_metadata_is_audited(initialized_library, monkeypatch):
    """The real call site routes through the wrapper (proven by a fake client)."""
    import tiro.ingestion.extractors as ex

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakeAnthropicModule:
        @staticmethod
        def Anthropic():
            return _FakeClient()

    monkeypatch.setattr(ex, "anthropic", FakeAnthropicModule)
    # _FakeClient returns _FakeResponse which lacks .content — extract_metadata's
    # broad except catches the AttributeError and returns defaults; the audit
    # entry must still exist because the API call itself succeeded.
    ex.extract_metadata("T", "body", initialized_library)
    entries = read_audit_entries(initialized_library, service="anthropic")
    assert entries and entries[-1]["endpoint"] == "extract_metadata"


def test_imap_failure_is_audited(initialized_library, monkeypatch):
    import tiro.ingestion.imap as imap_mod

    initialized_library.imap_user = "u@example.com"
    initialized_library.imap_password = "pw"

    class BoomIMAP:
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("nope")

    monkeypatch.setattr(imap_mod.imaplib, "IMAP4_SSL", BoomIMAP)
    with pytest.raises(RuntimeError):
        imap_mod.check_imap_inbox(initialized_library)
    entries = read_audit_entries(initialized_library, service="imap")
    assert entries and entries[-1]["success"] is False


def test_smtp_send_is_audited(initialized_library, monkeypatch):
    import tiro.intelligence.email_digest as ed

    initialized_library.digest_email = "u@example.com"

    class FakeSMTP:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def sendmail(self, *a, **k): ...

    monkeypatch.setattr(ed.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(ed, "get_cached_digest", lambda *a, **k: {
        "ranked": {"content": "# d", "article_ids": [], "created_at": "2026-07-03 10:00:00"}
    })
    ed.send_digest_email(initialized_library)
    entries = read_audit_entries(initialized_library, service="smtp")
    assert entries and entries[-1]["success"] is True
    assert entries[-1]["bytes_out"] > 0


def test_cli_audit_json(initialized_library, capsys):
    from types import SimpleNamespace

    from tiro import cli

    log_api_call(initialized_library, "anthropic", endpoint="digest",
                 model="claude-opus-4-6", tokens_in=100, tokens_out=50)
    month = date.today().isoformat()[:7]
    cli.cmd_audit(SimpleNamespace(config="unused", date=None, month=month,
                                  service=None, json=True,
                                  _config_override=initialized_library))
    out = json.loads(capsys.readouterr().out)
    assert out["anthropic"]["calls"] == 1


def test_audit_date_month_mutually_exclusive(monkeypatch):
    """--date and --month together should be a clean argparse error, not a
    silently-ANDed misleading/empty report."""
    import sys

    from tiro import cli

    monkeypatch.setattr(
        sys, "argv",
        ["tiro", "audit", "--date", "2026-01-01", "--month", "2026-01"],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2
