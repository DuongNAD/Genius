"""Real server boot smoke tests for serve.py.

Unlike test_serve.py (which mocks serve.start_server entirely), these tests
actually bind sockets:

* start_server() is run in-process as an asyncio task with its configured
  port already occupied -> it must fall back to a dynamic port, print the
  fallback warning, publish the real port in the (flat role -> port) service
  registry, answer GET /health over REAL localhost HTTP, and prune its
  registry entry on shutdown.
* serve.py is also booted as a REAL subprocess (`<python> serve.py --roles
  grok`, with PYTEST_CURRENT_TEST scrubbed from the env so it runs the
  production code paths) and polled until /health answers.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _occupy_port() -> socket.socket:
    """Bind and listen on an OS-assigned port, keeping it occupied.

    Bound on loopback: agent servers bind 127.0.0.1 by default now (see
    serve.bind_host), and on Windows a wildcard-bound blocker would not
    conflict with a specific-interface bind, so the fallback under test
    would never trigger.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


@pytest.mark.asyncio
async def test_start_server_port_fallback_health_and_registry_prune(
    tmp_path, monkeypatch, capsys
):
    import serve

    registry_path = tmp_path / "service_registry.json"
    monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", str(registry_path))

    blocker = _occupy_port()
    blocked_port = blocker.getsockname()[1]
    task = asyncio.create_task(serve.start_server("grok", blocked_port))
    try:
        # Wait for the fallback port to appear in the registry.
        dynamic_port = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if task.done():
                task.result()  # surfaces a startup crash immediately
                pytest.fail("start_server exited prematurely without error")
            if registry_path.exists():
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                if isinstance(registry.get("researcher"), int):
                    dynamic_port = registry["researcher"]
                    break
            await asyncio.sleep(0.1)

        assert dynamic_port is not None, "registry was never populated"
        assert dynamic_port != blocked_port
        # The registry stays a FLAT {"role": port} map.
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        assert registry == {"researcher": dynamic_port}

        # Real HTTP over localhost against the fallback port.
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    resp = await client.get(f"http://127.0.0.1:{dynamic_port}/health")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.2)
            assert resp is not None, "/health never became reachable"
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok", "role": "researcher"}
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=20)
        blocker.close()

    # The fallback warning was printed with enough detail to act on.
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "researcher" in out
    assert str(blocked_port) in out
    assert str(dynamic_port) in out

    # Clean shutdown pruned this role's entry from the registry.
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "researcher" not in registry


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def test_serve_subprocess_boots_grok_and_answers_health(tmp_path):
    """Boot `serve.py --roles grok` as a real production-mode subprocess."""
    registry_path = tmp_path / "service_registry.json"
    log_path = tmp_path / "serve.log"

    env = os.environ.copy()
    env["GENIUS_SERVICE_REGISTRY"] = str(registry_path)
    # Scrub pytest markers so the child runs the REAL (non-test) code paths.
    env.pop("PYTEST_CURRENT_TEST", None)

    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "serve.py", "--roles", "grok"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        # The server binds 8001 or, if that is taken on this machine, a
        # dynamic fallback; either way the registry publishes the real port.
        port = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log = log_path.read_text(encoding="utf-8", errors="replace")
                pytest.fail(
                    f"serve.py exited early (rc={proc.returncode}). Log:\n{log[-3000:]}"
                )
            if registry_path.exists():
                try:
                    registry = json.loads(registry_path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    registry = {}
                if isinstance(registry.get("researcher"), int):
                    port = registry["researcher"]
                    break
            time.sleep(0.25)
        assert port is not None, "registry never published the researcher port"

        healthy = False
        with httpx.Client(timeout=3.0) as client:
            while time.monotonic() < deadline:
                try:
                    resp = client.get(f"http://127.0.0.1:{port}/health")
                    if resp.status_code == 200:
                        assert resp.json() == {"status": "ok", "role": "researcher"}
                        healthy = True
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.5)
        if not healthy:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"/health never answered 200. Log:\n{log[-3000:]}")
    finally:
        _kill_tree(proc)
        log_file.close()
