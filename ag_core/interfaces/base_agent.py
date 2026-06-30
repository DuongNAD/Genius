import abc
import os
from typing import Any, List, Dict
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.memory.vector_store import VectorMemory


class BaseAgent(abc.ABC):
    """
    Abstract Base Class for all agents.
    Defines the core interface that every agent must implement to run its loop.
    """

    def __init__(self, name: str, provider: BaseProvider, **kwargs: Any) -> None:
        self.name = name
        self.provider = provider
        self.extra_params = kwargs

        # Read from config memory section if available
        config = kwargs.get("config") or getattr(self, "config", None)
        if not config:
            try:
                from ag_core.config import load_config

                config = load_config()
            except Exception:
                config = None

        memory_enabled = True
        use_chroma = False
        db_path = None
        chroma_persist_dir = None

        if config and hasattr(config, "memory"):
            memory_enabled = config.memory.enabled
            use_chroma = config.memory.use_chroma
            db_path = config.memory.db_path
            chroma_persist_dir = config.memory.chroma_persist_dir

        # Allow kwargs to override config values
        use_memory = kwargs.get("use_memory", memory_enabled)
        use_chroma = kwargs.get("use_chroma", use_chroma)
        db_path = kwargs.get("db_path", db_path)
        chroma_persist_dir = kwargs.get("chroma_persist_dir", chroma_persist_dir)

        self.memory = None
        if use_memory:
            self.memory = VectorMemory(
                collection_name=self.name,
                use_chroma=use_chroma,
                db_path=db_path,
                chroma_persist_dir=chroma_persist_dir,
            )

        from ag_core.utils.git import GitManager

        self.git = GitManager()
        self.history: List[Dict[str, str]] = []

    def clear_history(self) -> None:
        self.history.clear()

    def store_memory(self, text: str, metadata: dict | None = None) -> None:
        if self.memory:
            self.memory.add(text=text, metadata=metadata)

    def retrieve_memory(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self.memory:
            return self.memory.query(query_text=query, n_results=limit)
        return []

    def scan_context(self, context_data: dict | None = None):
        """Resolve the project file map (scanning cwd unless context_data is
        supplied) and render it as a prompt context block.

        Returns (scanned_files, context_str).
        """
        if context_data is not None:
            scanned_files = context_data
        else:
            from ag_core.scanner.project_scanner import ProjectScanner

            config = getattr(self, "config", None)
            exclude_patterns = config.scanner.exclude_patterns if config else None
            scanner = ProjectScanner(root_dir=os.getcwd(), extra_ignores=exclude_patterns)
            scanned_files = scanner.scan()

        context = ""
        for filepath, file_content in scanned_files.items():
            context += f"\n--- File: {filepath} ---\n{file_content}\n"
        return scanned_files, context

    def format_history(self) -> str:
        """Render self.history as a prompt preamble (empty string if none)."""
        if not self.history:
            return ""
        parts = ["Previous conversation history:\n"]
        for turn in self.history:
            parts.append(f"User: {turn['prompt']}\nAgent: {turn['response']}\n")
        parts.append("\n")
        return "".join(parts)

    def resolve_output_file(self, default: str) -> str:
        """Resolve the destination path: an explicit `output_file=None` kwarg
        means 'do not write' (sentinel "None"); absence falls back to default."""
        output_file = self.extra_params.get("output_file")
        if output_file is None:
            if "output_file" in self.extra_params:
                return "None"
            return default
        return output_file

    def write_output(self, output_file: str, content: str) -> None:
        """Write content to output_file, creating parent dirs. The "None"
        sentinel and empty paths are skipped. Write failures are non-fatal."""
        if not output_file or output_file == "None":
            return
        try:
            dir_name = os.path.dirname(output_file)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"Warning: Failed to write output file {output_file}: {e}")

    @abc.abstractmethod
    async def run(self) -> str:
        """
        Executes the agent's logic/loop and returns the final result as a string.
        """
