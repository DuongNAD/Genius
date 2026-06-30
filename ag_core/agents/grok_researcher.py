import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction
from ag_core.utils.prompt_templates import AGENT_CORE_RULES


class GrokResearcherAgent(BaseAgent):
    """
    Grok Researcher Agent that scans project files, researches requirements,
    and documents findings.
    """

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="GrokResearcherAgent", provider=provider, **kwargs)

    async def run(
        self, prompt: str | None = None, context_data: dict | None = None
    ) -> str:
        user_prompt = (
            prompt
            or self.extra_params.get("prompt")
            or "Research the project requirements and identify technical challenges."
        )

        # Parse and wrap specialized slash commands
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd == "/research":
                user_prompt = f"Perform an in-depth research on the following query, focusing on finding technical requirements, dependencies, and integration challenges:\n\n{query}"
            elif cmd == "/summarize":
                user_prompt = f"Provide a clear and concise summary of the following query and the project files, highlighting key points, architecture choices, and critical aspects:\n\n{query}"
            elif cmd == "/fact-check":
                user_prompt = f"Verify facts, check assumptions, and identify potential logical gaps or factual errors in the following query against the project files context:\n\n{query}"

        # Determine scanning root
        root_dir = os.getcwd()
        exclude_patterns = self.config.scanner.exclude_patterns

        # Scan files or use provided context_data
        if context_data is not None:
            scanned_files = context_data
        else:
            scanner = ProjectScanner(root_dir=root_dir, extra_ignores=exclude_patterns)
            scanned_files = scanner.scan()

        # Format scanned files as input context
        context = ""
        for filepath, content in scanned_files.items():
            context += f"\n--- File: {filepath} ---\n{content}\n"

        history_context = ""
        if self.history:
            history_context += "Previous conversation history:\n"
            for turn in self.history:
                history_context += (
                    f"User: {turn['prompt']}\nAgent: {turn['response']}\n"
                )
            history_context += "\n"

        full_prompt = (
            f"{history_context}{user_prompt}\n\nProject files context:\n{context}"
        )

        # Invoke provider
        response = await self.provider.send_prompt(full_prompt, system=AGENT_CORE_RULES)
        content = response.get("content", "")
        usage = response.get("usage", {})

        self.history.append({"prompt": user_prompt, "response": content})

        # Log transaction
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        # Write to output file
        self.write_output(content, "research.md")

        return content
