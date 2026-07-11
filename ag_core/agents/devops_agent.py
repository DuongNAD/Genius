from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.prompt_templates import DEVOPS_PROMPT
from ag_core.directives import ALL_MODIFIERS


class DevOpsAgent(BaseAgent):
    """
    DevOps Agent that scans project files, generates CI/CD pipelines,
    docker files, and automates deployment scripts.
    """

    __test__ = False

    DEFAULT_TASK = (
        "Generate a CI/CD workflow and deployment configuration for this project."
    )
    SLASH_PREFIXES = {
        "/deploy": "Generate a robust CI/CD deployment configuration and deployment steps for the following requirements:\n\n",
    }
    SYSTEM_PROMPT = DEVOPS_PROMPT
    USES_MEMORY = True
    DEFAULT_OUTPUT_FILE = "deploy.md"
    # Prose output consumed verbatim -> accepts every modifier.
    ACCEPTED_MODIFIERS = ALL_MODIFIERS

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="DevOpsAgent", provider=provider, **kwargs)

    async def run(
        self,
        prompt: str | None = None,
        context_data: dict | None = None,
        *,
        effort: str | None = None,
    ) -> str:
        return await self._run_standard(prompt, context_data, effort=effort)
