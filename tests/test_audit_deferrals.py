"""Phase-0 final-review deferrals: re-embed metadata parity + audit coverage.

Three items deferred from the Phase-0 final review (see CLAUDE.md M6/M7
conventions for the audit-log invariant):
  (a) retry_pending_vectors() re-embedded with a thinner metadata shape than
      the initial ingest upsert in processor.py.
  (b) imap.search()'s exception/non-OK-status path was unaudited (connect,
      login, and select failures already were).
  (c) TTS stream_article_audio()'s mid-stream client-disconnect path was
      unaudited and untested.
"""

import json

import httpx


def test_retry_reembeds_with_full_metadata(initialized_library, monkeypatch):
    from tiro.database import get_connection
    from tiro.vectorstore import retry_pending_vectors

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type, is_vip) VALUES ('Src', 'web', 1)")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path,"
        " published_at, vector_status)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 'T', 'sl', 'sl.md', '2026-01-02', 'pending')"
    )
    conn.execute("INSERT INTO tags (uid, name) VALUES ('01T', 'ai')")
    conn.execute("INSERT INTO article_tags (article_id, tag_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    (config.articles_dir / "sl.md").write_text("---\ntitle: T\n---\nbody")

    captured = {}

    class FakeCollection:
        def upsert(self, ids, documents, metadatas):
            captured["metadatas"] = metadatas

        def delete(self, ids):
            pass

    monkeypatch.setattr("tiro.vectorstore.get_collection", lambda: FakeCollection())
    n = retry_pending_vectors(config)
    assert n == 1
    md = captured["metadatas"][0]
    assert md["source"] == "Src"
    assert md["is_vip"] is True
    assert md["tags"] == "ai"
    assert md["published_at"] == "2026-01-02"
    assert md["title"] == "T"
    assert md["article_id"] == 1


def test_imap_search_failure_is_audited(test_config, monkeypatch):
    from tiro.ingestion.imap import check_imap_inbox

    class FakeIMAP:
        def __init__(self, *a, **k): ...
        def login(self, *a): ...
        def select(self, *a, **k): return ("OK", [b""])
        def search(self, *a): raise RuntimeError("boom")
        def close(self): ...
        def logout(self): ...

    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP)
    test_config.imap_user = "u@example.com"
    test_config.imap_password = "pw"
    try:
        check_imap_inbox(test_config)
    except Exception:
        pass
    files = list((test_config.library / "audit").glob("*.jsonl"))
    entries = [json.loads(line) for f in files for line in f.read_text().splitlines()]
    assert any(e["service"] == "imap" and e["success"] is False for e in entries)


def test_imap_search_non_ok_status_is_audited(test_config, monkeypatch):
    """A non-exception 'not OK' search status must also be audited, not just
    a raised exception."""
    from tiro.ingestion.imap import check_imap_inbox

    class FakeIMAP:
        def __init__(self, *a, **k): ...
        def login(self, *a): ...
        def select(self, *a, **k): return ("OK", [b""])
        def search(self, *a): return ("NO", [None])
        def close(self): ...
        def logout(self): ...

    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP)
    test_config.imap_user = "u@example.com"
    test_config.imap_password = "pw"
    result = check_imap_inbox(test_config)  # non-OK + empty falls through to the no-op return
    assert result["fetched"] == 0
    files = list((test_config.library / "audit").glob("*.jsonl"))
    entries = [json.loads(line) for f in files for line in f.read_text().splitlines()]
    assert any(
        e["service"] == "imap" and e["success"] is False and "search returned" in (e["error"] or "")
        for e in entries
    )


async def test_tts_disconnect_writes_audit(initialized_library, monkeypatch):
    """Drive the generator one chunk then abandon it — closing an async
    generator raises GeneratorExit inside it, which the `finally` must log."""
    from tiro import tts
    from tiro.audit import read_audit_entries
    from tiro.database import get_connection

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('Src', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES ('01BBBBBBBBBBBBBBBBBBBBBBBB', 1, 'T', 'sl', 'sl.md')"
    )
    conn.commit()
    conn.close()
    # Two paragraphs, each right under MAX_CHUNK_CHARS, so chunk_text() splits
    # this into two chunks — this pins that only the FIRST chunk's chars are
    # audited on a disconnect after chunk 1, not the whole article's chars.
    para1 = "First paragraph. " * 235  # ~4000 chars
    para2 = "Second paragraph. " * 235  # ~4000 chars
    body = f"{para1}\n\n{para2}"
    (config.articles_dir / "sl.md").write_text(f"---\ntitle: T\n---\n{body}")
    config.openai_api_key = "sk-fake"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"fake-mp3-bytes-that-are-not-really-audio")

    monkeypatch.setattr(
        tts, "_tts_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=120.0),
    )

    chunks = tts.chunk_text(f"T\n\n{body}")
    assert len(chunks) >= 2  # sanity: this article must actually span multiple chunks
    first_chunk_len = len(chunks[0])
    total_chars = sum(len(c) for c in chunks)
    assert first_chunk_len < total_chars

    agen = tts.stream_article_audio(1, config)
    first = await agen.__anext__()
    assert first  # got at least one chunk of bytes
    await agen.aclose()

    entries = read_audit_entries(config, service="openai_tts")
    disconnect_entries = [
        e for e in entries
        if e["endpoint"] == "stream" and e["success"] is False
        and "disconnect" in (e["error"] or "")
    ]
    assert disconnect_entries
    # Only the first chunk's characters were actually submitted to OpenAI
    # before the abort — the audited chars must reflect that, not the full
    # article's char count (which would overstate cost for the aborted call).
    assert disconnect_entries[0]["chars"] == first_chunk_len
    assert disconnect_entries[0]["chars"] < total_chars
    # The abandoned stream must not also report a fabricated success line.
    assert not any(e["endpoint"] == "speech" and e["success"] is True for e in entries)
