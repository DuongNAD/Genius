"""Tests for the R5 opt-in surgical-edit mode (Wave 5).

The code-preservation guidance must augment the coder system prompt only
when ``GENIUS_SURGICAL_EDITS`` is set and only for generation requests; by
default the system prompt stays byte-identical to before.
"""

import pytest
from unittest.mock import AsyncMock, patch

from ag_core.agents.codex_reviewer import (
    SURGICAL_EDIT_GUIDANCE,
    CodexReviewerAgent,
    _surgical_edits_enabled,
)
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.utils.prompt_templates import CODER_PROMPT


async def _capture_generation_system(prompt="/code write a hello function"):
    """Run a generation request and return the `system` sent to the provider."""
    provider = OpenAIProvider()
    # Stateless bundle: no file writes, no memory DB side effects.
    agent = CodexReviewerAgent(
        provider=provider,
        max_retries=1,
        output_file="None",
        use_memory=False,
        stateless=True,
    )
    mock_send = AsyncMock(
        return_value={"content": "```python\nx = 1\n```", "usage": {}}
    )
    with patch("ag_core.agents.codex_reviewer.log_transaction"), patch.object(
        provider, "send_prompt", mock_send
    ):
        await agent.run(prompt=prompt, context_data={"main.py": "code"})
    return mock_send.call_args.kwargs["system"]


def test_surgical_edits_flag_reads_env(monkeypatch):
    monkeypatch.delenv("GENIUS_SURGICAL_EDITS", raising=False)
    assert _surgical_edits_enabled() is False
    monkeypatch.setenv("GENIUS_SURGICAL_EDITS", "1")
    assert _surgical_edits_enabled() is True
    monkeypatch.setenv("GENIUS_SURGICAL_EDITS", "false")
    assert _surgical_edits_enabled() is False


@pytest.mark.asyncio
async def test_generation_system_unchanged_by_default(monkeypatch):
    monkeypatch.delenv("GENIUS_SURGICAL_EDITS", raising=False)
    system = await _capture_generation_system()
    assert system == CODER_PROMPT


@pytest.mark.asyncio
async def test_generation_system_augmented_when_enabled(monkeypatch):
    monkeypatch.setenv("GENIUS_SURGICAL_EDITS", "1")
    system = await _capture_generation_system()
    assert system == CODER_PROMPT + SURGICAL_EDIT_GUIDANCE
    assert "Code preservation" in system
    assert "NEVER change model names" in system
