"""Tests for the real-run resilience fixes:

- F6: poll-timeout clamped to the CLI timeout (effective_poll_timeout)
- F9: degraded-mode research fallback, design/plan written before the debate,
      debate failures no longer lose a valid design
- F10: self-healing loops survive agent-call PipelineErrors between attempts
- F11: failure logs embedded into prompts are truncated
- F12: previous artifacts are archived to .bak instead of deleted
- F13: HTTP error bodies surface in PipelineError messages
- F14: poll-timeout errors name the agent URL and task id
"""

import json
import os
import sys

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator
from orchestrator import (
    PipelineError,
    clean_output_files,
    effective_poll_timeout,
    run_e2e_pipeline,
    run_pipeline,
    truncate_log,
)


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _proc_mock(returncode=0, stdout=b"print('hello')"):
    proc = MagicMock()

    async def communicate():
        return (stdout, b"")

    proc.communicate = communicate
    proc.returncode = returncode
    return proc


async def _exec_ok(*args, **kwargs):
    return _proc_mock()


def sequential_call_api(responses, calls_recorded=None):
    """Build a call_api side effect that replays ``responses`` in order;
    an Exception instance in the list is raised instead of returned."""
    state = {"i": 0}

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if calls_recorded is not None:
            calls_recorded.append((url, prompt))
        resp = responses[state["i"]]
        state["i"] += 1
        if isinstance(resp, Exception):
            raise resp
        return resp

    return impl


# --- F6: poll-timeout clamp -------------------------------------------------


def test_effective_poll_timeout_clamps_when_cli_timeout_set(monkeypatch):
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "100")
    # 30s poll deadline would abandon a 100s CLI run: clamp to 100 + 60.
    assert effective_poll_timeout(30.0) == 160.0
    # A deadline already above the clamp is left alone.
    assert effective_poll_timeout(500.0) == 500.0


def test_effective_poll_timeout_pytest_passthrough_without_env(monkeypatch):
    # Under pytest with no explicit GENIUS_CLI_TIMEOUT the clamp is skipped so
    # tests exercising short poll timeouts stay fast.
    monkeypatch.delenv("GENIUS_CLI_TIMEOUT", raising=False)
    assert effective_poll_timeout(0.05) == 0.05


# --- F11: embedded log truncation --------------------------------------------


def test_truncate_log_keeps_tail_with_marker():
    log = "A" * 5000 + "TAIL"
    out = truncate_log(log, limit=1000)
    assert out.startswith("(truncated)\n")
    assert out.endswith("TAIL")
    assert len(out) == 1000 + len("(truncated)\n")


def test_truncate_log_short_input_passthrough():
    assert truncate_log("short") == "short"
    assert truncate_log("") == ""


# --- F12: archive instead of delete ------------------------------------------


def test_clean_output_files_archives_to_bak(tmp_path):
    f = tmp_path / "design.md"
    f.write_text("previous run output", encoding="utf-8")
    stale_bak = tmp_path / "design.md.bak"
    stale_bak.write_text("ancient backup", encoding="utf-8")

    clean_output_files([str(f)])

    assert not f.exists()
    # The newer artifact replaced the older .bak.
    assert stale_bak.read_text(encoding="utf-8") == "previous run output"


def test_clean_output_files_missing_file_is_noop(tmp_path):
    clean_output_files([str(tmp_path / "never_existed.md")])
    assert not (tmp_path / "never_existed.md.bak").exists()


# --- F13 / F14: actionable call_api errors ------------------------------------


@pytest.mark.asyncio
async def test_call_api_start_error_includes_http_response_body():
    class FakeClient:
        async def post(self, url, content=None, headers=None):
            req = httpx.Request("POST", url)
            return httpx.Response(401, content=b"Invalid API Key", request=req)

        async def get(self, url, headers=None):  # pragma: no cover - not reached
            raise AssertionError("should not poll after a failed start")

    with pytest.raises(PipelineError) as exc_info:
        await orchestrator.call_api(
            "http://localhost:8001", "k", "some prompt", client=FakeClient()
        )
    msg = str(exc_info.value)
    assert "401" in msg
    assert "Invalid API Key" in msg


@pytest.mark.asyncio
async def test_call_api_poll_error_includes_http_response_body():
    class FakeClient:
        async def post(self, url, content=None, headers=None):
            req = httpx.Request("POST", url)
            body = json.dumps({"task_id": "T7", "status": "processing"}).encode()
            return httpx.Response(200, content=body, request=req)

        async def get(self, url, headers=None):
            req = httpx.Request("GET", url)
            return httpx.Response(400, content=b"Checksum mismatch", request=req)

    with patch("orchestrator.verify_response_checksum", lambda r: None):
        with pytest.raises(PipelineError) as exc_info:
            await orchestrator.call_api(
                "http://localhost:8001", "k", "p", client=FakeClient()
            )
    msg = str(exc_info.value)
    assert "T7" in msg
    assert "Checksum mismatch" in msg


@pytest.mark.asyncio
async def test_call_api_poll_timeout_error_names_agent_and_task():
    class FakeClient:
        async def post(self, url, content=None, headers=None):
            req = httpx.Request("POST", url)
            body = json.dumps({"task_id": "T9", "status": "processing"}).encode()
            return httpx.Response(200, content=body, request=req)

        async def get(self, url, headers=None):
            req = httpx.Request("GET", url)
            body = json.dumps({"status": "processing"}).encode()
            return httpx.Response(200, content=body, request=req)

    with patch("orchestrator.verify_response_checksum", lambda r: None), patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        with pytest.raises(PipelineError) as exc_info:
            await orchestrator.call_api(
                "http://localhost:8007",
                "k",
                "p",
                client=FakeClient(),
                poll_timeout=0.02,
            )
    msg = str(exc_info.value)
    assert "T9" in msg
    assert "http://localhost:8007" in msg


# --- F9: degraded research fallback -------------------------------------------


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_degraded_mode_research_failure_falls_back_to_prompt(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.setenv("GENIUS_DEGRADED_MODE", "1")
    calls = []
    mock_call_api.side_effect = sequential_call_api(
        [
            PipelineError("Grok is out of credits"),  # research fails
            "Claude design without a file plan",  # design
            "Codex review report",  # codex (fallback single-file)
            "def test_x(): pass",  # tester
            "No vulnerabilities detected.",  # security
            "Deploy complete",  # devops
        ],
        calls,
    )
    mock_exec.side_effect = _exec_ok

    await run_pipeline(prompt="Build a calculator app", workspace=str(temp_workspace))

    research = (temp_workspace / "research.md").read_text(encoding="utf-8")
    assert "(research unavailable" in research
    assert "Original request: Build a calculator app" in research
    # Claude received the fallback content instead of nothing.
    assert "(research unavailable" in calls[1][1]


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_strict_mode_research_failure_still_raises(
    mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.delenv("GENIUS_DEGRADED_MODE", raising=False)
    mock_call_api.side_effect = sequential_call_api([PipelineError("Grok is down")])
    with pytest.raises(PipelineError, match="Grok is down"):
        await run_pipeline(prompt="Build x", workspace=str(temp_workspace))


# --- F9: design.md written before the debate ----------------------------------


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_design_md_survives_strict_debate_failure(
    mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.delenv("GENIUS_DEGRADED_MODE", raising=False)
    mock_call_api.side_effect = sequential_call_api(
        ["research", "Claude design v1", PipelineError("Grok critic died")]
    )
    with pytest.raises(PipelineError, match="critic died"):
        await run_pipeline(
            prompt="Build a calculator app",
            workspace=str(temp_workspace),
            max_debate_rounds=2,
        )
    # The pre-debate design was written and is NOT lost.
    assert (temp_workspace / "design.md").read_text(
        encoding="utf-8"
    ) == "Claude design v1"
    proj_design = temp_workspace / "projects" / "build_a_calculator_app" / "design.md"
    assert proj_design.read_text(encoding="utf-8") == "Claude design v1"


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_degraded_debate_failure_keeps_design_and_continues(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.setenv("GENIUS_DEGRADED_MODE", "1")
    mock_call_api.side_effect = sequential_call_api(
        [
            "research",
            "Claude design v1",
            PipelineError("Grok critic died"),  # debate round 1 critic
            "Codex review report",
            "def test_x(): pass",
            "No vulnerabilities detected.",
            "Deploy complete",
        ]
    )
    mock_exec.side_effect = _exec_ok

    await run_pipeline(
        prompt="Build a calculator app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )
    assert (temp_workspace / "design.md").read_text(
        encoding="utf-8"
    ) == "Claude design v1"


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_e2e_plan_md_survives_strict_debate_failure(
    mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.delenv("GENIUS_DEGRADED_MODE", raising=False)
    mock_call_api.side_effect = sequential_call_api(
        ["Claude plan v1", PipelineError("Grok critic died")]
    )
    with pytest.raises(PipelineError, match="critic died"):
        await run_e2e_pipeline(
            prompt="Build x",
            workspace=str(temp_workspace),
            max_debate_rounds=2,
        )
    assert (temp_workspace / "plan.md").read_text(encoding="utf-8") == "Claude plan v1"
    proj_plan = temp_workspace / "projects" / "build_x" / "plan.md"
    assert proj_plan.read_text(encoding="utf-8") == "Claude plan v1"


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_e2e_degraded_debate_failure_keeps_plan(
    mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.setenv("GENIUS_DEGRADED_MODE", "1")
    mock_call_api.side_effect = sequential_call_api(
        ["Claude plan v1 (no files)", PipelineError("Grok critic died")]
    )
    result = await run_e2e_pipeline(
        prompt="Build x",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )
    # No files to implement -> pipeline returns the (kept) plan content.
    assert result == "Claude plan v1 (no files)"
    assert (temp_workspace / "plan.md").read_text(
        encoding="utf-8"
    ) == "Claude plan v1 (no files)"


# --- F10: self-healing loops survive per-attempt agent failures ----------------

_DESIGN_WITH_FILE = (
    "```json\n"
    '{"files": [{"path": "src/app.py", "specification": "a function"}]}\n'
    "```"
)


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_self_heal_continues_past_codex_call_failure(
    mock_exec, mock_call_api, temp_workspace
):
    calls = []
    state = {"codex": 0}

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        calls.append((url, prompt))
        if "8001" in url:
            return "research"
        if "8002" in url:
            return _DESIGN_WITH_FILE
        if "8003" in url:
            state["codex"] += 1
            if state["codex"] == 1:
                raise PipelineError("codex API hiccup")
            return "def run(): return 1"
        if "8004" in url:
            return "def test_run(): pass"
        if "8005" in url:
            return "No vulnerabilities detected."
        if "8006" in url:
            return "deploy ok"
        raise AssertionError(f"unexpected URL {url}")

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_ok

    await run_pipeline(
        prompt="Build a self healer", workspace=str(temp_workspace), max_retries=3
    )

    assert state["codex"] == 2
    # The retry prompt carried the recorded failure back to Codex.
    codex_prompts = [p for (u, p) in calls if "8003" in u]
    assert "Codex agent call failed" in codex_prompts[1]


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_self_heal_continues_past_tester_security_failure(
    mock_exec, mock_call_api, temp_workspace
):
    calls = []
    state = {"tester": 0, "codex": 0}

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        calls.append((url, prompt))
        if "8001" in url:
            return "research"
        if "8002" in url:
            return _DESIGN_WITH_FILE
        if "8003" in url:
            state["codex"] += 1
            return "def run(): return 1"
        if "8004" in url:
            state["tester"] += 1
            if state["tester"] == 1:
                raise PipelineError("tester API hiccup")
            return "def test_run(): pass"
        if "8005" in url:
            return "No vulnerabilities detected."
        if "8006" in url:
            return "deploy ok"
        raise AssertionError(f"unexpected URL {url}")

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_ok

    await run_pipeline(
        prompt="Build a self healer", workspace=str(temp_workspace), max_retries=3
    )

    assert state["tester"] == 2
    assert state["codex"] == 2
    codex_prompts = [p for (u, p) in calls if "8003" in u]
    assert "Tester/Security agent call failed" in codex_prompts[1]


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
@patch("asyncio.create_subprocess_exec", new_callable=MagicMock)
async def test_self_heal_exhausted_attempts_still_fail(
    mock_exec, mock_call_api, temp_workspace, monkeypatch
):
    monkeypatch.delenv("GENIUS_DEGRADED_MODE", raising=False)

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if "8001" in url:
            return "research"
        if "8002" in url:
            return _DESIGN_WITH_FILE
        if "8003" in url:
            raise PipelineError("codex permanently down")
        raise AssertionError(f"unexpected URL {url}")

    mock_call_api.side_effect = impl
    mock_exec.side_effect = _exec_ok

    with pytest.raises(PipelineError, match="Self-healing loop failed"):
        await run_pipeline(
            prompt="Build a self healer", workspace=str(temp_workspace), max_retries=2
        )


@pytest.mark.asyncio
@patch("orchestrator.call_api", new_callable=MagicMock)
async def test_e2e_self_heal_continues_past_agent_call_failures(
    mock_call_api, temp_workspace
):
    state = {"codex": 0, "tester": 0}

    async def impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        if "8002" in url:
            return _DESIGN_WITH_FILE
        if "8003" in url:
            state["codex"] += 1
            if state["codex"] == 1:
                raise PipelineError("codex hiccup")
            return "def run(): return 1"
        if "8004" in url:
            state["tester"] += 1
            if state["tester"] == 1:
                raise PipelineError("tester hiccup")
            return "def test_run(): pass"
        raise AssertionError(f"unexpected URL {url}")

    mock_call_api.side_effect = impl

    with patch("orchestrator.run_subprocess", new=AsyncMock(return_value=(0, ""))):
        result = await run_e2e_pipeline(
            prompt="Build x e2e", workspace=str(temp_workspace), max_retries=3
        )

    assert "successfully" in result
    assert state["codex"] == 2
    assert state["tester"] == 2
