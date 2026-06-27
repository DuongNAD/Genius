import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction

class ClaudeArchitectAgent(BaseAgent):
    """
    Claude Architect Agent that scans project files, designs architecture,
    and documents structure and layout.
    """
    def __init__(self, provider: BaseProvider, config: Config = None, **kwargs: Any) -> None:
        self.config = config or load_config()
        super().__init__(name="ClaudeArchitectAgent", provider=provider, **kwargs)

    async def run(self, prompt: str | None = None) -> str:
        user_prompt = prompt or self.extra_params.get("prompt") or "Design architecture and structure for the project."
        
        # Determine scanning root
        root_dir = os.getcwd()
        exclude_patterns = self.config.scanner.exclude_patterns
        
        # Scan files
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
        output_file = self.extra_params.get("output_file") or "architecture.md"
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"Warning: Failed to write output file {output_file}: {e}")
            
        return content
