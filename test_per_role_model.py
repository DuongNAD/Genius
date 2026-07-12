"""Per-ROLE model override in build_backend (config.models.roles /
GENIUS_MODEL_ROLE_<ROLE>), precedence over the per-backend model, and
byte-identity when no per-role knob is set."""

from ag_core.provider_factory import build_backend
from ag_core.config import load_config


def test_no_role_knob_is_byte_identical(monkeypatch):
    # Passing a role with NO per-role knob resolves exactly like the old
    # per-backend-only path (and like role=None).
    monkeypatch.setenv("GENIUS_MODEL_AGY", "just-backend")
    assert build_backend("agy", load_config(), role="codex").model_name == "just-backend"
    assert build_backend("agy", load_config(), role="researcher").model_name == "just-backend"
    assert build_backend("agy", load_config()).model_name == "just-backend"


def test_per_role_env_splits_one_backend_into_two_models(monkeypatch):
    # The whole point: codex and researcher both on the agy backend, distinct models.
    monkeypatch.setenv("GENIUS_MODEL_ROLE_CODEX", "gemini-3.5-flash")
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", "gemini-3.1-pro")
    cfg = load_config()
    assert build_backend("agy", cfg, role="codex").model_name == "gemini-3.5-flash"
    assert build_backend("agy", cfg, role="researcher").model_name == "gemini-3.1-pro"


def test_per_role_env_beats_per_backend_env(monkeypatch):
    monkeypatch.setenv("GENIUS_MODEL_AGY", "backend-default")
    monkeypatch.setenv("GENIUS_MODEL_ROLE_CODEX", "role-specific")
    cfg = load_config()
    assert build_backend("agy", cfg, role="codex").model_name == "role-specific"
    # a role with no per-role knob falls through to the per-backend value
    assert build_backend("agy", cfg, role="tester").model_name == "backend-default"


def test_per_role_config_and_env_precedence(monkeypatch):
    cfg = load_config()
    cfg.models.roles.researcher = "cfg-role-model"
    assert build_backend("agy", cfg, role="researcher").model_name == "cfg-role-model"
    # env per-role wins over config per-role
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", "env-role-model")
    assert build_backend("agy", cfg, role="researcher").model_name == "env-role-model"


def test_role_alias_canonicalized(monkeypatch):
    # The legacy 'grok'/'grok_researcher' role ids canonicalize to 'researcher',
    # so the researcher per-role knob still applies.
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", "canon-model")
    cfg = load_config()
    assert build_backend("agy", cfg, role="grok").model_name == "canon-model"


def test_backend_env_still_works_without_role(monkeypatch):
    # Regression: the existing per-backend override is unchanged for role=None.
    monkeypatch.setenv("GENIUS_MODEL_CLAUDE", "claude-fable-5")
    assert build_backend("claude", load_config()).model_name == "claude-fable-5"
    monkeypatch.setenv("GENIUS_MODEL_CLAUDE", "")
    assert build_backend("claude", load_config()).model_name == load_config().models.anthropic


def test_foreign_role_model_never_leaks_into_single_family_backend(monkeypatch):
    """A live fallback drill died because the researcher's role model
    (gemini-3.1-pro, an agy slug) was passed to the CLAUDE fallback backend,
    which 404'd on it — the whole chain failed. A role model from another
    family is ignored on single-family backends (claude/codex/grok) so the
    fallback runs its own default model instead."""
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", "gemini-3.1-pro")
    monkeypatch.setenv("GENIUS_MODEL_CLAUDE", "claude-opus-4-8")
    cfg = load_config()
    # The primary (agy, multi-family CLI) still honours the role pin...
    assert build_backend("agy", cfg, role="researcher").model_name == "gemini-3.1-pro"
    # ...but the claude fallback uses ITS OWN model, not the gemini slug.
    assert (
        build_backend("claude", cfg, role="researcher").model_name
        == "claude-opus-4-8"
    )


def test_matching_role_model_still_applies_to_its_own_backend(monkeypatch):
    cfg = load_config()
    monkeypatch.setenv("GENIUS_MODEL_ROLE_CLAUDE", "claude-opus-4-8")
    assert build_backend("claude", cfg, role="claude").model_name == "claude-opus-4-8"
    monkeypatch.setenv("GENIUS_MODEL_ROLE_SECURITY", "gpt-5.6-sol")
    assert build_backend("codex", cfg, role="security").model_name == "gpt-5.6-sol"


def test_foreign_role_model_skipped_on_codex_backend(monkeypatch):
    monkeypatch.setenv("GENIUS_MODEL_ROLE_SECURITY", "gemini-3.5-flash")
    monkeypatch.setenv("GENIUS_MODEL_CODEX", "gpt-5.6-sol")
    cfg = load_config()
    assert build_backend("codex", cfg, role="security").model_name == "gpt-5.6-sol"
