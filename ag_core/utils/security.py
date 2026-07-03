import json
import hmac
import hashlib
import os
from typing import Any, Tuple, Optional


def calculate_checksum(payload: Any, secret: str) -> str:
    """
    Calculate the HMAC-SHA256 checksum of a payload.
    If the payload is bytes, it is used directly.
    If it is a string, it is encoded to UTF-8.
    Otherwise, it is serialized to JSON using canonical representation:
    sorted keys, no spaces around separators, and encoded to UTF-8.
    """
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def verify_checksum(payload: Any, checksum: str, secret: str) -> bool:
    """
    Verify the checksum of a payload, supporting only HMAC-SHA256.
    Returns False if secret is missing or empty.
    """
    if not secret:
        return False
    if not checksum:
        return False

    return hmac.compare_digest(calculate_checksum(payload, secret), checksum)


def verify_raw_body_checksum(
    body: bytes, checksum: str, secret: str
) -> Tuple[bool, bool]:
    """
    Verify the checksum of raw request body bytes.
    Returns (is_valid, is_plain).
    """
    if not secret:
        return False, False
    if not checksum:
        return False, False

    # 1. Try HMAC-SHA256
    try:
        computed_hmac = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(computed_hmac, checksum):
            return True, False
    except Exception:
        pass

    return False, False


# Centralized Authentication and Checksum Middleware
from fastapi import Depends, HTTPException, Header, status, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response as FastAPIResponse
from fastapi.security import APIKeyHeader
from ag_core.utils.jwt import decode_jwt, jwt_max_lifetime
from ag_core.config import load_config

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(
    x_api_key: Optional[str] = Depends(api_key_header),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> dict:
    token = None
    if x_api_key:
        token = x_api_key
    elif authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token"
        )
    config = load_config()
    expected_key = config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    try:
        payload = decode_jwt(
            token,
            expected_key,
            require_exp=True,
            max_lifetime=jwt_max_lifetime(),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {str(e)}",
        )
    return payload


def _max_request_bytes() -> int:
    """Max accepted request-body size for /run and /status. Bounds a pre-auth
    memory-exhaustion DoS; a skill payload (prompt + scanned context) is small
    relative to this. Tunable via GENIUS_MAX_REQUEST_BYTES; blank/junk -> 25 MiB
    (matches the response cap)."""
    try:
        val = int(os.getenv("GENIUS_MAX_REQUEST_BYTES") or 25 * 1024 * 1024)
        return val if val > 0 else 25 * 1024 * 1024
    except (TypeError, ValueError):
        return 25 * 1024 * 1024


async def checksum_middleware(request: Request, call_next):
    path = request.url.path
    config = load_config()
    expected_key = config.skill_api_key or os.getenv("SKILL_API_KEY", "")
    request.state.use_plain_checksum = False

    if path.endswith("/run") or "/status" in path:
        # Reject oversized bodies BEFORE buffering them: verify_raw_body_checksum
        # below reads the entire body into memory, so without this an attacker
        # who doesn't even know SKILL_API_KEY could POST a multi-GB body and
        # exhaust RAM before the checksum check fails. A missing/chunked
        # Content-Length still buffers, but the honest-and-huge case (the
        # practical DoS) is cut off cheaply here.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > _max_request_bytes()
            except (TypeError, ValueError):
                too_large = False
            if too_large:
                content = {"detail": "Request body too large"}
                body_bytes = json.dumps(content).encode("utf-8")
                checksum = hashlib.sha256(body_bytes).hexdigest()
                return JSONResponse(
                    status_code=413,
                    content=content,
                    headers={"X-Payload-SHA256": checksum},
                )

        x_payload = request.headers.get("X-Payload-SHA256")
        if not x_payload:
            content = {"detail": "Missing X-Payload-SHA256 header"}
            body_bytes = json.dumps(content).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return JSONResponse(
                status_code=400, content=content, headers={"X-Payload-SHA256": checksum}
            )

        body = await request.body()
        is_valid, is_plain = verify_raw_body_checksum(body, x_payload, expected_key)
        if not is_valid:
            content = {"detail": "Checksum mismatch"}
            body_bytes = json.dumps(content).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return JSONResponse(
                status_code=400, content=content, headers={"X-Payload-SHA256": checksum}
            )
        if is_plain:
            request.state.use_plain_checksum = True

    response = await call_next(request)

    # Buffer the response to compute its checksum, but bound the buffer so a
    # pathologically large response can't exhaust memory. Skill endpoints
    # return small JSON, so this only trips on a runaway payload.
    try:
        max_bytes = int(os.getenv("GENIUS_MAX_RESPONSE_BYTES") or 25 * 1024 * 1024)
    except (TypeError, ValueError):
        max_bytes = 25 * 1024 * 1024
    response_body = b""
    async for chunk in response.body_iterator:
        response_body += chunk
        if len(response_body) > max_bytes:
            content = {"detail": "Response too large to checksum"}
            body_bytes = json.dumps(content).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return JSONResponse(
                status_code=500,
                content=content,
                headers={"X-Payload-SHA256": checksum},
            )

    if getattr(request.state, "use_plain_checksum", False):
        checksum = hashlib.sha256(response_body).hexdigest()
    else:
        checksum = calculate_checksum(response_body, expected_key)

    response.headers["X-Payload-SHA256"] = checksum

    return FastAPIResponse(
        content=response_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
