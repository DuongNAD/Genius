"""MCP stdio transport smoke test: a REAL `mcp_server.py stdio` subprocess.

Drives the JSON-RPC handshake over real OS pipes (initialize ->
notifications/initialized -> tools/list -> BOM-prefixed ping ->
resources/list -> resources/read) and asserts the stdout stream is pure
JSON-RPC: exactly one parseable response line per request, correct ids, all
documented tools, artifact resources served from the server cwd, and no
log noise corrupting the stream. PYTEST_CURRENT_TEST is scrubbed from the
child env so it runs the production code paths. The server runs in a tmp
workspace dir so the artifact resource list is deterministic.
"""

import json
import os
import queue
import subprocess
import sys
import threading

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_TOOLS = {
    "research",
    "design",
    "code",
    "unit_test",
    "security_audit",
    "deploy",
    "orchestrate",
    "orchestrate_approve",
    "orchestrate_reject",
    "orchestrate_status",
    "doctor",
    "debate",
    "review",
    "code_graph",
    "eval",
    "notebooklm_list",
    "notebooklm_query",
    "notebooklm_research",
}

READ_TIMEOUT = 30.0


class McpStdioClient:
    """Minimal JSON-RPC-over-stdio driver with a deadline-bounded reader."""

    def __init__(self, tmp_path):
        env = os.environ.copy()
        env.pop("PYTEST_CURRENT_TEST", None)
        self.stderr_log = tmp_path / "mcp_stderr.log"
        self._stderr_file = open(self.stderr_log, "wb")
        # The server runs in an isolated tmp workspace (not the repo root) so
        # resources/list only sees the artifacts this test creates.
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO_ROOT, "mcp_server.py"), "stdio"],
            cwd=str(self.workspace),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
        )
        self.lines: "queue.Queue[bytes]" = queue.Queue()
        self.raw_stdout_lines = []  # every line ever seen, for purity checks
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self.raw_stdout_lines.append(line)
            self.lines.put(line)

    def send_bytes(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, obj: dict, bom: bool = False):
        payload = json.dumps(obj).encode("utf-8") + b"\n"
        if bom:
            payload = b"\xef\xbb\xbf" + payload
        self.send_bytes(payload)

    def read_response(self, timeout: float = READ_TIMEOUT) -> dict:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            stderr_tail = self.stderr_tail()
            pytest.fail(
                f"no JSON-RPC response within {timeout}s. stderr tail:\n{stderr_tail}"
            )
        text = line.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text)
        except ValueError:
            pytest.fail(f"non-JSON output on the MCP stdout stream: {text!r}")

    def assert_stream_quiet(self, grace: float = 0.5):
        """No unsolicited output may appear on stdout."""
        try:
            line = self.lines.get(timeout=grace)
            pytest.fail(f"unexpected extra line on MCP stdout: {line!r}")
        except queue.Empty:
            pass

    def stderr_tail(self) -> str:
        self._stderr_file.flush()
        try:
            return self.stderr_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            return "<unavailable>"

    def close(self):
        try:
            if self.proc.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                        capture_output=True,
                    )
                else:
                    self.proc.kill()
            self.proc.wait(timeout=15)
        finally:
            self._stderr_file.close()


@pytest.fixture
def mcp(tmp_path):
    client = McpStdioClient(tmp_path)
    yield client
    client.close()


def test_mcp_stdio_handshake_tools_list_and_stream_purity(mcp):
    # A pipeline artifact pre-created in the server's workspace cwd: the
    # resource endpoints must see exactly this file and nothing else.
    (mcp.workspace / "research.md").write_text("# Findings", encoding="utf-8")

    # --- initialize -> exactly one response with the matching id ---
    mcp.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "realrun-test", "version": "0.0.1"},
            },
        }
    )
    init = mcp.read_response()
    assert init["jsonrpc"] == "2.0"
    assert init["id"] == 1
    assert init["result"]["serverInfo"]["name"] == "genius"
    assert "tools" in init["result"]["capabilities"]
    assert init["result"]["capabilities"]["resources"] == {"listChanged": False}

    # --- notifications/initialized must NOT be answered ---
    mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    mcp.assert_stream_quiet()

    # --- tools/list -> the 17 documented tools ---
    mcp.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools_resp = mcp.read_response()
    assert tools_resp["id"] == 2
    tools = tools_resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == EXPECTED_TOOLS
    for tool in tools:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"

    # --- a request prefixed with a UTF-8 BOM still parses ---
    mcp.send({"jsonrpc": "2.0", "id": 3, "method": "ping"}, bom=True)
    pong = mcp.read_response()
    assert pong == {"jsonrpc": "2.0", "id": 3, "result": {}}

    # --- resources/list -> exactly the pre-created artifact ---
    mcp.send({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    res_list = mcp.read_response()
    assert res_list["id"] == 4
    resources = res_list["result"]["resources"]
    assert [r["uri"] for r in resources] == ["genius://artifacts/research.md"]
    assert resources[0]["mimeType"] == "text/markdown"

    # --- resources/read -> the artifact contents round-trip ---
    mcp.send(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": "genius://artifacts/research.md"},
        }
    )
    res_read = mcp.read_response()
    assert res_read["id"] == 5
    contents = res_read["result"]["contents"]
    assert contents == [
        {
            "uri": "genius://artifacts/research.md",
            "mimeType": "text/markdown",
            "text": "# Findings",
        }
    ]

    # --- stream purity: nothing but the five responses, all pure JSON ---
    mcp.assert_stream_quiet()
    assert len(mcp.raw_stdout_lines) == 5, (
        f"expected exactly 5 stdout lines (one per request), got "
        f"{len(mcp.raw_stdout_lines)}: {mcp.raw_stdout_lines!r}"
    )
    for raw in mcp.raw_stdout_lines:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        assert parsed.get("jsonrpc") == "2.0"


def test_mcp_stdio_unknown_method_yields_jsonrpc_error(mcp):
    mcp.send({"jsonrpc": "2.0", "id": 7, "method": "definitely/not-a-method"})
    resp = mcp.read_response()
    assert resp["id"] == 7
    assert resp["error"]["code"] == -32601
