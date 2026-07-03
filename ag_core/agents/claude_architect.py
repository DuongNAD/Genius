import json
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.models import DesignPlan
from ag_core.utils.logger import log_transaction
from ag_core.utils.prompt_templates import ARCHITECT_PROMPT


def _architect_system_prompt() -> str:
    """ARCHITECT_PROMPT + the DesignPlan JSON schema + a worked example.

    Entirely static, so it is built once at import — the schema dump and
    json.dumps used to run on every request.
    """
    if hasattr(DesignPlan, "model_json_schema"):
        schema_dict = DesignPlan.model_json_schema()
    else:
        schema_dict = DesignPlan.schema()
    schema_json = json.dumps(schema_dict, indent=2)
    return (
        ARCHITECT_PROMPT
        + f"\n\nThe single ```json block must conform to this DesignPlan JSON schema:\n{schema_json}"
        "\n\nExample of a valid response (structure only — adapt to the actual request):\n"
        "```json\n"
        "{\n"
        '  "project_name": "todo_api",\n'
        '  "description": "A small FastAPI TODO service.",\n'
        '  "files": [\n'
        '    {"path": "src/main.py", "specification": "FastAPI app exposing GET/POST /todos backed by an in-memory store. Define a Todo model with id:int and title:str, plus list_todos() and create_todo() handlers."},\n'
        '    {"path": "tests/test_main.py", "specification": "pytest tests using FastAPI TestClient that cover listing todos and creating a todo, asserting status codes and response bodies."}\n'
        "  ]\n"
        "}\n"
        "```"
    )


ARCHITECT_SYSTEM_PROMPT = _architect_system_prompt()


class ClaudeArchitectAgent(BaseAgent):
    """
    Claude Architect Agent that scans project files, designs architecture,
    and documents structure and layout.
    """

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="ClaudeArchitectAgent", provider=provider, **kwargs)

    async def run(
        self, prompt: str | None = None, context_data: dict | None = None
    ) -> str:
        user_prompt = (
            prompt
            or self.extra_params.get("prompt")
            or "Design architecture and structure for the project."
        )

        # Parse and wrap specialized slash commands
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd == "/plan":
                user_prompt = f"Develop a comprehensive, step-by-step implementation plan for the following request, including directory layout and file structure:\n\n{query}"
            elif cmd == "/design":
                user_prompt = f"Design the high-level architecture, module interactions, and system components for the following design request:\n\n{query}"
            elif cmd == "/review-architecture":
                user_prompt = f"Analyze the current project architecture, identifying architectural design patterns, coupling issues, and structural improvement areas:\n\n{query}"

        # Scan project files (or use provided context_data) and format context
        _, context = await self.scan_context_async(context_data)

        # Retrieve matching past interactions
        past_memories = await self.retrieve_memory_async(user_prompt, limit=3)
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

        # Invoke provider (system prompt is the static module-level constant)
        response = await self.provider.send_prompt(
            full_prompt, system=ARCHITECT_SYSTEM_PROMPT
        )
        content = response.get("content", "")
        usage = response.get("usage", {})

        self.history.append({"prompt": user_prompt, "response": content})

        # Save interaction to memory
        await self.store_memory_async(
            text=f"Prompt: {user_prompt}\nResponse: {content}",
            metadata={
                "type": "agent_run",
                "task_id": self.extra_params.get("task_id", "unknown"),
            },
        )

        # Log transaction
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        # Write to output file
        output_file = self.resolve_output_file("design.md")
        self.write_output(output_file, content)

        return content
