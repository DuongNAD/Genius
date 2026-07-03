import base64
import json
import hmac
import hashlib
import os
import time


def jwt_max_lifetime() -> float:
    """Max allowed (exp - now) for an authenticating token, in seconds.

    Caps how far in the future a token may claim to be valid so an absurd or
    forever-lived credential is rejected at the auth boundary. Production
    tokens are minted with a 300s lifetime; the default 3600s leaves margin.
    """
    try:
        return float(os.environ.get("GENIUS_JWT_MAX_LIFETIME") or 3600.0)
    except (TypeError, ValueError):
        return 3600.0


def base64url_encode(data: bytes) -> str:
    """Encode bytes to base64url format string."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def base64url_decode(data_str: str) -> bytes:
    """Decode base64url format string to bytes."""
    rem = len(data_str) % 4
    if rem > 0:
        data_str += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data_str.encode("utf-8"))


import uuid

# Purge expired jtis from the replay table every N inserts so it doesn't grow
# without bound on a long-lived process. The insert path runs on the single DB
# writer thread (enqueue_db_write serializes it), so the plain counter is safe
# without a lock.
_JTI_PURGE_INTERVAL = 500
_jti_insert_count = 0


def encode_jwt(payload: dict, secret: str) -> str:
    """
    Encode a JWT token with HS256 algorithm.
    """
    if not secret:
        raise ValueError("JWT secret key must be non-empty")
    payload = dict(payload)
    if "jti" not in payload:
        payload["jti"] = str(uuid.uuid4())

    header = {"alg": "HS256", "typ": "JWT"}
    header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    header_b64 = base64url_encode(header_json)
    payload_b64 = base64url_encode(payload_json)

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    secret_bytes = secret if isinstance(secret, bytes) else secret.encode("utf-8")

    signature = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    signature_b64 = base64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_jwt(
    token: str,
    secret: str,
    *,
    require_exp: bool = False,
    max_lifetime: float = None,
) -> dict:
    """
    Decode and verify a JWT token with HS256 algorithm.
    Raises ValueError on any parsing, verification or expiration error.

    ``require_exp`` rejects a token with no ``exp`` claim (a forever-valid
    credential); ``max_lifetime`` rejects one whose ``exp`` is further than
    that many seconds in the future. Both default off so the general-purpose
    decode path is unchanged — auth entry points opt into the strict checks.
    """
    if not secret:
        raise ValueError("JWT secret key must be non-empty")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token format")

    header_b64, payload_b64, signature_b64 = parts

    try:
        header_json = base64url_decode(header_b64)
        header = json.loads(header_json)
    except Exception as e:
        raise ValueError("Invalid header") from e

    if header.get("alg") != "HS256":
        raise ValueError("Unsupported algorithm")

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    secret_bytes = secret if isinstance(secret, bytes) else secret.encode("utf-8")

    expected_signature = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    expected_signature_b64 = base64url_encode(expected_signature)

    if not hmac.compare_digest(
        signature_b64.encode("utf-8"), expected_signature_b64.encode("utf-8")
    ):
        raise ValueError("Invalid signature")

    try:
        payload_json = base64url_decode(payload_b64)
        payload = json.loads(payload_json)
    except Exception as e:
        raise ValueError("Invalid payload") from e

    if "exp" in payload:
        exp = payload["exp"]
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise ValueError("Invalid exp claim type")
        now = time.time()
        if now > exp:
            raise ValueError("Token has expired")
        if max_lifetime is not None and (exp - now) > max_lifetime:
            raise ValueError("Token lifetime exceeds maximum allowed")
    elif require_exp:
        raise ValueError("Token missing required exp claim")

    jti = payload.get("jti")
    if not jti:
        raise ValueError("Missing jti claim")

    from ag_core.utils.db import enqueue_db_write

    def _verify_and_save_jti_impl(conn, jti_str: str, exp_val: float | None):
        global _jti_insert_count
        if _jti_insert_count % _JTI_PURGE_INTERVAL == 0:
            now = time.time()
            conn.execute(
                "DELETE FROM seen_jtis WHERE exp IS NOT NULL AND ? > exp", (now,)
            )
            conn.commit()
        _jti_insert_count += 1

        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_jtis WHERE jti = ?", (jti_str,))
        if cursor.fetchone():
            raise ValueError("Token replay detected")

        conn.execute(
            "INSERT INTO seen_jtis (jti, exp) VALUES (?, ?)", (jti_str, exp_val)
        )
        conn.commit()

    try:
        enqueue_db_write(_verify_and_save_jti_impl, jti, payload.get("exp"))
    except ValueError as ve:
        raise ve
    except Exception as e:
        raise ValueError(f"Database error verifying token: {e}")

    return payload
