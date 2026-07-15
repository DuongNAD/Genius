"""BodySizeLimitMiddleware: the ASGI-level request-body cap (P2 fix).

checksum_middleware's Content-Length fast-reject only covers honest clients —
a chunked (or length-less) POST used to stream its whole body into
``await request.body()`` / ``request.json()`` BEFORE authentication, so an
attacker without SKILL_API_KEY could exhaust RAM. The ASGI wrapper counts the
bytes actually arriving on the receive channel and answers 413 the moment the
running total passes GENIUS_MAX_REQUEST_BYTES, on both the skill servers and
the central hub's catch-all POST.

httpx sends ``content=<async generator>`` as a length-less chunked stream —
exactly the shape the old header-only check could not see.
"""

import httpx
import pytest

from ag_core.skill_app import create_skill_app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def _chunks(total_bytes: int, chunk: int = 512):
    sent = 0
    while sent < total_bytes:
        n = min(chunk, total_bytes - sent)
        yield b"x" * n
        sent += n


@pytest.mark.asyncio
async def test_skill_chunked_body_over_limit_is_413(monkeypatch):
    monkeypatch.setenv("GENIUS_MAX_REQUEST_BYTES", "1000")
    app = create_skill_app("researcher")
    async with _client(app) as client:
        res = await client.post(
            "/run",
            content=_chunks(8_000),
            headers={
                "Content-Type": "application/json",
                "X-Payload-SHA256": "junk",
            },
        )
    assert res.status_code == 413
    assert res.json() == {"detail": "Request body too large"}
    assert "X-Payload-SHA256" in res.headers


@pytest.mark.asyncio
async def test_skill_chunked_body_under_limit_still_flows(monkeypatch):
    # The counter must pass a small stream through untouched: the request
    # reaches the checksum middleware and dies on ITS verdict (mismatch),
    # not on a size error.
    monkeypatch.setenv("GENIUS_MAX_REQUEST_BYTES", "100000")
    app = create_skill_app("researcher")
    async with _client(app) as client:
        res = await client.post(
            "/run",
            content=_chunks(600),
            headers={
                "Content-Type": "application/json",
                "X-Payload-SHA256": "junk",
            },
        )
    assert res.status_code == 400
    assert res.json() == {"detail": "Checksum mismatch"}


@pytest.mark.asyncio
async def test_skill_status_path_also_capped(monkeypatch):
    monkeypatch.setenv("GENIUS_MAX_REQUEST_BYTES", "1000")
    app = create_skill_app("researcher")
    async with _client(app) as client:
        res = await client.post(
            "/status/abc",
            content=_chunks(5_000),
            headers={"X-Payload-SHA256": "junk"},
        )
    # 413 from the byte counter — or 405 would mean the route rejected the
    # method BEFORE the body was read, which never happens for POST bodies:
    # the middleware sits outside routing.
    assert res.status_code == 413


@pytest.mark.asyncio
async def test_hub_chunked_body_over_limit_is_413(monkeypatch):
    monkeypatch.setenv("GENIUS_MAX_REQUEST_BYTES", "1000")
    import serve

    async with _client(serve.app) as client:
        res = await client.post(
            "/tasks/dispatch",
            content=_chunks(8_000),
            headers={"Content-Type": "application/json"},
        )
    assert res.status_code == 413
    assert res.json() == {"detail": "Request body too large"}
