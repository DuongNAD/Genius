"""Tests for ag_core.agent_factory — the single agent-construction path."""

from unittest.mock import MagicMock

import pytest

from ag_core.agent_factory import (
    AGENT_CLASSES,
    LONG_ROLE_ALIASES,
    build_agent,
    resolve_role,
)
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.researcher import ResearcherAgent


def test_resolve_role_folds_all_aliases():
    assert resolve_role("grok") == "researcher"  # legacy backend-named id
    assert resolve_role("grok_researcher") == "researcher"
    assert resolve_role("claude_architect") == "claude"  # long service id
    assert resolve_role("codex_reviewer") == "codex"
    assert resolve_role("tester_agent") == "tester"
    assert resolve_role("security_agent") == "security"
    assert resolve_role("devops_agent") == "devops"
    assert resolve_role("codex") == "codex"  # canonical ids pass through


def test_agent_classes_cover_all_six_roles():
    assert set(AGENT_CLASSES) == {
        "researcher",
        "claude",
        "codex",
        "tester",
        "security",
        "devops",
    }


def test_build_agent_stateless_bundle():
    provider = MagicMock()
    agent = build_agent("researcher", provider=provider)
    assert isinstance(agent, ResearcherAgent)
    assert agent.provider is provider
    assert agent.extra_params.get("stateless") is True
    assert agent.memory is None  # use_memory=False
    # output_file="None" sentinel suppresses the artifact write
    assert agent.resolve_output_file("research.md") == "None"


def test_build_agent_stateful_keeps_defaults():
    provider = MagicMock()
    agent = build_agent("claude", provider=provider, stateless=False, use_memory=False)
    assert isinstance(agent, ClaudeArchitectAgent)
    assert "output_file" not in agent.extra_params
    assert agent.resolve_output_file("design.md") == "design.md"
    assert agent.extra_params.get("stateless") is None


def test_build_agent_unknown_role_raises():
    with pytest.raises(ValueError, match="Unknown role"):
        build_agent("nonexistent")


def test_agent_cls_override_wins():
    sentinel_cls = MagicMock()
    provider = MagicMock()
    build_agent("devops", provider=provider, agent_cls=sentinel_cls)
    assert sentinel_cls.called
    kwargs = sentinel_cls.call_args.kwargs
    assert kwargs["provider"] is provider
    assert kwargs["output_file"] == "None"
    assert kwargs["stateless"] is True
    assert kwargs["use_memory"] is False


def test_legacy_maps_derive_from_factory():
    """skill_app.ROLE_MAP and worker.ROLE_AGENT_MAP keep their historical
    shapes and accepted-id sets while deriving from the factory tables."""
    from ag_core.distributed.worker import ROLE_AGENT_MAP
    from ag_core.skill_app import ROLE_MAP

    assert ROLE_MAP == AGENT_CLASSES
    expected_ids = set(AGENT_CLASSES) | set(LONG_ROLE_ALIASES)
    assert set(ROLE_AGENT_MAP) == expected_ids
    for role_id, (mod, cls, factory_role) in ROLE_AGENT_MAP.items():
        assert resolve_role(role_id) == factory_role
        assert AGENT_CLASSES[factory_role] == (mod, cls)
