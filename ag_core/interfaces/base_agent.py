import abc
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
                chroma_persist_dir=chroma_persist_dir
            )
        
        from ag_core.utils.git import GitManager
        self.git = GitManager()

    def store_memory(self, text: str, metadata: dict | None = None) -> None:
        if self.memory:
            self.memory.add(text=text, metadata=metadata)

    def retrieve_memory(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self.memory:
            return self.memory.query(query_text=query, n_results=limit)
        return []

    @abc.abstractmethod
    async def run(self) -> str:
        """
        Executes the agent's logic/loop and returns the final result as a string.
        """
        pass
