import abc
from typing import Any
from ag_core.interfaces.base_provider import BaseProvider

class BaseAgent(abc.ABC):
    """
    Abstract Base Class for all agents.
    Defines the core interface that every agent must implement to run its loop.
    """
    def __init__(self, name: str, provider: BaseProvider, **kwargs: Any) -> None:
        self.name = name
        self.provider = provider
        self.extra_params = kwargs

    @abc.abstractmethod
    async def run(self) -> str:
        """
        Executes the agent's logic/loop and returns the final result as a string.
        """
        pass
