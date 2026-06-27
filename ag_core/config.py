import os
import yaml
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv

# Find and load .env robustly
def _load_env():
    curr_dir = os.path.abspath(os.getcwd())
    while curr_dir:
        temp_path = os.path.join(curr_dir, ".env")
        if os.path.exists(temp_path):
            load_dotenv(temp_path)
            return
        parent = os.path.dirname(curr_dir)
        if parent == curr_dir:
            break
        curr_dir = parent
    fallback_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(fallback_env):
        load_dotenv(fallback_env)
    else:
        load_dotenv()

_load_env()

class AppConfig(BaseModel):
    name: str = "Antigravity Core"
    version: str = "2.0"

class ModelsConfig(BaseModel):
    openai: str = "gpt-4o"
    anthropic: str = "claude-3-5-sonnet"
    grok: str = "grok-2"

class ScannerConfig(BaseModel):
    chunk_size_limit: int = 8000
    exclude_patterns: List[str] = Field(
        default_factory=lambda: [".git/", "node_modules/", "venv/", ".venv/", ".pytest_cache/"]
    )

class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    
    # Credentials injected directly from OS Environment
    openai_api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_api_key: str = Field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    grok_api_key: str = Field(default_factory=lambda: os.getenv("GROK_API_KEY", ""))

def load_config(config_path: str = "config.yaml") -> Config:
    """Reads YAML config and binds it alongside environmental secrets into Pydantic models."""
    actual_path = config_path
    if not os.path.isabs(actual_path):
        curr_dir = os.path.abspath(os.getcwd())
        found = False
        while curr_dir:
            temp_path = os.path.join(curr_dir, config_path)
            if os.path.exists(temp_path):
                actual_path = temp_path
                found = True
                break
            parent = os.path.dirname(curr_dir)
            if parent == curr_dir:
                break
            curr_dir = parent
        if not found:
            fallback_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), config_path)
            if os.path.exists(fallback_path):
                actual_path = fallback_path

    yaml_data = {}
    if os.path.exists(actual_path):
        try:
            with open(actual_path, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Failed to load config file ({e}). Using defaults.")

    return Config(**yaml_data)
