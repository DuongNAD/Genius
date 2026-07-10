import os
import threading
import time
import yaml
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv

from ag_core.runtime import under_pytest

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
    if under_pytest():
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
    # Empty = the CLI's own default model; a value is passed through to the
    # CLI's model flag (codex -m / claude --model / agy --model). Per-backend
    # env override: GENIUS_MODEL_<BACKEND>.
    openai: str = ""
    anthropic: str = ""
    # grok is an opt-in backend only (kept for GENIUS_PROVIDER_<ROLE>
    # overrides; no default provider chain uses it). Not passed to the CLI.
    grok: str = "grok-2"
    # Antigravity 2.0 (agy CLI / Gemini), the default Researcher primary.
    agy: str = ""
    # NotebookLM (nlm CLI). Opt-in backend only. This is NOT an LLM model name:
    # it holds the default notebook id/alias the provider queries (override at
    # runtime with GENIUS_NOTEBOOKLM_NOTEBOOK or GENIUS_MODEL_NOTEBOOKLM).
    notebooklm: str = ""


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
    # The Researcher service (role id "researcher"; formerly "grok" — the
    # legacy yaml key "grok_researcher" is still accepted by load_config).
    researcher: str = "http://localhost:8001"
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


# load_config() is on several per-request hot paths (checksum middleware, JWT
# verification, response-checksum checks on every 0.5s status poll), and each
# uncached call re-walks the directory tree for .env and config.yaml, re-parses
# the YAML, rebuilds the Pydantic model and re-reads the service registry. A
# short TTL amortizes that without making config effectively static.
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict = {}  # config_path -> (Config, monotonic expiry)
_CONFIG_CACHE_DEFAULT_TTL = 5.0


def _config_cache_ttl() -> float:
    """TTL seconds for the load_config cache.

    ``GENIUS_CONFIG_CACHE_TTL`` overrides (blank = unset, ``0`` disables).
    Always disabled under pytest: conftest re-points SKILL_API_KEY per test
    file and tests write registry/config files expecting fresh reads.
    """
    if under_pytest():
        return 0.0
    raw = os.environ.get("GENIUS_CONFIG_CACHE_TTL")
    if raw:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            pass
    return _CONFIG_CACHE_DEFAULT_TTL


def load_config(config_path: str = "config.yaml") -> Config:
    """Reads YAML config and binds it alongside environmental secrets into
    Pydantic models.

    Results are cached for a few seconds in production (see
    :func:`_config_cache_ttl`); the returned object is shared across callers
    within that window and must be treated as read-only.
    """
    ttl = _config_cache_ttl()
    if ttl > 0:
        with _CONFIG_CACHE_LOCK:
            cached = _CONFIG_CACHE.get(config_path)
            if cached and cached[1] > time.monotonic():
                return cached[0]

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
            # A malformed config.yaml is a real misconfiguration, not something
            # to silently paper over with defaults. Fail fast with an
            # actionable message that names the file and the parse error
            # (the old code printed "Using defaults" then raised — a lie).
            raise RuntimeError(
                f"Failed to parse config file {actual_path}: {e}. "
                f"Fix the YAML syntax (or remove the file to use built-in "
                f"defaults)."
            ) from e

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
        # `if val` (not `is not None`): a blank env value — e.g. the empty
        # SKILL_API_KEY shipped in .env.example and loaded into os.environ as
        # "" by python-dotenv — must NOT clobber a real value set in
        # config.yaml. Treat blank as unset (same idiom as the GENIUS_* vars).
        if val:
            yaml_data[field_name] = val

    # Legacy config.yaml key: the Researcher service used to be
    # "grok_researcher" (role id "grok"); map it to the renamed field.
    services_yaml = yaml_data.get("services")
    if isinstance(services_yaml, dict) and "grok_researcher" in services_yaml:
        services_yaml.setdefault("researcher", services_yaml.pop("grok_researcher"))

    config = Config(**yaml_data)

    if under_pytest():
        config.services.researcher = "http://localhost:8001/researcher"
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
                "researcher": "researcher",
                # Legacy registry keys from before the role rename.
                "grok": "researcher",
                "grok_researcher": "researcher",
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
                    if under_pytest():
                        suffix = f"/{role}"
                    setattr(
                        config.services, field_name, f"http://localhost:{port}{suffix}"
                    )
        except Exception:
            pass

    if ttl > 0:
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[config_path] = (config, time.monotonic() + ttl)

    return config
