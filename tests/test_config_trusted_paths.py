"""Explicit trusted config paths.

``GENIUS_CONFIG_PATH`` / ``GENIUS_ENV_FILE`` pin exactly which config.yaml /
.env ``ag_core.config`` loads and disable the cwd-upward walk — running Genius
inside an untrusted repo must not let that repo's files reconfigure provider
paths, service URLs, or behavior toggles. Unset, the legacy walk is unchanged.
"""

import os

from ag_core import config as config_mod


def test_genius_config_path_pins_the_yaml(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted.yaml"
    trusted.write_text("app:\n  name: Pinned\n", encoding="utf-8")
    # A config.yaml in the cwd that the walk WOULD otherwise pick up.
    hostile_dir = tmp_path / "hostile"
    hostile_dir.mkdir()
    (hostile_dir / "config.yaml").write_text(
        "app:\n  name: Hostile\n", encoding="utf-8"
    )
    monkeypatch.chdir(hostile_dir)
    monkeypatch.setenv("GENIUS_CONFIG_PATH", str(trusted))
    cfg = config_mod.load_config()
    assert cfg.app.name == "Pinned"


def test_without_env_pin_the_walk_still_finds_cwd_config(tmp_path, monkeypatch):
    walk_dir = tmp_path / "walk"
    walk_dir.mkdir()
    (walk_dir / "config.yaml").write_text("app:\n  name: FromCwd\n", encoding="utf-8")
    monkeypatch.chdir(walk_dir)
    monkeypatch.delenv("GENIUS_CONFIG_PATH", raising=False)
    cfg = config_mod.load_config()
    assert cfg.app.name == "FromCwd"


def test_explicit_config_path_argument_wins_over_env(tmp_path, monkeypatch):
    arg_cfg = tmp_path / "arg.yaml"
    arg_cfg.write_text("app:\n  name: FromArg\n", encoding="utf-8")
    env_cfg = tmp_path / "env.yaml"
    env_cfg.write_text("app:\n  name: FromEnv\n", encoding="utf-8")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", str(env_cfg))
    cfg = config_mod.load_config(str(arg_cfg))
    assert cfg.app.name == "FromArg"


def test_service_registry_rejects_invalid_ports(tmp_path, monkeypatch):
    """Registry values are TCP ports: junk/out-of-range/float entries are
    ignored (with a warning) instead of smuggling arbitrary URL suffixes into
    service destinations or silently truncating."""
    import json

    registry = tmp_path / "service_registry.json"
    registry.write_text(
        json.dumps(
            {
                "claude": 9002,  # valid -> applied
                "codex": "not-a-port",  # junk -> ignored
                "tester": 99999,  # out of range -> ignored
                "security": 8005.5,  # float -> ignored, never truncated
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", str(registry))
    cfg = config_mod.load_config()
    assert cfg.services.claude_architect == "http://localhost:9002/claude"
    # The rejected entries keep the (pytest-rewritten) defaults.
    assert cfg.services.codex_reviewer == "http://localhost:8003/codex"
    assert cfg.services.tester_agent == "http://localhost:8004/tester"
    assert cfg.services.security_agent == "http://localhost:8005/security"


def test_genius_env_file_pins_the_dotenv(tmp_path, monkeypatch):
    # _reload_env_safely returns early under pytest; force the production path.
    monkeypatch.setattr(config_mod, "under_pytest", lambda: False)
    trusted_env = tmp_path / "trusted.env"
    trusted_env.write_text("GENIUS_TEST_TRUSTED_VAR=from-trusted\n", encoding="utf-8")
    hostile_dir = tmp_path / "hostile"
    hostile_dir.mkdir()
    (hostile_dir / ".env").write_text(
        "GENIUS_TEST_TRUSTED_VAR=from-hostile\n", encoding="utf-8"
    )
    monkeypatch.chdir(hostile_dir)
    monkeypatch.setenv("GENIUS_ENV_FILE", str(trusted_env))
    monkeypatch.delenv("GENIUS_TEST_TRUSTED_VAR", raising=False)
    try:
        config_mod._reload_env_safely()
        assert os.environ.get("GENIUS_TEST_TRUSTED_VAR") == "from-trusted"
    finally:
        os.environ.pop("GENIUS_TEST_TRUSTED_VAR", None)


def test_genius_env_file_missing_loads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "under_pytest", lambda: False)
    hostile_dir = tmp_path / "hostile2"
    hostile_dir.mkdir()
    (hostile_dir / ".env").write_text(
        "GENIUS_TEST_TRUSTED_VAR2=from-hostile\n", encoding="utf-8"
    )
    monkeypatch.chdir(hostile_dir)
    monkeypatch.setenv("GENIUS_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    monkeypatch.delenv("GENIUS_TEST_TRUSTED_VAR2", raising=False)
    try:
        config_mod._reload_env_safely()
        assert "GENIUS_TEST_TRUSTED_VAR2" not in os.environ
    finally:
        os.environ.pop("GENIUS_TEST_TRUSTED_VAR2", None)
