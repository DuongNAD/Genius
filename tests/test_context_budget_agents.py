"""BaseAgent context budgeting on the DIRECT paths (P1 fix).

The pipeline budgets its scanned context at its own call sites, but the
direct paths — skill /run with no supplied context, MCP single-agent tools,
the distributed worker, the run.py CLIs — used to inline the ENTIRE cwd scan
into the prompt (a real repo produced a 2.3 MB prompt that killed agy's argv
and degraded every backend). BaseAgent.scan_context now trims a SELF-scanned
workspace through ag_core.scanner.repo_graph.build_budgeted_context:

* under GENIUS_CONTEXT_TOKEN_BUDGET -> identity passthrough (old behavior);
* over it -> graph-ranked trim, seeded by the task text;
* caller-supplied context_data is NEVER touched (the caller's contract).

Also pins the task_text threading: _run_standard, the codex reviewer, and
the tester all pass the resolved user prompt into scan_context_async so the
graph ranking can seed on files the task names.
"""

import pytest

from ag_core.interfaces.base_agent import BaseAgent

BIG_FILE = "x = 1\n" * 4000  # ~24 KB / well over any tiny test budget per file


class _EchoProvider:
    """Duck-typed provider capturing the composed prompt."""

    model_name = "echo-model"

    def __init__(self):
        self.prompts = []

    async def send_prompt(self, prompt, system=None, *, effort=None, **kwargs):
        self.prompts.append(prompt)
        return {"content": "ok", "usage": {}}


class _Agent(BaseAgent):
    DEFAULT_TASK = "default task"

    async def run(self, prompt=None, context_data=None, *, effort=None):
        return await self._run_standard(prompt, context_data, effort=effort)


def _agent():
    # output_file=None sentinel: never write artifacts from a unit test.
    return _Agent(name="budget-test", provider=_EchoProvider(),
                  use_memory=False, output_file=None)


def _big_scan():
    return {f"m{i}.py": BIG_FILE for i in range(6)}


def _install_scan(monkeypatch, scanned):
    monkeypatch.setattr(
        "ag_core.scanner.project_scanner.ProjectScanner.scan",
        lambda self: dict(scanned),
    )


def test_self_scan_is_budgeted(monkeypatch):
    monkeypatch.setenv("GENIUS_CONTEXT_TOKEN_BUDGET", "400")
    scanned = _big_scan()
    _install_scan(monkeypatch, scanned)

    agent = _agent()
    files, context = agent.scan_context(None, task_text="please improve m2.py")

    full = "".join(
        f"\n--- File: {p} ---\n{c}\n" for p, c in scanned.items()
    )
    # Seeded file survives in full; the rendering is dramatically smaller
    # than the unbudgeted join.
    assert "m2.py" in files
    assert files["m2.py"] == BIG_FILE
    assert len(context) < len(full) / 3


def test_self_scan_under_budget_is_identity(monkeypatch):
    monkeypatch.delenv("GENIUS_CONTEXT_TOKEN_BUDGET", raising=False)
    scanned = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    _install_scan(monkeypatch, scanned)

    agent = _agent()
    files, context = agent.scan_context(None, task_text="anything")

    assert files == scanned
    assert context == "".join(
        f"\n--- File: {p} ---\n{c}\n" for p, c in scanned.items()
    )


def test_supplied_context_data_never_budgeted(monkeypatch):
    # Explicit context_data is the caller's contract: even an absurd budget
    # must not rewrite it (the orchestrator budgets at its own call sites).
    monkeypatch.setenv("GENIUS_CONTEXT_TOKEN_BUDGET", "1")
    supplied = _big_scan()

    agent = _agent()
    files, context = agent.scan_context(supplied, task_text="whatever")

    assert files is supplied
    assert len(context) > len(BIG_FILE) * 5  # all six files rendered in full


@pytest.mark.asyncio
async def test_run_standard_prompt_is_bounded(monkeypatch):
    monkeypatch.setenv("GENIUS_CONTEXT_TOKEN_BUDGET", "400")
    _install_scan(monkeypatch, _big_scan())

    agent = _agent()
    await agent.run(prompt="tidy up m3.py")

    prompt = agent.provider.prompts[0]
    # Six unbudgeted files would be ~150 KB of context; the budgeted prompt
    # stays within the same order of magnitude as ONE file + skeletons.
    assert len(prompt) < len(BIG_FILE) * 3
    assert "m3.py" in prompt


class _Sentinel(Exception):
    """Raised by the recording scan to skip the rest of run()."""


def _record_scan(agent, records):
    def _scan(context_data=None, task_text=""):
        records.append((context_data, task_text))
        raise _Sentinel()

    agent.scan_context = _scan


@pytest.mark.asyncio
async def test_run_standard_threads_task_text():
    agent = _agent()
    records = []
    _record_scan(agent, records)

    with pytest.raises(_Sentinel):
        await agent.run(prompt="hello base")

    assert records == [(None, "hello base")]


@pytest.mark.asyncio
async def test_codex_reviewer_threads_task_text():
    from ag_core.agents.codex_reviewer import CodexReviewerAgent

    agent = CodexReviewerAgent(
        provider=_EchoProvider(), use_memory=False, output_file=None
    )
    records = []
    _record_scan(agent, records)

    with pytest.raises(_Sentinel):
        await agent.run(prompt="hello codex")

    assert records == [(None, "hello codex")]


@pytest.mark.asyncio
async def test_tester_threads_task_text():
    from ag_core.agents.tester import TesterAgent

    agent = TesterAgent(
        provider=_EchoProvider(), use_memory=False, output_file=None
    )
    records = []
    _record_scan(agent, records)

    with pytest.raises(_Sentinel):
        await agent.run(prompt="hello tester")

    assert records == [(None, "hello tester")]
