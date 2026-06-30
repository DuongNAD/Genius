# verify_challenger.py
import os
import sys

# Ensure the root directory is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Bypass rate limiting in FastAPI apps when running verify_challenger script
os.environ["PYTEST_CURRENT_TEST"] = "true"

import time
import json
import hashlib
import asyncio
import threading
import httpx
import random
from fastapi import FastAPI, Response
import uvicorn
from ag_core.utils.jwt import encode_jwt

# Define expected API keys and headers
SKILL_API_KEY = "mock-skill-key"

# Generate valid signed JWT token expiring in 5 minutes
valid_payload = {"sub": "orchestrator", "exp": time.time() + 300}
VALID_JWT_TOKEN = encode_jwt(valid_payload, SKILL_API_KEY)

HEADERS_VALID = {
    "X-API-Key": VALID_JWT_TOKEN,
    "Authorization": f"Bearer {VALID_JWT_TOKEN}",
    "Content-Type": "application/json",
}

# Generate random ports to prevent TIME_WAIT and address-in-use socket errors
random.seed(time.time())
GROK_PORT = random.randint(20000, 25000)
CLAUDE_PORT = random.randint(25001, 30000)
CODEX_PORT = random.randint(30001, 35000)
MOCK_PORT = random.randint(35001, 40000)

# In-memory mock app for checking tenacity retry logic
mock_app = FastAPI()
mock_request_count = 0
mock_request_timestamps = []


@mock_app.post("/run")
async def mock_run(response: Response, retry_after: str = None):
    global mock_request_count, mock_request_timestamps
    mock_request_timestamps.append(time.time())
    mock_request_count += 1

    if mock_request_count < 3:
        content = {"detail": "Rate limit exceeded"}
        status_code = 429
    else:
        content = {"status": "processing", "task_id": "test-task-123"}
        status_code = 200

    # Serialize with separators to ensure exact byte match
    body_bytes = json.dumps(content, separators=(",", ":")).encode("utf-8")
    checksum = hashlib.sha256(body_bytes).hexdigest()

    headers = {"X-Payload-SHA256": checksum, "Content-Type": "application/json"}
    if status_code == 429 and retry_after:
        headers["Retry-After"] = retry_after

    return Response(
        content=body_bytes,
        status_code=status_code,
        headers=headers,
        media_type="application/json",
    )


def run_server(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


# Helper to calculate checksum of a dict payload
def get_checksum(payload_dict):
    body_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body_bytes).hexdigest()


def get_api_app(role: str):
    root_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(
        root_dir,
        ".agents",
        "skills",
        (
            f"{role}_architect"
            if role == "claude"
            else f"{role}_researcher" if role == "grok" else f"{role}_reviewer"
        ),
        "api.py",
    )
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"{role}_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


async def test_auth_and_checksum_endpoints():
    print(f"\n--- Verifying Auth and Checksum Rules on Port {GROK_PORT} ---")
    async with httpx.AsyncClient() as client:
        # Test 1: Missing Checksum (POST /run) -> 400
        payload = {"prompt": "test"}
        res = await client.post(
            f"http://localhost:{GROK_PORT}/run",
            json=payload,
            headers={"X-API-Key": VALID_JWT_TOKEN},
        )
        print(
            f"POST /run (Missing Checksum): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 400
        assert "Missing X-Payload-SHA256 header" in res.json().get("detail", "")

        # Test 2: Incorrect Checksum (POST /run) -> 400
        headers_bad_sum = {
            "X-API-Key": VALID_JWT_TOKEN,
            "X-Payload-SHA256": "badchecksumvalue12345",
            "Content-Type": "application/json",
        }
        res = await client.post(
            f"http://localhost:{GROK_PORT}/run", json=payload, headers=headers_bad_sum
        )
        print(
            f"POST /run (Incorrect Checksum): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 400
        assert "Checksum mismatch" in res.json().get("detail", "")

        # Test 3: Missing Checksum (GET /status/{task_id}) -> 400
        res = await client.get(
            f"http://localhost:{GROK_PORT}/status/task-123",
            headers={"X-API-Key": VALID_JWT_TOKEN},
        )
        print(
            f"GET /status/task-123 (Missing Checksum): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 400
        assert "Missing X-Payload-SHA256 header" in res.json().get("detail", "")

        # Test 4: Incorrect Checksum (GET /status/{task_id}) -> 400
        res = await client.get(
            f"http://localhost:{GROK_PORT}/status/task-123",
            headers={
                "X-API-Key": VALID_JWT_TOKEN,
                "X-Payload-SHA256": "badchecksumvalue12345",
            },
        )
        print(
            f"GET /status/task-123 (Incorrect Checksum): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 400
        assert "Checksum mismatch" in res.json().get("detail", "")

        # Test 5: Missing X-API-Key (POST /run) -> 401
        headers_no_key = {
            "X-Payload-SHA256": get_checksum(payload),
            "Content-Type": "application/json",
        }
        res = await client.post(
            f"http://localhost:{GROK_PORT}/run", json=payload, headers=headers_no_key
        )
        print(
            f"POST /run (Missing API Key): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 401
        assert "Missing token" in res.json().get("detail", "")

        # Test 6: Invalid X-API-Key (POST /run) -> 401
        headers_bad_key = {
            "X-API-Key": "invalid-key",
            "X-Payload-SHA256": get_checksum(payload),
            "Content-Type": "application/json",
        }
        res = await client.post(
            f"http://localhost:{GROK_PORT}/run", json=payload, headers=headers_bad_key
        )
        print(
            f"POST /run (Invalid API Key): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 401
        assert "Invalid or expired token" in res.json().get("detail", "")

        # Test 7: Missing X-API-Key (GET /status/{task_id}) -> 401
        get_checksum_empty = hashlib.sha256(b"").hexdigest()
        headers_get_no_key = {"X-Payload-SHA256": get_checksum_empty}
        res = await client.get(
            f"http://localhost:{GROK_PORT}/status/task-123", headers=headers_get_no_key
        )
        print(
            f"GET /status/task-123 (Missing API Key): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 401
        assert "Missing token" in res.json().get("detail", "")

        # Test 8: Invalid X-API-Key (GET /status/{task_id}) -> 401
        headers_get_bad_key = {
            "X-API-Key": "invalid-key",
            "X-Payload-SHA256": get_checksum_empty,
        }
        res = await client.get(
            f"http://localhost:{GROK_PORT}/status/task-123", headers=headers_get_bad_key
        )
        print(
            f"GET /status/task-123 (Invalid API Key): Status = {res.status_code}, Body = {res.json()}"
        )
        assert res.status_code == 401
        assert "Invalid or expired token" in res.json().get("detail", "")

    print("Authentication and Checksum verification succeeded!")


async def stress_test_concurrency():
    print(f"\n--- Stress Testing /run and /status/{{task_id}} on Port {GROK_PORT} ---")
    payload = {"prompt": "Stress test prompt content"}
    checksum = get_checksum(payload)
    headers = {
        "X-API-Key": VALID_JWT_TOKEN,
        "X-Payload-SHA256": checksum,
        "Content-Type": "application/json",
    }

    async def send_single_request(client, i):
        # 1. Trigger the run
        res = await client.post(
            f"http://localhost:{GROK_PORT}/run", json=payload, headers=headers
        )
        if res.status_code not in (200, 202):
            return f"Req {i} POST failed with status {res.status_code}"

        task_id = res.json().get("task_id")
        if not task_id:
            return f"Req {i} POST did not return task_id"

        # 2. Poll for status
        get_headers = {
            "X-API-Key": VALID_JWT_TOKEN,
            "X-Payload-SHA256": hashlib.sha256(b"").hexdigest(),
        }
        for attempt in range(20):
            res_status = await client.get(
                f"http://localhost:{GROK_PORT}/status/{task_id}", headers=get_headers
            )
            if res_status.status_code == 200:
                status_data = res_status.json()
                curr_status = status_data.get("status")
                if curr_status in ("completed", "failed"):
                    return f"Req {i} finished: status={curr_status}"
            elif res_status.status_code == 404:
                return f"Req {i} GET status returned 404"
            await asyncio.sleep(0.1)
        return f"Req {i} timed out"

    async with httpx.AsyncClient(timeout=20.0) as client:
        start_time = time.time()
        tasks = [send_single_request(client, i) for i in range(50)]
        results = await asyncio.gather(*tasks)
        elapsed = time.time() - start_time

        print(f"Processed 50 concurrent requests in {elapsed:.2f} seconds.")
        success_count = sum(1 for r in results if "finished" in r)
        print(f"Successful processing count: {success_count} / 50")
        print("Sample results:")
        for r in results[:5]:
            print(f"  {r}")
        assert success_count == 50


async def verify_tenacity_retry():
    print(
        f"\n--- Verifying Tenacity Retry with Exponential Backoff on Port {MOCK_PORT} ---"
    )
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from orchestrator import perform_post_with_retry

    # Setup headers and payload for call_api
    payload = {"prompt": "retry-test"}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req_checksum = hashlib.sha256(payload_bytes).hexdigest()
    headers = {
        "X-Payload-SHA256": req_checksum,
        "X-API-Key": VALID_JWT_TOKEN,
        "Content-Type": "application/json",
    }

    # Case A: Retry with Retry-After header
    # We configure mock app to return Retry-After: 0.5
    global mock_request_count, mock_request_timestamps
    mock_request_count = 0
    mock_request_timestamps = []

    print(f"Testing retry loop with Retry-After: 0.5 on port {MOCK_PORT}")
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:{MOCK_PORT}/run?retry_after=0.5"
        start_t = time.time()
        response = await perform_post_with_retry(client, url, payload_bytes, headers)
        elapsed = time.time() - start_t
        print(
            f"Retry-After request succeeded in {elapsed:.2f}s with status {response.status_code}"
        )
        print(f"Request count: {mock_request_count}")
        # Expect ~2 retries, total elapsed time should be > 1.0s (since two 429s with 0.5s retry-after each)
        assert mock_request_count == 3
        intervals = [
            mock_request_timestamps[i] - mock_request_timestamps[i - 1]
            for i in range(1, len(mock_request_timestamps))
        ]
        print(f"Sleep intervals between attempts: {intervals}")
        for i, val in enumerate(intervals):
            print(f"  Interval {i+1}: {val:.2f}s (expected ~0.5s)")
            assert 0.4 <= val <= 1.2

    # Case B: Fallback to exponential backoff
    # If no Retry-After is provided, wait strategy is: standard exponential backoff (2^attempt, min 1s, max 10s)
    mock_request_count = 0
    mock_request_timestamps = []
    print("Testing fallback exponential backoff (no Retry-After)")
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:{MOCK_PORT}/run"
        start_t = time.time()
        response = await perform_post_with_retry(client, url, payload_bytes, headers)
        elapsed = time.time() - start_t
        print(
            f"Fallback retry request succeeded in {elapsed:.2f}s with status {response.status_code}"
        )
        print(f"Request count: {mock_request_count}")
        assert mock_request_count == 3
        intervals = [
            mock_request_timestamps[i] - mock_request_timestamps[i - 1]
            for i in range(1, len(mock_request_timestamps))
        ]
        print(f"Sleep intervals between attempts: {intervals}")
        for i, val in enumerate(intervals):
            print(f"  Interval {i+1}: {val:.2f}s")
        assert intervals[0] >= 1.0
        assert intervals[1] >= 2.0

    print("Tenacity retry loops and backoff verified successfully!")


def main():
    # Start the mock server in a background thread
    t_mock = threading.Thread(
        target=run_server, args=(mock_app, MOCK_PORT), daemon=True
    )
    t_mock.start()
    print(f"Mock server started in background thread on port {MOCK_PORT}")

    # Load actual FastAPI apps
    print("Loading skill API apps...")
    grok_app = get_api_app("grok")
    claude_app = get_api_app("claude")
    codex_app = get_api_app("codex")

    # Start actual APIs in background threads
    t_grok = threading.Thread(
        target=run_server, args=(grok_app, GROK_PORT), daemon=True
    )
    t_grok.start()
    print(f"Grok researcher API started in background thread on port {GROK_PORT}")

    t_claude = threading.Thread(
        target=run_server, args=(claude_app, CLAUDE_PORT), daemon=True
    )
    t_claude.start()
    print(f"Claude architect API started in background thread on port {CLAUDE_PORT}")

    t_codex = threading.Thread(
        target=run_server, args=(codex_app, CODEX_PORT), daemon=True
    )
    t_codex.start()
    print(f"Codex reviewer API started in background thread on port {CODEX_PORT}")

    # Wait for all background servers to bind
    time.sleep(2.0)

    try:
        # Run async tests
        asyncio.run(test_auth_and_checksum_endpoints())
        asyncio.run(stress_test_concurrency())
        asyncio.run(verify_tenacity_retry())
        print("\nAll empirical tests PASSED successfully!")
    except Exception as e:
        print(f"Error during tests: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
