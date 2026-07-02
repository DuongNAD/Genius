import os
import yaml
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv

_original_env = dict(os.environ)


# Find and load .env robustly
def _load_env():
    curr_dir = os.path.abspath(os.getcwd())
    while curr_dir:
        temp_path = os.path.join(curr_dir, ".env")
        if os.path.exists(temp_path):
            load_dotenv(temp_path, override=False)
            return
        parent = os.path.dirname(curr_dir)
        if parent == curr_dir:
            break
        curr_dir = parent
    fallback_env = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    if os.path.exists(fallback_env):
        load_dotenv(fallback_env, override=False)
    else:
        load_dotenv(override=False)


_load_env()


def _reload_env_safely():
    import sys

    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return
    curr_dir = os.path.abspath(os.getcwd())
    env_path = None
    while curr_dir:
        temp_path = os.path.join(curr_dir, ".env")
        if os.path.exists(temp_path):
            env_path = temp_path
            break
        parent = os.path.dirname(curr_dir)
        if parent == curr_dir:
            break
        curr_dir = parent
    if not env_path:
        fallback_env = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        if os.path.exists(fallback_env):
            env_path = fallback_env

    if env_path and os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    current_val = os.environ.get(k)
                    original_val = _original_env.get(k)
                    if current_val == original_val:
                        os.environ[k] = v


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
        default_factory=lambda: [
            ".git/",
            "node_modules/",
            "venv/",
            ".venv/",
            ".pytest_cache/",
        ]
    )


class ServicesConfig(BaseModel):
    grok_researcher: str = "http://localhost:8001"
    claude_architect: str = "http://localhost:8002"
    codex_reviewer: str = "http://localhost:8003"
    tester_agent: str = "http://localhost:8004"
    security_agent: str = "http://localhost:8005"
    devops_agent: str = "http://localhost:8006"


class MemoryConfig(BaseModel):
    enabled: bool = True
    use_chroma: bool = False
    db_path: str = Field(
        default_factory=lambda: os.environ.get("GENIUS_MEMORY_DB_PATH")
        or os.environ.get("GENIUS_DB_PATH")
        or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "genius.db"
        )
    )
    # `or` (not a get() default) so a blank CHROMA_PERSIST_DIR shipped in
    # .env.example and loaded into os.environ by python-dotenv is treated as
    # unset instead of silently overriding the default with "".
    chroma_persist_dir: str = Field(
        default_factory=lambda: os.getenv("CHROMA_PERSIST_DIR")
        or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".chroma"
        )
    )


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    skill_api_key: str = Field(default_factory=lambda: os.getenv("SKILL_API_KEY", ""))

    # Credentials injected directly from OS Environment
    openai_api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_api_key: str = Field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    grok_api_key: str = Field(default_factory=lambda: os.getenv("GROK_API_KEY", ""))
    git_username: str = Field(default_factory=lambda: os.getenv("GIT_USERNAME", ""))
    git_token: str = Field(default_factory=lambda: os.getenv("GIT_TOKEN", ""))


def load_config(config_path: str = "config.yaml") -> Config:
    """Reads YAML config and binds it alongside environmental secrets into Pydantic models."""
    _reload_env_safely()
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
            fallback_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), config_path
            )
            if os.path.exists(fallback_path):
                actual_path = fallback_path

    yaml_data = {}
    if os.path.exists(actual_path):
        try:
            with open(actual_path, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Failed to load config file ({e}). Using defaults.")
            raise e

    env_keys = {
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "grok_api_key": "GROK_API_KEY",
        "skill_api_key": "SKILL_API_KEY",
        "git_username": "GIT_USERNAME",
        "git_token": "GIT_TOKEN",
    }
    for field_name, env_var in env_keys.items():
        val = os.getenv(env_var)
        if val is not None:
            yaml_data[field_name] = val

    config = Config(**yaml_data)
    import sys

    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        config.services.grok_researcher = "http://localhost:8001/grok"
        config.services.claude_architect = "http://localhost:8002/claude"
        config.services.codex_reviewer = "http://localhost:8003/codex"
        config.services.tester_agent = "http://localhost:8004/tester"
        config.services.security_agent = "http://localhost:8005/security"
        config.services.devops_agent = "http://localhost:8006/devops"
        if not config.skill_api_key:
            config.skill_api_key = ""

    import json

    # `or` (not a get() default) so the blank GENIUS_SERVICE_REGISTRY shipped
    # in .env.example (and put into os.environ as "" by python-dotenv) falls
    # back to the in-repo default instead of silently disabling the registry
    # override (which would break dynamic-port discovery). Mirrors the write
    # side in serve._resolve_registry_path().
    registry_path = os.environ.get("GENIUS_SERVICE_REGISTRY") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".agents",
        "service_registry.json",
    )
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
            role_to_field = {
                "grok": "grok_researcher",
                "claude": "claude_architect",
                "codex": "codex_reviewer",
                "tester": "tester_agent",
                "security": "security_agent",
                "devops": "devops_agent",
            }
            for role, port in registry.items():
                field_name = role_to_field.get(role)
                if field_name and hasattr(config.services, field_name):
                    suffix = ""
                    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
                        suffix = f"/{role}"
                    setattr(
                        config.services, field_name, f"http://localhost:{port}{suffix}"
                    )
        except Exception:
            pass

    return config
