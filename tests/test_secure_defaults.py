"""GENIUS_SECURE_DEFAULTS production profile (ag_core.security_profile).

Off by default: the checker returns no violations no matter what else is
unset, so every existing deployment (and the whole test suite) is unchanged.
On: fail-open conveniences must be explicitly overridden or startup is
refused — by EVERY entrypoint (serve.py, mcp_server.py, the orchestrator CLI,
dashboard.py, control_panel.py), not just serve.py. The pinned paths must be
absolute and existing, and the flag itself must come from the real process
environment — never from a loaded .env.
"""

import os
import subprocess
import sys

import pytest

from ag_core.security_profile import (
    enforce_secure_defaults,
    secure_defaults_enabled,
    secure_defaults_violations,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "GENIUS_SECURE_DEFAULTS",
        "GENIUS_CONFIG_PATH",
        "GENIUS_ENV_FILE",
        "GENIUS_HUB_ADMIN_KEY",
        "GENIUS_HUB_TLS",
        "GENIUS_ALLOW_PLAINTEXT",
    ):
        monkeypatch.delenv(var, raising=False)


def _violations(distributed):
    # Declare the CURRENT (monkeypatched) environment to be the real process
    # environment, so each check is exercised in isolation without tripping
    # the bootstrap-source check (which gets its own tests below).
    return secure_defaults_violations(
        distributed=distributed, original_env=dict(os.environ)
    )


def _trusted_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("app:\n  name: Prod\n", encoding="utf-8")
    return str(cfg)


def test_off_by_default_is_a_noop_even_with_everything_unset():
    assert secure_defaults_enabled() is False
    assert secure_defaults_violations(distributed=True) == []
    assert secure_defaults_violations(distributed=False) == []


def test_on_and_bare_distributed_flags_all_three(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    problems = _violations(distributed=True)
    joined = "\n".join(problems)
    assert "GENIUS_CONFIG_PATH" in joined
    assert "GENIUS_HUB_ADMIN_KEY" in joined
    assert "plaintext" in joined
    assert len(problems) == 3


def test_on_non_distributed_only_requires_config_path(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    problems = _violations(distributed=False)
    assert len(problems) == 1
    assert "GENIUS_CONFIG_PATH" in problems[0]


def test_fully_configured_distributed_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", _trusted_config(tmp_path))
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("GENIUS_HUB_TLS", "1")
    assert _violations(distributed=True) == []


def test_allow_plaintext_satisfies_the_transport_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", _trusted_config(tmp_path))
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("GENIUS_ALLOW_PLAINTEXT", "1")
    assert _violations(distributed=True) == []


def test_tls_alone_does_not_waive_admin_or_config(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_HUB_TLS", "1")
    problems = _violations(distributed=True)
    joined = "\n".join(problems)
    assert "GENIUS_CONFIG_PATH" in joined
    assert "GENIUS_HUB_ADMIN_KEY" in joined
    assert "plaintext" not in joined


def test_config_path_must_be_absolute(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", "relative/config.yaml")
    problems = _violations(distributed=False)
    assert len(problems) == 1
    assert "absolute" in problems[0]


def test_config_path_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", str(tmp_path / "gone.yaml"))
    problems = _violations(distributed=False)
    assert len(problems) == 1
    assert "missing" in problems[0]


def test_env_file_must_be_absolute_and_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", _trusted_config(tmp_path))

    monkeypatch.setenv("GENIUS_ENV_FILE", "relative.env")
    problems = _violations(distributed=False)
    assert len(problems) == 1
    assert "GENIUS_ENV_FILE" in problems[0]
    assert "absolute" in problems[0]

    monkeypatch.setenv("GENIUS_ENV_FILE", str(tmp_path / "gone.env"))
    problems = _violations(distributed=False)
    assert len(problems) == 1
    assert "GENIUS_ENV_FILE" in problems[0]
    assert "missing" in problems[0]

    pinned = tmp_path / "prod.env"
    pinned.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setenv("GENIUS_ENV_FILE", str(pinned))
    assert _violations(distributed=False) == []


def test_flag_introduced_by_dotenv_is_refused(tmp_path, monkeypatch):
    """The trust-bootstrap check: a workspace .env that itself flips
    GENIUS_SECURE_DEFAULTS (and plants a config path satisfying the pin
    check) must still be refused — the profile cannot bootstrap trust from
    the input it distrusts."""
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", _trusted_config(tmp_path))
    # Baseline WITHOUT the flag = the real process env never had it; it can
    # only have appeared via a loaded .env (or runtime mutation).
    problems = secure_defaults_violations(distributed=False, original_env={})
    assert len(problems) == 1
    assert "real process" in problems[0]


def test_enforce_raises_one_readable_system_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    with pytest.raises(SystemExit) as exc:
        enforce_secure_defaults(distributed=False)
    assert "GENIUS_SECURE_DEFAULTS" in str(exc.value)
    assert "GENIUS_CONFIG_PATH" in str(exc.value)

    # And a clean profile is a no-op. original_env is not injectable through
    # the enforce wrapper, so satisfy the bootstrap-source check by patching
    # the snapshot the checker compares against.
    monkeypatch.setenv("GENIUS_CONFIG_PATH", _trusted_config(tmp_path))
    from ag_core import config as config_mod

    monkeypatch.setattr(config_mod, "_original_env", dict(os.environ))
    enforce_secure_defaults(distributed=False)


def test_serve_refuses_to_start_on_violation(monkeypatch):
    """The one-line serve.py wiring: a violating profile aborts startup."""
    import asyncio

    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setattr("sys.argv", ["serve.py", "--distributed"])
    import serve

    with pytest.raises(SystemExit) as exc:
        asyncio.run(serve.main_async())
    assert "GENIUS_SECURE_DEFAULTS" in str(exc.value)


def test_orchestrator_cli_refuses_on_violation(monkeypatch):
    """Enforcement runs BEFORE argparse: no --prompt on the command line, yet
    the exit message is the profile refusal, not argparse usage."""
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setattr("sys.argv", ["orchestrator.py"])
    import orchestrator

    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert "GENIUS_SECURE_DEFAULTS" in str(exc.value)


def test_control_panel_refuses_on_violation(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    import control_panel

    with pytest.raises(SystemExit) as exc:
        control_panel.main()
    assert "GENIUS_SECURE_DEFAULTS" in str(exc.value)


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param(["mcp_server.py", "stdio"], id="mcp-stdio"),
        pytest.param(["dashboard.py"], id="dashboard"),
    ],
)
def test_entrypoint_subprocess_refuses_on_violation(cmd, tmp_path):
    """mcp_server.py's and dashboard.py's gates live under __main__, so they
    are exercised as real subprocesses. The refusal must reach stderr (on the
    MCP stdio transport stdout stays pure JSON-RPC) with a non-zero exit."""
    env = os.environ.copy()
    env["GENIUS_SECURE_DEFAULTS"] = "1"
    env.pop("GENIUS_CONFIG_PATH", None)
    # Pin a missing .env so the child cannot pick up ambient files regardless
    # of where the suite runs (a missing pin loads nothing, by contract).
    env["GENIUS_ENV_FILE"] = str(tmp_path / "none.env")
    proc = subprocess.run(
        [sys.executable, *cmd],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "GENIUS_SECURE_DEFAULTS" in proc.stderr
    assert "GENIUS_CONFIG_PATH" in proc.stderr
