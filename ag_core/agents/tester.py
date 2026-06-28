import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction

class TesterAgent(BaseAgent):
    """
    Tester Agent that receives Codex's review output, scans project files,
    and writes automatically generated unit tests/scenarios.
    """
    __test__ = False

    def __init__(self, provider: BaseProvider, config: Config = None, **kwargs: Any) -> None:
        self.config = config or load_config()
        super().__init__(name="TesterAgent", provider=provider, **kwargs)

    async def run(self, prompt: str | None = None, context_data: dict | None = None) -> str:
        user_prompt = prompt or self.extra_params.get("prompt") or "Generate unit tests and scenarios based on the review output and project files."
        
        # Parse and wrap specialized slash commands
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd == "/unit-test":
                user_prompt = f"Generate comprehensive unit tests and test suites using pytest for the project files context, focusing on edge cases and validation:\n\n{query}"
            elif cmd == "/stress-test":
                user_prompt = f"Create a performance or stress testing script or scenario to simulate heavy concurrent load, analyzing latency and failure modes:\n\n{query}"

        
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
            
        full_prompt = f"{user_prompt}\n\nProject files context:\n{context}"
        
        # Invoke provider
        response = await self.provider.send_prompt(full_prompt)
        content = response.get("content", "")
        usage = response.get("usage", {})
        
        # Log transaction
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0)
        )
        
        # Write to output file
        output_file = self.extra_params.get("output_file")
        if output_file is None:
            if "output_file" in self.extra_params:
                output_file = "None"
            else:
                output_file = "test_generated.py"
        
        if output_file != "None":
            try:
                dir_name = os.path.dirname(output_file)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"Warning: Failed to write output file {output_file}: {e}")
            
        return content
