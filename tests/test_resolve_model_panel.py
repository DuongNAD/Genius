"""provider_factory.resolve_model — the SINGLE model-resolution path — and the
control panel reporting through it (P2 fix).

The panel used to re-implement only the per-backend half of the resolution
(GENIUS_MODEL_<BACKEND> / config.models.<attr>) and reported gemini-3.5-flash
for the researcher while the runtime honored the per-role
GENIUS_MODEL_ROLE_RESEARCHER pin. Now build_backend and the panel both call
resolve_model, so the UI can no longer disagree with the runtime — including
the foreign-family veto (a gemini pin must not show up on the claude
fallback's row, because build_backend refuses to serve it there).
"""

from types import SimpleNamespace

import pytest

from ag_core.config import load_config
from ag_core.provider_factory import build_backend, resolve_model

_MODEL_ENV_VARS = (
    "GENIUS_MODEL_AGY",
    "GENIUS_MODEL_CLAUDE",
    "GENIUS_MODEL_CODEX",
    "GENIUS_MODEL_ROLE_RESEARCHER",
    "GENIUS_MODEL_ROLE_CLAUDE",
    "GENIUS_MODEL_ROLE_CODEX",
    "GENIUS_MODEL_ROLE_TESTER",
    "GENIUS_MODEL_ROLE_SECURITY",
    "GENIUS_MODEL_ROLE_DEVOPS",
)

ROLE_PIN = "Gemini 3.1 Pro (High)"  # the agy 1.1.2 display-name model id


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_per_role_pin_wins_for_its_backend(monkeypatch):
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", ROLE_PIN)
    cfg = load_config()
    assert resolve_model("agy", cfg, role="researcher") == ROLE_PIN
    # Other roles on the same backend keep the per-backend resolution.
    assert resolve_model("agy", cfg, role="codex") == (cfg.models.agy or "")


def test_foreign_family_pin_vetoed_on_other_backends(monkeypatch):
    # The researcher's gemini pin must never leak into its claude fallback:
    # claude runs its own model, exactly as build_backend behaves at runtime.
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", ROLE_PIN)
    cfg = load_config()
    assert resolve_model("claude", cfg, role="researcher") == cfg.models.anthropic
    assert resolve_model("codex", cfg, role="researcher") == cfg.models.openai


def test_build_backend_and_resolve_model_agree(monkeypatch):
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", ROLE_PIN)
    cfg = load_config()
    for backend, role in (
        ("agy", "researcher"),
        ("claude", "researcher"),
        ("agy", "codex"),
        ("claude", "claude"),
        ("codex", "security"),
    ):
        provider = build_backend(backend, cfg, role=role)
        assert provider.model_name == resolve_model(backend, cfg, role=role), (
            backend,
            role,
        )


def test_legacy_grok_role_alias_resolves(monkeypatch):
    # canonical_role folds the legacy "grok" id into "researcher".
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", ROLE_PIN)
    cfg = load_config()
    assert resolve_model("agy", cfg, role="grok") == ROLE_PIN


def test_panel_reports_the_per_role_model(monkeypatch):
    from control_panel import _model_for

    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", ROLE_PIN)
    cfg = load_config()
    assert _model_for("agy", "researcher", cfg) == ROLE_PIN
    # And the claude fallback row shows claude's own model, not the pin.
    assert _model_for("claude", "researcher", cfg) == cfg.models.anthropic


def test_panel_cli_default_placeholder():
    from control_panel import _model_for

    stub = SimpleNamespace(
        models=SimpleNamespace(agy="", roles=SimpleNamespace())
    )
    assert _model_for("agy", "tester", stub) == "(CLI default)"
