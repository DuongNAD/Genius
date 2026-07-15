"""GENIUS_SECURE_DEFAULTS production profile (ag_core.security_profile).

Off by default: the checker returns no violations no matter what else is
unset, so every existing deployment (and the whole test suite) is unchanged.
On: fail-open conveniences must be explicitly overridden or startup is
refused.
"""

import pytest

from ag_core.security_profile import (
    secure_defaults_enabled,
    secure_defaults_violations,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "GENIUS_SECURE_DEFAULTS",
        "GENIUS_CONFIG_PATH",
        "GENIUS_HUB_ADMIN_KEY",
        "GENIUS_HUB_TLS",
        "GENIUS_ALLOW_PLAINTEXT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_off_by_default_is_a_noop_even_with_everything_unset():
    assert secure_defaults_enabled() is False
    assert secure_defaults_violations(distributed=True) == []
    assert secure_defaults_violations(distributed=False) == []


def test_on_and_bare_distributed_flags_all_three(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    problems = secure_defaults_violations(distributed=True)
    joined = "\n".join(problems)
    assert "GENIUS_CONFIG_PATH" in joined
    assert "GENIUS_HUB_ADMIN_KEY" in joined
    assert "plaintext" in joined
    assert len(problems) == 3


def test_on_non_distributed_only_requires_config_path(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    problems = secure_defaults_violations(distributed=False)
    assert len(problems) == 1
    assert "GENIUS_CONFIG_PATH" in problems[0]


def test_fully_configured_distributed_passes(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", "/etc/genius/config.yaml")
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("GENIUS_HUB_TLS", "1")
    assert secure_defaults_violations(distributed=True) == []


def test_allow_plaintext_satisfies_the_transport_gate(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_CONFIG_PATH", "/etc/genius/config.yaml")
    monkeypatch.setenv("GENIUS_HUB_ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("GENIUS_ALLOW_PLAINTEXT", "1")
    assert secure_defaults_violations(distributed=True) == []


def test_tls_alone_does_not_waive_admin_or_config(monkeypatch):
    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setenv("GENIUS_HUB_TLS", "1")
    problems = secure_defaults_violations(distributed=True)
    joined = "\n".join(problems)
    assert "GENIUS_CONFIG_PATH" in joined
    assert "GENIUS_HUB_ADMIN_KEY" in joined
    assert "plaintext" not in joined


def test_serve_refuses_to_start_on_violation(monkeypatch):
    """The one-line serve.py wiring: a violating profile aborts startup."""
    import asyncio

    monkeypatch.setenv("GENIUS_SECURE_DEFAULTS", "1")
    monkeypatch.setattr("sys.argv", ["serve.py", "--distributed"])
    import serve

    with pytest.raises(SystemExit) as exc:
        asyncio.run(serve.main_async())
    assert "GENIUS_SECURE_DEFAULTS" in str(exc.value)
