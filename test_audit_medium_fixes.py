"""Regression tests for the smaller medium-severity audit fixes.

- repo_graph/_norm and code_parse: strip only a leading "./" prefix, never
  every leading '.'/'/', so paths under a dotfile directory survive.
- config.load_config: a malformed service_registry.json is logged (not
  swallowed by a bare except) and falls back to defaults without crashing.
"""

import logging

from ag_core.scanner.repo_graph import _norm


def test_norm_strips_only_dot_slash_prefix():
    assert _norm("./a/b.py") == "a/b.py"
    assert _norm("a/b.py") == "a/b.py"
    assert _norm("./.github/x.py") == ".github/x.py"
    assert _norm("a\\b.py") == "a/b.py"  # windows separator normalized


def test_norm_preserves_dotfile_directory():
    # The old lstrip("./") mangled this to "github/workflows/ci.py" and dropped
    # the file from the import graph.
    assert _norm(".github/workflows/ci.py") == ".github/workflows/ci.py"
    assert _norm(".config/settings.py") == ".config/settings.py"


def test_malformed_service_registry_is_logged_and_non_fatal(
    tmp_path, monkeypatch, caplog
):
    reg = tmp_path / "registry.json"
    reg.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", str(reg))

    from ag_core.config import load_config

    with caplog.at_level(logging.WARNING, logger="ag_core.config"):
        cfg = load_config()  # must not raise

    assert cfg is not None
    assert any(
        "malformed service registry" in rec.getMessage().lower()
        for rec in caplog.records
    )
