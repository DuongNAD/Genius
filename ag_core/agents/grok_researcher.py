"""Backward-compat shim: the Researcher agent moved to
``ag_core.agents.researcher`` when the role id was renamed grok -> researcher.

``GrokResearcherAgent`` is the SAME class object as ``ResearcherAgent``, so
existing ``patch("ag_core.agents.grok_researcher.GrokResearcherAgent.run")``
call sites keep patching the real agent.
"""

from ag_core.agents.researcher import ResearcherAgent as GrokResearcherAgent

__all__ = ["GrokResearcherAgent"]
