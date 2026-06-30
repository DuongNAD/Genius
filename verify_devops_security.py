# verify_devops_security.py
import os
import sys
import time
import json
import hashlib
import asyncio
import threading
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import uvicorn

# Ensure the root directory is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# Load the actual apps
def get_security_app():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root_dir, ".agents", "skills", "security_agent", "api.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("security_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def get_devops_app():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root_dir, ".agents", "skills", "devops_agent", "api.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("devops_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


# Helper to compute SHA256 of bytes
def compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# Helper to build valid request headers
def build_auth_headers(api_key="mock-skill-key", expired=False, bad_signature=False):
    from ag_core.utils.jwt import encode_jwt

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


def test_agent_endpoints(app_getter, role_name, run_path, status_path_template):
    print(f"\n--- Testing {role_name.upper()} Agent Endpoints (using TestClient) ---")
    app = app_getter()
    client = TestClient(app)

    # 1. Missing Token -> 401
    resp = client.post(run_path, json={"prompt": "test"})
    print(f"Missing Token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401
    assert (
        "Missing token" in resp.json()["detail"]
        or "Not authenticated" in resp.json()["detail"]
    )

    # 2. Invalid Token -> 401
    headers_invalid = {
        "X-API-Key": "invalid-token",
        "Authorization": "Bearer invalid-token",
    }
    resp = client.post(run_path, json={"prompt": "test"}, headers=headers_invalid)
    print(f"Invalid Token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401

    # 3. Expired Token -> 401
    headers_expired = build_auth_headers(expired=True)
    resp = client.post(run_path, json={"prompt": "test"}, headers=headers_expired)
    print(f"Expired Token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401

    # 4. Bad Signature Token -> 401
    headers_bad_sig = build_auth_headers(bad_signature=True)
    resp = client.post(run_path, json={"prompt": "test"}, headers=headers_bad_sig)
    print(f"Bad Signature Token: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 401

    # 5. Missing X-Payload-SHA256 Header -> 400
    headers_valid_no_payload_sum = build_auth_headers()
    resp = client.post(
        run_path, json={"prompt": "test"}, headers=headers_valid_no_payload_sum
    )
    print(
        f"Missing X-Payload-SHA256 Header: Status = {resp.status_code}, Body = {resp.text}"
    )
    assert resp.status_code == 400
    assert "Missing X-Payload-SHA256 header" in resp.json()["detail"]

    # 6. Bad X-Payload-SHA256 Header -> 400
    headers_bad_sum = build_auth_headers()
    headers_bad_sum["X-Payload-SHA256"] = "wrongchecksum"
    resp = client.post(run_path, json={"prompt": "test"}, headers=headers_bad_sum)
    print(
        f"Bad X-Payload-SHA256 Header: Status = {resp.status_code}, Body = {resp.text}"
    )
    assert resp.status_code == 400
    assert "Checksum mismatch" in resp.json()["detail"]

    # 7. Correct headers, empty prompt -> 400/422
    body_empty = {"prompt": ""}
    payload_bytes_empty = json.dumps(body_empty, separators=(",", ":")).encode("utf-8")
    headers_empty = build_auth_headers()
    headers_empty["X-Payload-SHA256"] = compute_sha256(payload_bytes_empty)
    resp = client.post(run_path, content=payload_bytes_empty, headers=headers_empty)
    print(f"Empty prompt: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code in [400, 422]

    # 8. Correct headers, missing prompt field -> 422
    body_missing = {"context": {}}
    payload_bytes_missing = json.dumps(body_missing, separators=(",", ":")).encode(
        "utf-8"
    )
    headers_missing = build_auth_headers()
    headers_missing["X-Payload-SHA256"] = compute_sha256(payload_bytes_missing)
    resp = client.post(run_path, content=payload_bytes_missing, headers=headers_missing)
    print(f"Missing prompt field: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code in [400, 422]

    # 9. Correct headers, invalid JSON body -> 400/422
    payload_bytes_invalid_json = b"{invalid json"
    headers_invalid_json = build_auth_headers()
    headers_invalid_json["X-Payload-SHA256"] = compute_sha256(
        payload_bytes_invalid_json
    )
    resp = client.post(
        run_path, content=payload_bytes_invalid_json, headers=headers_invalid_json
    )
    print(f"Invalid JSON body: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code in [400, 422]

    # 10. Valid POST request -> 200, checks Response Headers
    body_valid = {
        "prompt": "audit project" if role_name == "security" else "deploy project"
    }
    payload_bytes_valid = json.dumps(body_valid, separators=(",", ":")).encode("utf-8")
    headers_valid = build_auth_headers()
    headers_valid["X-Payload-SHA256"] = compute_sha256(payload_bytes_valid)
    resp = client.post(run_path, content=payload_bytes_valid, headers=headers_valid)
    print(f"Valid POST: Status = {resp.status_code}, Body = {resp.text}")
    assert resp.status_code == 200
    assert "task_id" in resp.json()
    assert "X-Payload-SHA256" in resp.headers
    assert compute_sha256(resp.content) == resp.headers["X-Payload-SHA256"]

    task_id = resp.json()["task_id"]

    # 11. Valid GET status -> 200, checks Response Headers
    headers_get = build_auth_headers()
    headers_get["X-Payload-SHA256"] = compute_sha256(b"")
    resp_get = client.get(
        status_path_template.format(task_id=task_id), headers=headers_get
    )
    print(f"Valid GET status: Status = {resp_get.status_code}, Body = {resp_get.text}")
    assert resp_get.status_code == 200
    assert "status" in resp_get.json()
    assert "X-Payload-SHA256" in resp_get.headers
    assert compute_sha256(resp_get.content) == resp_get.headers["X-Payload-SHA256"]

    # 12. GET status with Task Not Found -> 404
    resp_get_404 = client.get(
        status_path_template.format(task_id="nonexistent-task-id"), headers=headers_get
    )
    print(
        f"GET status 404: Status = {resp_get_404.status_code}, Body = {resp_get_404.text}"
    )
    assert resp_get_404.status_code == 404

    print(f"{role_name.upper()} endpoint checks passed!")


def test_rate_limiter(app_getter, role_name):
    print(f"\n--- Testing {role_name.upper()} Agent Rate Limiting ---")
    app = app_getter()

    # Override/bypass the PYTEST bypass in rate limiter
    if "PYTEST_CURRENT_TEST" in os.environ:
        del os.environ["PYTEST_CURRENT_TEST"]
    os.environ["ENABLE_RATE_LIMITER"] = "1"

    from ag_core.utils.rate_limiter import limiter

    limiter.reset()

    client = TestClient(app)

    # Consume all tokens first
    for _ in range(10):
        limiter.consume(1.0)

    # Now next request should be rate limited
    body_valid = {"prompt": "test prompt"}
    payload_bytes_valid = json.dumps(body_valid, separators=(",", ":")).encode("utf-8")
    headers_valid = build_auth_headers()
    headers_valid["X-Payload-SHA256"] = compute_sha256(payload_bytes_valid)

    resp = client.post("/run", content=payload_bytes_valid, headers=headers_valid)
    print(
        f"Rate Limited Request: Status = {resp.status_code}, Body = {resp.text}, Headers = {resp.headers}"
    )
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "1"

    # Restore env vars
    os.environ["PYTEST_CURRENT_TEST"] = "true"
    limiter.reset()
    print(f"{role_name.upper()} Agent Rate Limiting check passed!")


def verify_slash_commands_and_routing():
    print("\n--- Verifying Slash Commands and Routing Configurations ---")
    import serve
    import orchestrator

    # Verify Routing table matches
    print("Checking ROUTING_TABLE definitions...")

    serve_expected_routing = {
        "/security": ("security", 8005),
        "/audit": ("security", 8005),
        "/security-audit": ("security", 8005),
        "/deploy": ("devops", 8006),
    }

    orchestrator_expected_routing = {
        "/security": ("security", "audit.md"),
        "/audit": ("security", "audit.md"),
        "/security-audit": ("security", "audit.md"),
        "/deploy": ("devops", "deploy.md"),
    }

    for cmd, route in serve_expected_routing.items():
        assert (
            serve.ROUTING_TABLE.get(cmd) == route
        ), f"serve.ROUTING_TABLE mismatch for command {cmd}: expected {route}, got {serve.ROUTING_TABLE.get(cmd)}"

    for cmd, route in orchestrator_expected_routing.items():
        assert (
            orchestrator.ROUTING_TABLE.get(cmd) == route
        ), f"orchestrator.ROUTING_TABLE mismatch for command {cmd}: expected {route}, got {orchestrator.ROUTING_TABLE.get(cmd)}"

    # Verify Role normalization
    print("Checking serve.normalize_roles normalization...")
    assert "security" in serve.normalize_roles("7")
    assert "security" in serve.normalize_roles("security")
    assert "security" in serve.normalize_roles("security_agent")
    assert "security" in serve.normalize_roles("security api")

    assert "devops" in serve.normalize_roles("8")
    assert "devops" in serve.normalize_roles("devops")
    assert "devops" in serve.normalize_roles("devops_agent")
    assert "devops" in serve.normalize_roles("devops api")

    print("Routing and normalization checks passed!")


def main():
    try:
        # 1. Test Security Agent Endpoints on Port 8005 paths
        # Note: API registers /run and /security/run, as well as /status/{task_id} and /security/status/{task_id}
        test_agent_endpoints(get_security_app, "security", "/run", "/status/{task_id}")
        test_agent_endpoints(
            get_security_app, "security", "/security/run", "/security/status/{task_id}"
        )

        # 2. Test DevOps Agent Endpoints on Port 8006 paths
        test_agent_endpoints(get_devops_app, "devops", "/run", "/status/{task_id}")
        test_agent_endpoints(
            get_devops_app, "devops", "/devops/run", "/devops/status/{task_id}"
        )

        # 3. Test Rate limiters
        test_rate_limiter(get_security_app, "security")
        test_rate_limiter(get_devops_app, "devops")

        # 4. Verify slash commands and routing
        verify_slash_commands_and_routing()

        print("\n=== ALL DEV OPS & SECURITY CHALLENGER TESTS PASSED SUCCESSFULLY! ===")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: Test execution failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
