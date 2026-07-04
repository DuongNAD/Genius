"""Single construction path for the six role agents.

Role -> agent-class wiring used to be duplicated across three hand-synced
tables (``skill_app.ROLE_MAP``, the distributed worker's ``ROLE_AGENT_MAP``,
``mcp_server.TOOL_AGENTS``) plus four near-identical construction blocks.
This module is now the one source of truth: the legacy tables are derived
from :data:`AGENT_CLASSES` so their public shapes (and every accepted role
id) stay exactly as before.

Provider selection stays in ``ag_core.provider_factory`` (role -> backend
chain, env-overridable).
"""

import importlib

from ag_core.config import load_config
from ag_core.provider_factory import canonical_role, make_provider

# canonical role id -> (agent module, agent class name)
AGENT_CLASSES = {
    "researcher": ("ag_core.agents.researcher", "ResearcherAgent"),
    "claude": ("ag_core.agents.claude_architect", "ClaudeArchitectAgent"),
    "codex": ("ag_core.agents.codex_reviewer", "CodexReviewerAgent"),
    "tester": ("ag_core.agents.tester", "TesterAgent"),
    "security": ("ag_core.agents.security_agent", "SecurityAgent"),
    "devops": ("ag_core.agents.devops_agent", "DevOpsAgent"),
}

# Long-form service ids: the distributed worker has always accepted these
# directly (they are the ids remote hubs may dispatch with), so the factory
# folds them too. Legacy ids ("grok", "grok_researcher") are handled by
# provider_factory.canonical_role before this table is consulted.
LONG_ROLE_ALIASES = {
    "claude_architect": "claude",
    "codex_reviewer": "codex",
    "tester_agent": "tester",
    "security_agent": "security",
    "devops_agent": "devops",
}

# The stateless bundle: an API/MCP/worker request must leave no trace on the
# server's working directory — no artifact writes, no vector-memory DB load
# on the event loop, and no self-healing loop that executes the host's test
# suite (see CodexReviewerAgent).
STATELESS_KWARGS = {"output_file": "None", "use_memory": False, "stateless": True}


def resolve_role(role: str) -> str:
    """Canonical role id, folding legacy ("grok") and long service aliases."""
    role = canonical_role(role)
    return LONG_ROLE_ALIASES.get(role, role)


def agent_class(role: str):
    """Import and return the agent class for a canonical/alias role id."""
    resolved = resolve_role(role)
    if resolved not in AGENT_CLASSES:
        raise ValueError(f"Unknown role: {resolved}")
    module_name, class_name = AGENT_CLASSES[resolved]
    return getattr(importlib.import_module(module_name), class_name)


def build_agent(
    role: str,
    *,
    config=None,
    provider=None,
    default_chain=None,
    stateless: bool = True,
    agent_cls=None,
    **agent_kwargs,
):
    """Instantiate the agent (+ provider) for ``role``.

    ``agent_cls`` overrides the class lookup — mcp_server passes its own
    module globals so tests can keep patching ``mcp_server.<AgentClass>``.
    ``default_chain`` flows to make_provider and only applies when no
    explicit GENIUS_PROVIDER_<ROLE> chain is set.
    """
    resolved = resolve_role(role)
    if resolved not in AGENT_CLASSES:
        raise ValueError(f"Unknown role: {resolved}")
    if config is None:
        config = load_config()
    if provider is None:
        provider = make_provider(resolved, config, default_chain=default_chain)
    cls = agent_cls if agent_cls is not None else agent_class(resolved)
    kwargs = {"provider": provider, "config": config}
    kwargs.update(agent_kwargs)
    if stateless:
        kwargs.update(STATELESS_KWARGS)
    return cls(**kwargs)
