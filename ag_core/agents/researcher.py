from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.prompt_templates import RESEARCHER_PROMPT


class ResearcherAgent(BaseAgent):
    """
    Researcher Agent that scans project files, researches requirements, and
    documents findings. (Formerly ``GrokResearcherAgent`` — named after its
    original backend; the role now defaults to the agy/Gemini-first chain.)
    """

    DEFAULT_TASK = (
        "Research the project requirements and identify technical challenges."
    )
    SLASH_PREFIXES = {
        "/research": "Perform an in-depth research on the following query, focusing on finding technical requirements, dependencies, and integration challenges:\n\n",
        "/summarize": "Provide a clear and concise summary of the following query and the project files, highlighting key points, architecture choices, and critical aspects:\n\n",
        "/fact-check": "Verify facts, check assumptions, and identify potential logical gaps or factual errors in the following query against the project files context:\n\n",
    }
    SYSTEM_PROMPT = RESEARCHER_PROMPT
    USES_MEMORY = False
    DEFAULT_OUTPUT_FILE = "research.md"

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="ResearcherAgent", provider=provider, **kwargs)

    async def run(
        self, prompt: str | None = None, context_data: dict | None = None
    ) -> str:
        return await self._run_standard(prompt, context_data)
