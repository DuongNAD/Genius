from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.prompt_templates import SECURITY_PROMPT


class SecurityAgent(BaseAgent):
    """
    Security Agent that scans project files, performs security audits,
    checks for hardcoded secrets, vulnerabilities, and insecure dependencies.
    """

    __test__ = False

    DEFAULT_TASK = (
        "Perform a security audit on the codebase, checking for "
        "vulnerabilities and secrets."
    )
    SLASH_PREFIXES = {
        "/security": "Perform a security code audit, looking for vulnerabilities, insecure practices, data leaks, or potential attack vectors:\n\n",
        "/audit": "Perform a comprehensive security audit of the following code and assets:\n\n",
        "/security-audit": "Perform a comprehensive security audit of the following code and assets:\n\n",
    }
    SYSTEM_PROMPT = SECURITY_PROMPT
    USES_MEMORY = True
    DEFAULT_OUTPUT_FILE = "audit.md"
    # Output is parsed by parse_security_verdict (JSON/verdict) -> effort only;
    # a @table here could drop to the free-text heuristic and invert the verdict.
    ACCEPTED_MODIFIERS = frozenset({"deep"})

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="SecurityAgent", provider=provider, **kwargs)

    async def run(
        self,
        prompt: str | None = None,
        context_data: dict | None = None,
        *,
        effort: str | None = None,
    ) -> str:
        return await self._run_standard(prompt, context_data, effort=effort)
