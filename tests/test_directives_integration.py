"""Agent-level integration for PromptDirectives (slice 1).

Drives real agents (built via the stateless bundle) with a recording provider
to verify: @deep threads effort to the provider, format/generation modifiers
render guidance for prose agents, structure-sensitive agents gate everything but
effort, a directive-only prompt falls back to DEFAULT_TASK, and two concurrent
runs at different efforts don't bleed.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from ag_core.agent_factory import build_agent  # noqa: E402


class RecordingProvider:
    """Minimal provider stand-in: records the effort + composed prompt."""

    model_name = "fake-model"

    def __init__(self):
        self.calls = []

    async def send_prompt(self, prompt, system=None, *, effort=None, **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "effort": effort})
        return {"content": "ok", "usage": {}}


def _agent(role):
    agent = build_agent(role, stateless=True)
    agent.provider = RecordingProvider()
    return agent


@pytest.mark.asyncio
async def test_deep_threads_effort_high():
    agent = _agent("researcher")
    await agent.run(prompt="@deep Explain Raft consensus", context_data={})
    assert agent.provider.calls[0]["effort"] == "high"


@pytest.mark.asyncio
async def test_no_deep_no_effort():
    agent = _agent("researcher")
    await agent.run(prompt="Explain Raft consensus", context_data={})
    assert agent.provider.calls[0]["effort"] is None


@pytest.mark.asyncio
async def test_prose_format_guidance_injected():
    agent = _agent("researcher")
    await agent.run(prompt="@table Compare SQLite and PostgreSQL", context_data={})
    prompt = agent.provider.calls[0]["prompt"]
    assert "Markdown table" in prompt
    assert "@table" not in prompt  # modifier stripped from the text


@pytest.mark.asyncio
async def test_prose_variants_guidance():
    agent = _agent("researcher")
    await agent.run(prompt="@variants=3 Draft a retry policy", context_data={})
    assert agent.directives.variants == 3
    assert "3 genuinely distinct" in agent.provider.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_codex_gates_variants_keeps_deep():
    agent = _agent("codex")
    await agent.run(
        prompt="/code @deep @variants=3 implement login", context_data={}
    )
    call = agent.provider.calls[0]
    # effort still threads; variants rejected and never rendered as guidance
    assert call["effort"] == "high"
    assert agent.directives.variants is None
    assert "variants" in agent.directives.rejected
    assert "@variants" not in call["prompt"]
    assert "Variant" not in call["prompt"]


@pytest.mark.asyncio
async def test_security_gates_table():
    agent = _agent("security")
    await agent.run(
        prompt="/security-audit @table review this module", context_data={}
    )
    call = agent.provider.calls[0]
    assert "Markdown table" not in call["prompt"]  # verdict JSON contract intact
    assert "table" in agent.directives.rejected


@pytest.mark.asyncio
async def test_architect_gates_format_keeps_deep():
    agent = _agent("claude")
    await agent.run(prompt="/design @deep @steps the retry flow", context_data={})
    call = agent.provider.calls[0]
    assert call["effort"] == "high"
    assert "step-by-step" not in call["prompt"]  # DesignPlan JSON contract intact
    assert "steps" in agent.directives.rejected


@pytest.mark.asyncio
async def test_directive_only_prompt_uses_default_task():
    agent = _agent("researcher")
    await agent.run(prompt="@deep", context_data={})
    call = agent.provider.calls[0]
    assert call["effort"] == "high"
    assert type(agent).DEFAULT_TASK in call["prompt"]


@pytest.mark.asyncio
async def test_concurrency_isolation():
    a1 = _agent("researcher")
    a2 = _agent("researcher")
    await asyncio.gather(
        a1.run(prompt="@deep analyse throughput", context_data={}),
        a2.run(prompt="analyse latency", context_data={}),
    )
    assert a1.provider.calls[0]["effort"] == "high"
    assert a2.provider.calls[0]["effort"] is None


@pytest.mark.asyncio
async def test_whitespace_only_prompt_uses_default_task():
    agent = _agent("researcher")
    await agent.run(prompt="   ", context_data={})
    assert type(agent).DEFAULT_TASK in agent.provider.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_plain_prompt_composition_unchanged():
    # No directive -> guidance must be absent (byte-identical no-op path).
    agent = _agent("researcher")
    await agent.run(prompt="Summarise the design", context_data={})
    assert "--- Response directives ---" not in agent.provider.calls[0]["prompt"]
