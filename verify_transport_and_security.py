import os
import sys
import time
import json
import hashlib
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serve import app
import serve
from ag_core.utils.jwt import encode_jwt


def compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_auth_headers(api_key="mock-skill-key", expired=False, bad_signature=False):
    payload = {
        "sub": "orchestrator",
        "exp": time.time() - 10 if expired else time.time() + 300,
    }
    secret = "wrong-secret" if bad_signature else api_key
    jwt_token = encode_jwt(payload, secret)
    return {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }


def main():
    print("======================================================================")
    print("RUNNING TRANSPORT AND SECURITY CORRECTNESS VERIFICATION")
    print("======================================================================")

    # ------------------------------------------------------------------------
    # 1. VERIFY WEBSOCKET IS DISABLED IN LOCAL MODE (WITHOUT --distributed)
    # ------------------------------------------------------------------------
    print("\n1. Verifying WebSocket connect behavior in local mode:")
    # Ensure IS_DISTRIBUTED is False and not in pytest
    serve.IS_DISTRIBUTED = False
    if "PYTEST_CURRENT_TEST" in os.environ:
        del os.environ["PYTEST_CURRENT_TEST"]

    client = TestClient(app)

    # Connect to websocket, it should return 404 (FastAPI raises HTTPException 404)
    # TestClient raises WebSocketDisconnect or HTTPException depending on how Starlette handles 404
    try:
        token = encode_jwt(
            {"sub": "worker-1", "exp": time.time() + 60}, "mock-skill-key"
        )
        with client.websocket_connect(f"/ws/connect?token={token}") as websocket:
            print("FAILED: WebSocket connection succeeded but should have failed!")
            sys.exit(1)
    except Exception as e:
        # Starlette raises a WebSocketDisconnect or an HTTP exception for 404
        # Let's inspect the error / status code
        print(f"PASSED: WebSocket connection blocked. Error: {e}")

    # ------------------------------------------------------------------------
    # 2. VERIFY JWT VALIDATION (HTTP 401 ON INVALID/MISSING TOKENS)
    # ------------------------------------------------------------------------
    print("\n2. Verifying JWT validation rules (HTTP 401):")
    # We use security app from skills
    from test_e2e_phase5 import get_security_agent_app

    sec_app = get_security_agent_app()
    sec_client = TestClient(sec_app)

    # Payload
    payload = {"prompt": "audit this code"}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    checksum = compute_sha256(payload_bytes)

    # Case A: Missing token (with valid checksum)
    headers = {"X-Payload-SHA256": checksum, "Content-Type": "application/json"}
    resp = sec_client.post("/run", content=payload_bytes, headers=headers)
    print(f"A. Missing token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    # Case B: Invalid/malformed token
    headers = {
        "X-API-Key": "invalid.jwt.token",
        "X-Payload-SHA256": checksum,
        "Content-Type": "application/json",
    }
    resp = sec_client.post("/run", content=payload_bytes, headers=headers)
    print(f"B. Malformed token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    # Case C: Wrong signature
    wrong_headers = build_auth_headers(bad_signature=True)
    wrong_headers["X-Payload-SHA256"] = checksum
    resp = sec_client.post("/run", content=payload_bytes, headers=wrong_headers)
    print(f"C. Wrong signature: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    # Case D: Expired token
    expired_headers = build_auth_headers(expired=True)
    expired_headers["X-Payload-SHA256"] = checksum
    resp = sec_client.post("/run", content=payload_bytes, headers=expired_headers)
    print(f"D. Expired token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    print("JWT validation checks PASSED.")

    # ------------------------------------------------------------------------
    # 3. VERIFY PAYLOAD CHECKSUMS (HTTP 400 ON MISSING/MISMATCHED CHECKSUM)
    # ------------------------------------------------------------------------
    print("\n3. Verifying payload checksum rules (HTTP 400):")

    # Case A: Missing checksum header
    valid_headers_no_checksum = build_auth_headers()
    resp = sec_client.post(
        "/run", content=payload_bytes, headers=valid_headers_no_checksum
    )
    print(
        f"A. Missing checksum header: Status = {resp.status_code}, Body = {resp.text}"
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
    assert "Missing X-Payload-SHA256 header" in resp.json()["detail"]

    # Case B: Mismatched checksum value
    valid_headers_bad_checksum = build_auth_headers()
    valid_headers_bad_checksum["X-Payload-SHA256"] = "wrong-checksum-value"
    resp = sec_client.post(
        "/run", content=payload_bytes, headers=valid_headers_bad_checksum
    )
    print(f"B. Mismatched checksum: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
    assert "Checksum mismatch" in resp.json()["detail"]

    print("Payload checksum validation checks PASSED.")
    print("\n======================================================================")
    print("ALL TESTS COMPLETED SUCCESSFULLY!")
    print("======================================================================")


if __name__ == "__main__":
    main()
