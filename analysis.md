# FastAPI and Uvicorn Server Lifecycle Stability Analysis

This report documents the assessment of the FastAPI and Uvicorn server lifecycle stability in Genius, focusing on `tests/test_distributed.py` and `test_milestone1_distributed.py`, while extending to related test suites (`tests/test_distributed_milestone2.py`, `tests/test_milestone3_adversarial_challenger.py`, etc.) and the core `serve.py` orchestration.

---

## 1. Executive Summary
* **Test Isolation**: `tests/test_distributed.py` uses a purely mock network environment (`MockNetworkProtocol`), which successfully isolates tests from TCP port binding and WebSocket resource exhaustion risks. `test_milestone1_distributed.py` uses Starlette's `TestClient` to perform in-memory ASGI calls without binding to a physical port.
* **Core Vulnerabilities Identified**:
  1. **Port Reuse Race Conditions (TOCTOU)**: The helper function `get_free_port()` used in multiple test files contains a Time-of-Check to Time-of-Use race condition.
  2. **Orphaned Background Tasks (Async Resource Leaks)**: Disconnects in `ClientWorker` cancel the main communication loops but leave task executions (`execute_task`) running as orphaned tasks in the event loop. In addition, `CentralHub` sweepers are left dangling in several test suites because `stop_sweeper()` is not called on teardown.
  3. **Unclosed WebSockets on Test Failure**: Live WebSocket integration tests in `test_milestone3_adversarial_challenger.py` manually manage websockets without context managers, risking unclosed sockets and Uvicorn hangs if an assertion fails.
  4. **Infinite Hang in serve.py CLI**: Running `serve.py` interactively starts the background servers but never stops them, hanging the CLI indefinitely.

---

## 2. Port Reuse Conflicts
Across the distributed test suite (specifically in `tests/test_distributed_milestone2.py`, `tests/test_milestone3_adversarial_challenger.py`, `tests/test_milestone3_challenger.py`, etc.), the helper function `get_free_port()` is defined as follows:

```python
def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port
```

### Risks:
* **TOCTOU Race Condition**: After `s.close()` releases the port, there is a time window before the Uvicorn server binds to that port. If tests are run concurrently (e.g., via `pytest -n auto`), another worker can fetch the same port, causing Uvicorn to crash with `OSError: [Errno 98] Address already in use` when it attempts to start.
* **Fixed Port Collision**: `serve.py` maps specific microservice roles to static ports (e.g., `8001` for Grok, `8002` for Claude, etc.). If these ports are already in use on the host system, launching microservices will fail immediately.

---

## 3. Hung Servers & CLI Hangs
Uvicorn is designed to perform a graceful shutdown by waiting for all active connections and request handlers to terminate before exiting.

### Risks:
* **Interactive CLI Hang**: In `serve.py`, the CLI starts servers in background asyncio tasks. If a user runs the orchestrator interactively (by typing the prompt rather than passing `--prompt`), the CLI does not cancel the server tasks on completion, hanging the terminal indefinitely at `await asyncio.gather(*server_tasks)`.
* **Teardown Blockage**: Real test servers are terminated by setting `server.should_exit = True` and awaiting the server task. If a client connection (like a WebSocket) is kept alive or a task hangs, Uvicorn will block during graceful shutdown. If no timeout is enforced on the server task await, the entire pytest suite hangs.

---

## 4. Unclosed WebSockets
Starlette's `TestClient.websocket_connect` context manager automatically handles websocket closures. However, live integration tests (e.g., `tests/test_milestone3_adversarial_challenger.py`) create WebSockets manually:

```python
ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/connect?token={token}")
# ...
await asyncio.gather(*(ws.close() for ws, w_id in workers_connections))
```

### Risks:
* **Leakage on Assertion Failure**: If any exception or assertion failure occurs before `ws.close()` is reached, the `finally` block runs, setting `server.should_exit = True` without closing the WebSocket connections. The server will hang waiting for the client websockets to close, delaying teardown or hanging the test process.

---

## 5. Async Resource Leaks
We identified multiple vectors of background task and memory accumulation:

### A. CentralHub Sweeper Task Leak
When `CentralHub.set_network` is invoked, it launches `self.start_sweeper()`, which spawns a background asyncio task checking liveness every 10ms:
```python
self._sweeper_task = asyncio.create_task(self._sweeper_loop())
```
In `tests/test_milestone3_adversarial_challenger.py`, several tests (e.g. `test_concurrent_worker_disconnections_mocked`, `test_concurrent_cancellations`) instantiate `CentralHub` and call `set_network` but **never call `stop_sweeper()`**. These background tasks continue running in the event loop indefinitely, causing async task pollution.

### B. Worker Task Orphanage
In `ag_core/distributed/worker.py`, when a worker's `run_production_loop` is cancelled, the `finally` block cancels only the communication tasks:
```python
finally:
    self.running = False
    hb_task.cancel()
    read_task.cancel()
    await asyncio.gather(hb_task, read_task, return_exceptions=True)
    self.ws = None
```
However, the worker does **not** cancel or await the tasks registered in `self.running_tasks` (spawns of `execute_task`). These tasks continue running in the background loop despite the worker loop being cancelled and disconnected, causing orphaned tasks to leak memory/CPU.

### C. Heartbeat Loop Leakage on Test Failure
In `tests/test_distributed.py`, tests that start the periodic worker heartbeat task (`w.start_heartbeats()`) clean them up via `w.stop_heartbeats()` at the end of the test body. If an assertion fails, `stop_heartbeats()` is skipped, leaving the worker's heartbeat loop active in the background.

### D. Global pending_tasks Memory Leak
`pending_tasks` is a global dictionary in `serve.py`. In `test_live_websocket_concurrency_and_disconnects` in `tests/test_milestone3_adversarial_challenger.py`, task futures are inserted manually:
```python
pending_tasks[t_id] = fut
```
If the test fails, these futures are never deleted, leaking reference counts and holding memory across pytest executions.

---

## 6. Recommendations
1. **Use Port 0 for Real Test Servers**: In tests, configure Uvicorn to bind to `port=0` (ephemeral port chosen by the OS). After startup, read the actual bound port from `server.servers[0].sockets[0].getsockname()[1]` to avoid TOCTOU races.
2. **Robust Task and Socket Cleanup via Fixtures**: Wrap all worker registration and websocket connections in `pytest` fixtures or `try/finally` blocks to guarantee that `stop_heartbeats()`, `ws.close()`, and `pending_tasks.pop()` are always executed even on test failure.
3. **Cancel Running Tasks on Worker Shutdown**: Update `ClientWorker`'s shutdown logic to cancel all tasks in `self.running_tasks` before discarding the connection.
4. **Ensure sweeper stop is always called**: Bind `CentralHub` lifespan to a fixture that guarantees `stop_sweeper()` is called during cleanup.
