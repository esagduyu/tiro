"""Fold-in 1a (T2 fable review): the shared page-extraction fetch is byte-capped.

Per-entry RSS page re-fetch was uncapped; both `fetch_and_extract` (async) and
`fetch_and_extract_sync` (sync) now stream under `MAX_PAGE_BYTES` and raise
`PageTooLarge` mid-stream, mirroring `_fetch_feed`'s 10 MB pattern.

Offline: an `httpx.MockTransport` is injected by patching the client
constructor the web module reaches through.
"""

import httpx
import pytest

from tiro.ingestion import web

_REAL_ASYNC = httpx.AsyncClient
_REAL_SYNC = httpx.Client


def _oversized_body() -> bytes:
    return b"<html><body>" + b"x" * (web.MAX_PAGE_BYTES + 1024) + b"</body></html>"


def _small_body() -> bytes:
    return b"<html><head><title>Small</title></head><body><p>hello world</p></body></html>"


def _patch_clients(monkeypatch, body: bytes):
    handler = lambda req: httpx.Response(200, content=body)  # noqa: E731

    def async_factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC(transport=httpx.MockTransport(handler), **kwargs)

    def sync_factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_SYNC(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", async_factory)
    monkeypatch.setattr(web.httpx, "Client", sync_factory)


def test_max_page_bytes_constant_is_sane():
    assert web.MAX_PAGE_BYTES == 5 * 1024 * 1024


def test_sync_fetch_raises_on_oversized(monkeypatch):
    _patch_clients(monkeypatch, _oversized_body())
    with pytest.raises(web.PageTooLarge):
        web.fetch_and_extract_sync("https://example.com/huge")


def test_sync_fetch_ok_under_cap(monkeypatch):
    _patch_clients(monkeypatch, _small_body())
    out = web.fetch_and_extract_sync("https://example.com/small")
    assert "hello world" in out["content_md"]


async def test_async_fetch_raises_on_oversized(monkeypatch):
    _patch_clients(monkeypatch, _oversized_body())
    with pytest.raises(web.PageTooLarge):
        await web.fetch_and_extract("https://example.com/huge")
