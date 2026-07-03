from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction
from ag_core.utils.prompt_templates import RESEARCHER_PROMPT


class ResearcherAgent(BaseAgent):
    """
    Researcher Agent that scans project files, researches requirements, and
    documents findings. (Formerly ``GrokResearcherAgent`` — named after its
    original backend; the role now defaults to the agy/Gemini-first chain.)
    """

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="ResearcherAgent", provider=provider, **kwargs)

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

        # Scan project files (or use provided context_data) and format context
        _, context = await self.scan_context_async(context_data)
        history_context = self.format_history()

        full_prompt = (
            f"{history_context}{user_prompt}\n\nProject files context:\n{context}"
        )

        # Invoke provider
        response = await self.provider.send_prompt(
            full_prompt, system=RESEARCHER_PROMPT
        )
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
        output_file = self.resolve_output_file("research.md")
        self.write_output(output_file, content)

        return content
