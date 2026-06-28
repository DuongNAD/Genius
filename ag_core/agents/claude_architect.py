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

    async def run(self, prompt: str | None = None, context_data: dict | None = None) -> str:
        user_prompt = prompt or self.extra_params.get("prompt") or "Design architecture and structure for the project."
        
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
        for filepath, file_content in scanned_files.items():
            context += f"\n--- File: {filepath} ---\n{file_content}\n"
            
        # Retrieve matching past interactions
        past_memories = self.retrieve_memory(user_prompt, limit=3)
        memory_context = ""
        if past_memories:
            memory_context = "\n--- Relevant Historical Memory Context ---\n"
            for i, mem in enumerate(past_memories, 1):
                memory_context += f"Interaction #{i}:\n{mem['text']}\n"

        full_prompt = f"{user_prompt}\n"
        if memory_context:
            full_prompt += f"{memory_context}\n"
        full_prompt += f"\nProject files context:\n{context}"
        
        # Invoke provider
        response = await self.provider.send_prompt(full_prompt)
        content = response.get("content", "")
        usage = response.get("usage", {})
        
        # Save interaction to memory
        self.store_memory(
            text=f"Prompt: {user_prompt}\nResponse: {content}",
            metadata={"type": "agent_run", "task_id": self.extra_params.get("task_id", "unknown")}
        )
        
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
                output_file = "design.md"
        
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
