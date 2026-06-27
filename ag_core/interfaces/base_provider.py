import abc
from typing import Any, Dict
from pydantic import BaseModel, Field

# --- Response Schemas ---

class TokenUsage(BaseModel):
    """Token usage tracking structure."""
    prompt_tokens: int = Field(default=0, description="Tokens used in the prompt")
    completion_tokens: int = Field(default=0, description="Tokens generated in the completion")
    total_tokens: int = Field(default=0, description="Total tokens consumed")

class ProviderResponse(BaseModel):
    """Standardized LLM response validation model."""
    content: str = Field(..., description="The generated text response from the LLM")
    usage: TokenUsage = Field(default_factory=TokenUsage, description="Token usage statistics")


# --- Base Provider ABC ---

class BaseProvider(abc.ABC):
    """
    Abstract Base Class for LLM providers.
    All concrete provider implementations (e.g. OpenAI, Anthropic, Grok) must inherit from this class.
    """
    def __init__(self, model_name: str, api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.extra_params = kwargs

    @abc.abstractmethod
    async def send_prompt(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Sends a prompt to the provider's model asynchronously.
        
        Args:
            prompt: The string prompt to send to the model.
            **kwargs: Extra parameters to forward to the API (e.g., temperature, max_tokens).
            
        Returns:
            A standard dictionary matching the structure:
            {
                "content": str,
                "usage": {
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "total_tokens": int
                }
            }
        """
        pass
