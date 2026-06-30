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

    def write_output(self, content: str, default_filename: str) -> str:
        """Resolve the configured output file and write ``content`` to it.

        Mirrors the shared convention across agents: if ``output_file`` is the
        literal string ``"None"`` (or was passed as the kwarg and left None)
        nothing is written — used for stateless runs. Otherwise the content is
        written to ``output_file`` (or ``default_filename`` when unset).
        Returns the resolved output filename.
        """
        output_file = self.extra_params.get("output_file")
        if output_file is None:
            output_file = (
                "None" if "output_file" in self.extra_params else default_filename
            )

        if output_file != "None":
            try:
                dir_name = os.path.dirname(output_file)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"Warning: Failed to write output file {output_file}: {e}")
        return output_file

    @abc.abstractmethod
    async def run(self) -> str:
        """
        Executes the agent's logic/loop and returns the final result as a string.
        """
        pass
