from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction
from ag_core.utils.prompt_templates import SECURITY_PROMPT


class SecurityAgent(BaseAgent):
    """
    Security Agent that scans project files, performs security audits,
    checks for hardcoded secrets, vulnerabilities, and insecure dependencies.
    """

    __test__ = False

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="SecurityAgent", provider=provider, **kwargs)

    async def run(
        self, prompt: str | None = None, context_data: dict | None = None
    ) -> str:
        user_prompt = (
            prompt
            or self.extra_params.get("prompt")
            or "Perform a security audit on the codebase, checking for vulnerabilities and secrets."
        )

        # Command routing
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd == "/security":
                user_prompt = f"Perform a security code audit, looking for vulnerabilities, insecure practices, data leaks, or potential attack vectors:\n\n{query}"
            elif cmd in ["/audit", "/security-audit"]:
                user_prompt = f"Perform a comprehensive security audit of the following code and assets:\n\n{query}"

        _, context = self.scan_context(context_data)

        # Retrieve matching past interactions
        past_memories = self.retrieve_memory(user_prompt, limit=3)
        memory_context = ""
        if past_memories:
            memory_context = "\n--- Relevant Historical Memory Context ---\n"
            for i, mem in enumerate(past_memories, 1):
                memory_context += f"Interaction #{i}:\n{mem['text']}\n"

        history_context = self.format_history()

        full_prompt = f"{history_context}{user_prompt}\n"
        if memory_context:
            full_prompt += f"{memory_context}\n"
        full_prompt += f"\nProject files context:\n{context}"

        response = await self.provider.send_prompt(full_prompt, system=SECURITY_PROMPT)
        content = response.get("content", "")
        usage = response.get("usage", {})

        self.history.append({"prompt": user_prompt, "response": content})

        self.store_memory(
            text=f"Prompt: {user_prompt}\nResponse: {content}",
            metadata={
                "type": "agent_run",
                "task_id": self.extra_params.get("task_id", "unknown"),
            },
        )

        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        output_file = self.resolve_output_file("audit.md")
        self.write_output(output_file, content)

        return content
