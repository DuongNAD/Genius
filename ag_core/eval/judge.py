"""The default provider-backed judge for LLM metrics (R5).

The judge is an in-process LLM call, built exactly like the MCP
``debate``/``review`` tools: a provider resolved through
``ag_core.provider_factory.make_provider`` (so ``GENIUS_PROVIDER_<ROLE>``
overrides apply and a dead backend falls through the chain). No agent
class, no history, no memory, no file writes - just ``send_prompt`` with a
strict judge system prompt.

The grade layer stays testable because :func:`ag_core.eval.grader.grade_case`
accepts any ``judge`` callable; tests inject a fake instead of shelling out
to a real CLI.
"""

import logging

logger = logging.getLogger("ag_core.eval")

JUDGE_SYSTEM_PROMPT = (
    "You are a strict, fair software-evaluation judge. You score one metric "
    "at a time on a 1-5 integer scale using the rubric in the user message. "
    "Be calibrated: reserve 5 for genuinely excellent work and 1 for work "
    "that fails the goal. Do not be swayed by verbosity or confidence. "
    'Respond with ONLY a JSON object of the form {"score": <int>, '
    '"explanation": "<brief reason>"} and nothing else.'
)

# Judging is a reasoning task; reuse the architect ("claude") role's
# default chain (claude -> agy -> codex) rather than inventing a 7th role.
JUDGE_ROLE = "claude"


def default_judge(config=None, role: str = JUDGE_ROLE):
    """Return an async ``judge(prompt) -> raw_text`` backed by a provider.

    The provider (or fallback chain) is constructed once and reused across
    every metric in a grade run.
    """
    from ag_core.config import load_config
    from ag_core.provider_factory import make_provider

    cfg = config or load_config()
    provider = make_provider(role, cfg)

    async def _judge(prompt: str) -> str:
        try:
            response = await provider.send_prompt(prompt, system=JUDGE_SYSTEM_PROMPT)
        except Exception as e:  # noqa: BLE001 - a judge failure must not
            # crash the whole grade; parse_verdict maps "" to score 0 (N/A).
            logger.warning("Judge call failed: %s", e)
            return ""
        return response.get("content", "") or ""

    return _judge
