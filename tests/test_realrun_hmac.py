"""Real HMAC loopback: orchestrator-signed requests against a real skill app.

conftest.py normally patches verify_checksum/verify_raw_body_checksum to also
accept plain SHA-256 (legacy tests); this file is registered in conftest's
strict opt-out (like test_upgrades), so every assertion here runs against the
production HMAC-only crypto in ag_core/utils/security.py.

Nothing on the server side is mocked except the agent's run() coroutine (a
canned result): the checksum middleware, JWT auth dependency, routes and
response checksumming are the production code, exercised over a real ASGI
transport. The client side uses orchestrator.call_api itself (full path:
canonical payload serialization, HMAC X-Payload-SHA256, JWT X-API-Key /
Authorization headers, /run + /status polling, response checksum verify).
"""

import hashlib
import json
import os
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import orchestrator
from ag_core.skill_app import create_skill_app
from ag_core.utils import security
from ag_core.utils.jwt import encode_jwt

AGENT_RUN = "ag_core.agents.grok_researcher.GrokResearcherAgent.run"
CANNED_RESULT = "canned grok research result"


def _secret() -> str:
    return os.environ["SKILL_API_KEY"]


def _client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _signed_headers(payload: dict, secret: str, *, jwt_secret: str = None) -> dict:
    """Sign a /run request exactly like orchestrator.call_api does."""
    token = encode_jwt(
        {"sub": "orchestrator", "exp": time.time() + 300}, jwt_secret or secret
    )
    return {
        "X-API-Key": token,
        "Authorization": f"Bearer {token}",
        "X-Payload-SHA256": security.calculate_checksum(payload, secret),
        "Content-Type": "application/json",
    }


def _canonical_bytes(payload: dict) -> bytes:
    # call_api's canonicalization: sorted keys, compact separators, UTF-8.
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.mark.asyncio
async def test_call_api_full_hmac_loopback_roundtrip():
    """orchestrator.call_api -> ASGI -> middleware -> JWT auth -> agent ->
    poll /status -> response HMAC verified: the whole client path for real."""
    app = create_skill_app("grok")
    with patch(AGENT_RUN, new=AsyncMock(return_value=CANNED_RESULT)):
        async with _client_for(app) as client:
            result = await orchestrator.call_api(
                "http://testserver",
                _secret(),
                "ping the researcher",
                client=client,
                poll_timeout=30.0,
            )
    assert result == CANNED_RESULT


@pytest.mark.asyncio
async def test_orchestrator_style_signing_accepted_and_response_hmac_verifies():
    app = create_skill_app("grok")
    secret = _secret()
    payload = {"prompt": "ping", "context": None}
    with patch(AGENT_RUN, new=AsyncMock(return_value=CANNED_RESULT)):
        async with _client_for(app) as client:
            resp = await client.post(
                "/run",
                content=_canonical_bytes(payload),
                headers=_signed_headers(payload, secret),
            )

    assert resp.status_code == 200
    assert "task_id" in resp.json()

    # The response is integrity-protected with a real HMAC checksum...
    resp_checksum = resp.headers.get("X-Payload-SHA256")
    assert resp_checksum, "response is missing the X-Payload-SHA256 header"
    assert security.verify_checksum(resp.content, resp_checksum, secret)
    # ...and it is genuinely keyed (not a plain SHA-256 of the body).
    assert resp_checksum != hashlib.sha256(resp.content).hexdigest()
    # A different secret must fail verification.
    assert not security.verify_checksum(resp.content, resp_checksum, "other-secret")


@pytest.mark.asyncio
async def test_plain_sha256_checksum_is_rejected():
    """A valid JWT but a plain (unkeyed) SHA-256 body checksum must be
    rejected by the production HMAC-only middleware."""
    app = create_skill_app("grok")
    secret = _secret()
    payload = {"prompt": "ping", "context": None}
    body = _canonical_bytes(payload)
    headers = _signed_headers(payload, secret)
    headers["X-Payload-SHA256"] = hashlib.sha256(body).hexdigest()

    async with _client_for(app) as client:
        resp = await client.post("/run", content=body, headers=headers)

    assert resp.status_code == 400
    assert "checksum" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_wrong_skill_api_key_is_rejected():
    """A JWT signed with the wrong SKILL_API_KEY fails auth (the body checksum
    is valid, isolating the JWT check)."""
    app = create_skill_app("grok")
    secret = _secret()
    payload = {"prompt": "ping", "context": None}
    headers = _signed_headers(payload, secret, jwt_secret="wrong-skill-api-key")

    async with _client_for(app) as client:
        resp = await client.post(
            "/run", content=_canonical_bytes(payload), headers=headers
        )

    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_tampered_body_after_signing_is_rejected():
    app = create_skill_app("grok")
    secret = _secret()
    payload = {"prompt": "ping", "context": None}
    headers = _signed_headers(payload, secret)
    tampered = _canonical_bytes({"prompt": "evil injected prompt", "context": None})

    async with _client_for(app) as client:
        resp = await client.post("/run", content=tampered, headers=headers)

    assert resp.status_code == 400
    assert "checksum" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_checksum_header_is_rejected():
    app = create_skill_app("grok")
    secret = _secret()
    payload = {"prompt": "ping", "context": None}
    headers = _signed_headers(payload, secret)
    del headers["X-Payload-SHA256"]

    async with _client_for(app) as client:
        resp = await client.post(
            "/run", content=_canonical_bytes(payload), headers=headers
        )

    assert resp.status_code == 400
    assert "X-Payload-SHA256" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_status_polling_requires_hmac_signature_too():
    """GET /status is checksum-protected as well: call_api signs it with the
    HMAC of the empty body; a plain SHA-256 of b'' must be rejected."""
    app = create_skill_app("grok")
    secret = _secret()
    token = encode_jwt({"sub": "orchestrator", "exp": time.time() + 300}, secret)

    async with _client_for(app) as client:
        # HMAC of the empty body (what call_api sends) -> accepted (404: the
        # task id does not exist, which proves we got past the middleware).
        good = await client.get(
            "/status/nonexistent-task",
            headers={
                "X-API-Key": token,
                "Authorization": f"Bearer {token}",
                "X-Payload-SHA256": security.calculate_checksum(b"", secret),
            },
        )
        assert good.status_code == 404

        token2 = encode_jwt({"sub": "orchestrator", "exp": time.time() + 300}, secret)
        bad = await client.get(
            "/status/nonexistent-task",
            headers={
                "X-API-Key": token2,
                "Authorization": f"Bearer {token2}",
                "X-Payload-SHA256": hashlib.sha256(b"").hexdigest(),
            },
        )
        assert bad.status_code == 400
