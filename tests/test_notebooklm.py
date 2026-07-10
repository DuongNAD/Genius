"""Tests for the NotebookLM integration:

* the ``nlm`` CLI resolver + JSON helpers,
* the shared ``nlm_query`` / ``nlm_list_notebooks`` / ``nlm_research``
  coroutines (mocked subprocess),
* the ``NotebookLMProvider`` drop-in Researcher backend,
* the provider-factory wiring (opt-in ``notebooklm`` backend),
* the ``notebooklm_*`` MCP tools, and
* the ``--doctor`` probe entry.

Every subprocess is mocked - no test ever touches the real ``nlm`` CLI or the
network. The subprocess-driving tests wrap the coroutine in ``asyncio.run`` so
they run without depending on pytest-asyncio's mode (mirrors
test_agy_provider.py).
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from ag_core import diagnostics, provider_factory
from ag_core.config import load_config
from ag_core.provider_factory import FallbackProvider, make_provider, resolve_chain
from ag_core.providers import notebooklm_provider as nlm
from ag_core.providers.notebooklm_provider import (
    NotebookLMProvider,
    _extract_notebook_id,
    _parse_nlm_json,
    resolve_notebooklm_cli,
)

_NLM_ENV_VARS = (
    "GENIUS_NLM_PATH",
    "GENIUS_NOTEBOOKLM_NOTEBOOK",
    "GENIUS_MODEL_NOTEBOOKLM",
    "GENIUS_NLM_QUERY_TIMEOUT",
    "GENIUS_NLM_RESEARCH_TIMEOUT",
    "GENIUS_NLM_MAX_QUERY_CHARS",
    "GENIUS_PROVIDER_RESEARCHER",
    "GENIUS_PROVIDER_GROK",
)


@pytest.fixture(autouse=True)
def _clean_nlm_env(monkeypatch):
    """Start every test from the no-knobs-set default."""
    for var in _NLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _mock_process(stdout=b"", stderr=b"", returncode=0):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


def _json_proc(payload, returncode=0):
    return _mock_process(
        stdout=json.dumps(payload).encode("utf-8"), returncode=returncode
    )


def _run(coro):
    return asyncio.run(coro)


# --- CLI resolution ---------------------------------------------------------


def test_resolve_env_path_override_wins(monkeypatch):
    monkeypatch.setenv("GENIUS_NLM_PATH", r"D:\tools\nlm.exe")
    with patch("shutil.which", return_value=r"C:\elsewhere\nlm.exe"):
        assert resolve_notebooklm_cli() == r"D:\tools\nlm.exe"


def test_resolve_blank_env_path_treated_as_unset(monkeypatch):
    # A blank GENIUS_NLM_PATH must fall through to the PATH scan, not be
    # returned as the (empty) executable. Use an absolute path outside the
    # repo for the platform (else which_external re-scans an in-repo match).
    real_cli = r"C:\real\nlm.exe" if os.name == "nt" else "/opt/real/nlm"
    monkeypatch.setenv("GENIUS_NLM_PATH", "   ")
    with patch("shutil.which", return_value=real_cli):
        assert resolve_notebooklm_cli() == real_cli


def test_resolve_keeps_benign_literal_under_pytest():
    # PYTEST_CURRENT_TEST is set right now: an uninstalled nlm still resolves
    # to a harmless literal (unit tests stub the subprocess layer).
    with patch("shutil.which", return_value=None):
        assert resolve_notebooklm_cli() == "nlm"


def test_resolve_raises_actionably_outside_pytest():
    with (
        patch("shutil.which", return_value=None),
        patch.dict(os.environ, {}, clear=True),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            resolve_notebooklm_cli()
    msg = str(exc_info.value)
    assert "GENIUS_NLM_PATH" in msg
    assert "nlm login" in msg
    assert "doctor" in msg


# --- JSON helpers -----------------------------------------------------------


def test_parse_nlm_json_object_array_and_noise():
    assert _parse_nlm_json('{"answer": "hi"}') == {"answer": "hi"}
    assert _parse_nlm_json("[1, 2, 3]") == [1, 2, 3]
    # A stray banner line before the JSON is tolerated.
    assert _parse_nlm_json('warning: stale cache\n{"a": 1}') == {"a": 1}
    assert _parse_nlm_json("   ") is None
    assert _parse_nlm_json("not json at all") is None


def test_extract_notebook_id_flat_and_nested():
    assert _extract_notebook_id({"id": "nb1"}) == "nb1"
    assert _extract_notebook_id({"notebook_id": "nb2"}) == "nb2"
    assert _extract_notebook_id({"notebook": {"id": "nb3"}}) == "nb3"
    assert _extract_notebook_id({"title": "no id here"}) is None
    assert _extract_notebook_id("not a dict") is None


# --- nlm_query --------------------------------------------------------------


def test_nlm_query_builds_args_and_parses_answer():
    proc = _json_proc({"answer": "42", "conversation_id": "c1"})
    # Force the CLI to resolve to the benign "nlm" literal so this argv-shape
    # test is deterministic whether or not a real nlm is installed on PATH.
    with (
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex,
    ):
        mex.return_value = proc
        data = _run(nlm.nlm_query("nb1", "what is the answer?"))

    assert data["answer"] == "42"
    args = mex.call_args.args
    assert args[0] == "nlm"
    assert args[1:6] == ("notebook", "query", "nb1", "what is the answer?", "--json")
    # --timeout is always passed (deterministic) - default 120s.
    assert "--timeout" in args and "120" in args


def test_nlm_query_passes_source_ids():
    proc = _json_proc({"answer": "ok"})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        _run(nlm.nlm_query("nb1", "q", source_ids="s1,s2"))
    args = mex.call_args.args
    assert "--source-ids" in args and "s1,s2" in args


def test_nlm_query_non_json_raises():
    proc = _mock_process(stdout=b"not json", returncode=0)
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        with pytest.raises(RuntimeError, match="no parseable JSON"):
            _run(nlm.nlm_query("nb1", "q"))


def test_nlm_query_empty_notebook_raises():
    with pytest.raises(RuntimeError, match="non-empty notebook"):
        _run(nlm.nlm_query("   ", "q"))


# --- nlm_list_notebooks -----------------------------------------------------


def test_nlm_list_parses_array():
    payload = [{"id": "nb1", "title": "A"}, {"id": "nb2", "title": "B"}]
    proc = _json_proc(payload)
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        got = _run(nlm.nlm_list_notebooks())
    assert got == payload
    args = mex.call_args.args
    assert args[1:] == ("notebook", "list", "--json")


def test_nlm_list_unwraps_dict_shape():
    proc = _json_proc({"notebooks": [{"id": "nb1"}]})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        got = _run(nlm.nlm_list_notebooks())
    assert got == [{"id": "nb1"}]


# --- nlm_research (multi-step orchestration) --------------------------------


def test_nlm_research_creates_notebook_then_queries():
    procs = [
        _json_proc({"id": "nbNEW"}),  # notebook create
        _mock_process(stdout=b"research done", returncode=0),  # research start
        _json_proc({"answer": "grounded answer"}),  # notebook query
    ]
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.side_effect = procs
        result = _run(nlm.nlm_research("quantum computing", mode="deep"))

    assert result["notebook_id"] == "nbNEW"
    assert result["created_notebook"] is True
    assert result["mode"] == "deep"
    assert result["answer"] == "grounded answer"
    assert mex.call_count == 3

    create_args = mex.call_args_list[0].args
    assert create_args[1:4] == (
        "notebook",
        "create",
        "Genius research: quantum computing",
    )
    start_args = mex.call_args_list[1].args
    assert start_args[1:3] == ("research", "start")
    assert "quantum computing" in start_args
    assert "-n" in start_args and "nbNEW" in start_args
    assert "-m" in start_args and "deep" in start_args
    assert "--force" in start_args and "--wait-and-import" in start_args
    query_args = mex.call_args_list[2].args
    assert query_args[1:5] == ("notebook", "query", "nbNEW", "quantum computing")


def test_nlm_research_existing_notebook_skips_create():
    procs = [
        _mock_process(stdout=b"ok", returncode=0),  # research start
        _json_proc({"answer": "A"}),  # notebook query
    ]
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.side_effect = procs
        result = _run(
            nlm.nlm_research("topic", notebook="nbGIVEN", question="focused Q")
        )

    assert result["notebook_id"] == "nbGIVEN"
    assert result["created_notebook"] is False
    assert mex.call_count == 2  # no create step
    query_args = mex.call_args_list[1].args
    assert query_args[1:5] == ("notebook", "query", "nbGIVEN", "focused Q")


def test_nlm_research_bad_create_output_raises():
    proc = _mock_process(stdout=b"no id in here", returncode=0)
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        with pytest.raises(RuntimeError, match="notebook id"):
            _run(nlm.nlm_research("topic"))


# --- NotebookLMProvider -----------------------------------------------------


def test_provider_success_queries_configured_notebook(monkeypatch):
    monkeypatch.setenv("GENIUS_NOTEBOOKLM_NOTEBOOK", "nbENV")
    provider = NotebookLMProvider()
    proc = _json_proc({"answer": "from the notebook"})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        res = _run(provider.send_prompt("summarize the sources"))

    assert res["content"] == "from the notebook"
    assert res["usage"]["total_tokens"] == 0
    args = mex.call_args.args
    assert args[1:5] == ("notebook", "query", "nbENV", "summarize the sources")


def test_provider_model_name_used_as_notebook():
    # config.models.notebooklm lands in model_name; it is the notebook id.
    provider = NotebookLMProvider(model_name="nbMODEL")
    proc = _json_proc({"answer": "x"})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        _run(provider.send_prompt("q"))
    assert mex.call_args.args[4] == "q"
    assert mex.call_args.args[3] == "nbMODEL"


def test_provider_env_notebook_wins_over_model_name(monkeypatch):
    monkeypatch.setenv("GENIUS_NOTEBOOKLM_NOTEBOOK", "nbENV")
    provider = NotebookLMProvider(model_name="nbMODEL")
    proc = _json_proc({"answer": "x"})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        _run(provider.send_prompt("q"))
    assert mex.call_args.args[3] == "nbENV"


def test_provider_no_notebook_raises():
    provider = NotebookLMProvider()  # no model_name, no env
    with pytest.raises(RuntimeError, match="no notebook configured"):
        _run(provider.send_prompt("q"))


def test_provider_empty_answer_raises(monkeypatch):
    monkeypatch.setenv("GENIUS_NOTEBOOKLM_NOTEBOOK", "nb1")
    provider = NotebookLMProvider()
    proc = _json_proc({"answer": ""})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        with pytest.raises(RuntimeError, match="empty answer"):
            _run(provider.send_prompt("q"))


def test_provider_auth_failure_raises_with_hint(monkeypatch):
    monkeypatch.setenv("GENIUS_NOTEBOOKLM_NOTEBOOK", "nb1")
    provider = NotebookLMProvider()
    # nlm prints the auth error to stdout and exits non-zero.
    proc = _mock_process(
        stdout=b"Authentication expired. Run 'nlm login' to re-authenticate.",
        returncode=1,
    )
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        with pytest.raises(RuntimeError) as exc_info:
            _run(provider.send_prompt("q"))
    msg = str(exc_info.value)
    assert "nlm CLI failed with exit code 1" in msg
    assert "Hint" in msg and "authentication" in msg


def test_provider_truncates_overlong_question(monkeypatch):
    monkeypatch.setenv("GENIUS_NOTEBOOKLM_NOTEBOOK", "nb1")
    monkeypatch.setenv("GENIUS_NLM_MAX_QUERY_CHARS", "100")
    provider = NotebookLMProvider()
    proc = _json_proc({"answer": "x"})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        _run(provider.send_prompt("A" * 500))
    question_arg = mex.call_args.args[4]
    assert len(question_arg) == 100  # truncated to the configured ceiling


def test_provider_system_prompt_is_prepended(monkeypatch):
    monkeypatch.setenv("GENIUS_NOTEBOOKLM_NOTEBOOK", "nb1")
    provider = NotebookLMProvider()
    proc = _json_proc({"answer": "x"})
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mex:
        mex.return_value = proc
        _run(provider.send_prompt("Task text", system="Be terse"))
    question_arg = mex.call_args.args[4]
    assert question_arg.startswith("[System instructions]\nBe terse")
    assert "[Question]\nTask text" in question_arg


# --- provider factory wiring ------------------------------------------------


def test_notebooklm_registered_as_backend():
    assert "notebooklm" in provider_factory.BACKENDS


def test_notebooklm_absent_from_every_default_chain():
    for chain in provider_factory.DEFAULT_CHAINS.values():
        assert "notebooklm" not in chain


def test_resolve_chain_accepts_notebooklm(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_RESEARCHER", "notebooklm,agy,claude")
    assert resolve_chain("researcher") == ["notebooklm", "agy", "claude"]


def test_make_provider_single_notebooklm_backend(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_RESEARCHER", "notebooklm")
    provider = make_provider("researcher", load_config())
    assert isinstance(provider, NotebookLMProvider)
    assert not isinstance(provider, FallbackProvider)


def test_make_provider_notebooklm_first_chain(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_RESEARCHER", "notebooklm,agy")
    provider = make_provider("researcher", load_config())
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["notebooklm", "agy"]


def test_genius_model_notebooklm_env_sets_notebook(monkeypatch):
    # GENIUS_MODEL_NOTEBOOKLM feeds the backend's model_name, which the
    # provider treats as the default notebook id.
    from ag_core.provider_factory import build_backend

    monkeypatch.setenv("GENIUS_MODEL_NOTEBOOKLM", "nbFROMENV")
    provider = build_backend("notebooklm", load_config())
    assert isinstance(provider, NotebookLMProvider)
    assert provider.model_name == "nbFROMENV"


# --- MCP tools --------------------------------------------------------------


def test_mcp_dispatch_notebooklm_list():
    import mcp_server

    fake = [{"id": "nb1", "title": "A"}]
    with patch.object(nlm, "nlm_list_notebooks", AsyncMock(return_value=fake)):
        out = _run(mcp_server.dispatch_tool("notebooklm_list", {}))
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["notebooks"] == fake


def test_mcp_dispatch_notebooklm_query():
    import mcp_server

    fake = {"answer": "grounded", "citations": {}}
    with patch.object(nlm, "nlm_query", AsyncMock(return_value=fake)) as mq:
        out = _run(
            mcp_server.dispatch_tool(
                "notebooklm_query", {"notebook": "nb1", "query": "what?"}
            )
        )
    assert json.loads(out) == fake
    mq.assert_awaited_once()
    assert mq.await_args.args == ("nb1", "what?")


def test_mcp_dispatch_notebooklm_query_requires_notebook_and_query():
    import mcp_server

    with pytest.raises(ValueError, match="notebook"):
        _run(mcp_server.dispatch_tool("notebooklm_query", {"query": "q"}))
    with pytest.raises(ValueError, match="query"):
        _run(mcp_server.dispatch_tool("notebooklm_query", {"notebook": "nb1"}))


def test_mcp_dispatch_notebooklm_research():
    import mcp_server

    fake = {"notebook_id": "nbX", "answer": "found it"}
    with patch.object(nlm, "nlm_research", AsyncMock(return_value=fake)) as mr:
        out = _run(
            mcp_server.dispatch_tool(
                "notebooklm_research", {"query": "topic", "mode": "fast"}
            )
        )
    assert json.loads(out) == fake
    mr.assert_awaited_once()
    assert mr.await_args.args == ("topic",)
    assert mr.await_args.kwargs["mode"] == "fast"


def test_mcp_dispatch_notebooklm_research_requires_query():
    import mcp_server

    with pytest.raises(ValueError, match="query"):
        _run(mcp_server.dispatch_tool("notebooklm_research", {}))


def test_mcp_tools_list_includes_notebooklm():
    import mcp_server

    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"notebooklm_list", "notebooklm_query", "notebooklm_research"} <= names
    # Every tool still has a description + object schema.
    for tool in mcp_server.TOOLS:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


# --- doctor wiring ----------------------------------------------------------


def test_doctor_probe_table_includes_notebooklm():
    entry = next((c for c in diagnostics.CLI_CHECKS if c[0] == "notebooklm"), None)
    assert entry is not None
    assert entry[1] is resolve_notebooklm_cli
    assert any("NotebookLM" in dep for dep in entry[2])


def test_doctor_missing_notebooklm_is_not_fatal():
    # notebooklm is opt-in only (in no default chain): its absence never makes
    # the doctor NOT READY.
    def _r(cli, status):
        return {
            "cli": cli,
            "dependents": ["x"],
            "path": f"/{cli}",
            "status": status,
            "detail": "d",
        }

    results = [
        _r("claude", "OK"),
        _r("codex", "OK"),
        _r("notebooklm", "MISSING"),
    ]
    lines, code = diagnostics.report_lines(results, skill_key_ok=True)
    assert code == 0
    assert "NOT READY" not in "\n".join(lines)
