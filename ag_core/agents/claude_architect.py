import json
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.models import DesignPlan
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

    DEFAULT_TASK = "Design architecture and structure for the project."
    SLASH_PREFIXES = {
        "/plan": "Develop a comprehensive, step-by-step implementation plan for the following request, including directory layout and file structure:\n\n",
        "/design": "Design the high-level architecture, module interactions, and system components for the following design request:\n\n",
        "/review-architecture": "Analyze the current project architecture, identifying architectural design patterns, coupling issues, and structural improvement areas:\n\n",
    }
    SYSTEM_PROMPT = ARCHITECT_SYSTEM_PROMPT
    USES_MEMORY = True
    DEFAULT_OUTPUT_FILE = "design.md"
    # Output is parsed into a DesignPlan JSON block -> effort only; no
    # format/variants that could perturb the single ```json``` contract.
    ACCEPTED_MODIFIERS = frozenset({"deep"})

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        super().__init__(name="ClaudeArchitectAgent", provider=provider, **kwargs)

    async def run(
        self,
        prompt: str | None = None,
        context_data: dict | None = None,
        *,
        effort: str | None = None,
    ) -> str:
        return await self._run_standard(prompt, context_data, effort=effort)
