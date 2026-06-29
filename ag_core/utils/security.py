import json
import hmac
import hashlib
import os
from typing import Any, Tuple, Union, Optional

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
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()

def verify_checksum(payload: Any, checksum: str, secret: str) -> bool:
    """
    Verify the checksum of a payload, supporting HMAC-SHA256 and falling back to
    plain SHA-256 with various serialization formats for backward compatibility.
    """
    if not checksum:
        return False
    
    # 1. Try HMAC-SHA256
    try:
        if calculate_checksum(payload, secret) == checksum:
            return True
    except Exception:
        pass
    
    # 2. Fall back to plain SHA-256
    if isinstance(payload, (bytes, str)):
        data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        return hashlib.sha256(data).hexdigest() == checksum
    
    # Try various JSON serialization formats for plain SHA-256
    # a. Canonical (sorted, no spaces)
    try:
        data_canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(data_canonical).hexdigest() == checksum:
            return True
    except Exception:
        pass
    
    # b. Spaced sort_keys (sorted, default separators)
    try:
        data_spaced_sorted = json.dumps(payload, sort_keys=True).encode("utf-8")
        if hashlib.sha256(data_spaced_sorted).hexdigest() == checksum:
            return True
    except Exception:
        pass
    
    # c. Un-sorted, no space
    try:
        data_unsorted_nospace = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(data_unsorted_nospace).hexdigest() == checksum:
            return True
    except Exception:
        pass
    
    # d. Un-sorted, spaced (default json.dumps)
    try:
        data_unsorted_spaced = json.dumps(payload).encode("utf-8")
        if hashlib.sha256(data_unsorted_spaced).hexdigest() == checksum:
            return True
    except Exception:
        pass
        
    return False

def verify_raw_body_checksum(body: bytes, checksum: str, secret: str) -> Tuple[bool, bool]:
    """
    Verify the checksum of raw request body bytes.
    Returns (is_valid, is_plain).
    """
    if not checksum:
        return False, False
    
    # 1. Try HMAC-SHA256
    try:
        computed_hmac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if computed_hmac == checksum:
            return True, False
    except Exception:
        pass
    
    # 2. Try plain SHA-256
    try:
        computed_plain = hashlib.sha256(body).hexdigest()
        if computed_plain == checksum:
            return True, True
    except Exception:
        pass
        
    return False, False

# Centralized Authentication and Checksum Middleware
from fastapi import Depends, HTTPException, Header, status, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response as FastAPIResponse
from fastapi.security import APIKeyHeader
from ag_core.utils.jwt import decode_jwt
from ag_core.config import load_config

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(
    x_api_key: Optional[str] = Depends(api_key_header),
    authorization: Optional[str] = Header(None, alias="Authorization")
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
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token"
        )
    config = load_config()
    expected_key = config.skill_api_key or os.getenv("SKILL_API_KEY", "mock-skill-key")
    try:
        payload = decode_jwt(token, expected_key)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {str(e)}"
        )
    return payload

async def checksum_middleware(request: Request, call_next):
    path = request.url.path
    config = load_config()
    expected_key = config.skill_api_key or os.getenv("SKILL_API_KEY", "mock-skill-key")
    request.state.use_plain_checksum = False

    if path.endswith("/run") or "/status" in path:
        x_payload = request.headers.get("X-Payload-SHA256")
        if not x_payload:
            content = {"detail": "Missing X-Payload-SHA256 header"}
            body_bytes = json.dumps(content).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return JSONResponse(
                status_code=400,
                content=content,
                headers={"X-Payload-SHA256": checksum}
            )
            
        body = await request.body()
        is_valid, is_plain = verify_raw_body_checksum(body, x_payload, expected_key)
        if not is_valid:
            content = {"detail": "Checksum mismatch"}
            body_bytes = json.dumps(content).encode("utf-8")
            checksum = hashlib.sha256(body_bytes).hexdigest()
            return JSONResponse(
                status_code=400,
                content=content,
                headers={"X-Payload-SHA256": checksum}
            )
        if is_plain:
            request.state.use_plain_checksum = True
            
    response = await call_next(request)
    
    response_body = b""
    async for chunk in response.body_iterator:
        response_body += chunk
        
    if getattr(request.state, "use_plain_checksum", False):
        checksum = hashlib.sha256(response_body).hexdigest()
    else:
        checksum = calculate_checksum(response_body, expected_key)
        
    response.headers["X-Payload-SHA256"] = checksum
    
    return FastAPIResponse(
        content=response_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type
    )

