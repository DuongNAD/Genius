import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction

class DevOpsAgent(BaseAgent):
    """
    DevOps Agent that scans project files, generates CI/CD pipelines,
    docker files, and automates deployment scripts.
    """
    __test__ = False

    def __init__(self, provider: BaseProvider, config: Config = None, **kwargs: Any) -> None:
        self.config = config or load_config()
        super().__init__(name="DevOpsAgent", provider=provider, **kwargs)

    async def run(self, prompt: str | None = None, context_data: dict | None = None) -> str:
        user_prompt = prompt or self.extra_params.get("prompt") or "Generate a CI/CD workflow and deployment configuration for this project."
        
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd == "/deploy":
                user_prompt = f"Generate a robust CI/CD deployment configuration and deployment steps for the following requirements:\n\n{query}"

        root_dir = os.getcwd()
        exclude_patterns = self.config.scanner.exclude_patterns
        
        if context_data is not None:
            scanned_files = context_data
        else:
            scanner = ProjectScanner(root_dir=root_dir, extra_ignores=exclude_patterns)
            scanned_files = scanner.scan()
        
        context = ""
        for filepath, file_content in scanned_files.items():
            context += f"\n--- File: {filepath} ---\n{file_content}\n"
            
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
        
        response = await self.provider.send_prompt(full_prompt)
        content = response.get("content", "")
        usage = response.get("usage", {})
        
        self.store_memory(
            text=f"Prompt: {user_prompt}\nResponse: {content}",
            metadata={"type": "agent_run", "task_id": self.extra_params.get("task_id", "unknown")}
        )
        
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0)
        )
        
        output_file = self.extra_params.get("output_file")
        if output_file is None:
            if "output_file" in self.extra_params:
                output_file = "None"
            else:
                output_file = "deploy.md"
        
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
