"""Regression tests for ag_core.utils.cli_resolver.which_external.

These guard the production failure that the mock-CLI test suite never exercised:
on Windows ``shutil.which`` searches the current directory first, so running the
providers from the repo root resolved the bundled wrappers
(``grok.cmd``/``claude.cmd``/``codex.cmd``) instead of the real vendor CLI. That
wrapper re-enters the agent (wrapper -> run.py -> agent -> provider -> wrapper):
an infinite recursion that also fails immediately when ``python`` is absent from
PATH. ``which_external`` must always skip any match inside the repo root.
"""

import os
from unittest.mock import patch

from ag_core.utils.cli_resolver import which_external, _within, _REPO_ROOT


def test_within_flags_repo_paths_but_not_external_ones():
    assert _within(os.path.join(_REPO_ROOT, "grok.cmd"), _REPO_ROOT)
    assert _within(_REPO_ROOT, _REPO_ROOT)
    assert not _within(os.path.join(os.sep + "usr", "local", "bin", "grok"), _REPO_ROOT)


def test_external_match_passes_through_unchanged():
    # A genuine, non-repo match from shutil.which must be returned as-is.
    with patch("shutil.which", return_value="/usr/local/bin/grok"):
        assert which_external("grok") == "/usr/local/bin/grok"


def test_repo_wrapper_is_skipped_and_real_cli_on_path_wins(tmp_path):
    # Reproduce the Windows collision: shutil.which finds the repo wrapper first,
    # while the genuine CLI lives in a real PATH directory.
    wrapper = os.path.join(_REPO_ROOT, "grok.cmd")
    real_dir = tmp_path / "realbin"
    real_dir.mkdir()
    real_name = "grok.exe" if os.name == "nt" else "grok"
    real_cli = real_dir / real_name
    real_cli.write_text("")

    env = {"PATH": str(real_dir), "PATHEXT": ".EXE;.CMD;.BAT"}
    with patch("shutil.which", return_value=wrapper):
        with patch.dict(os.environ, env, clear=False):
            got = which_external("grok")

    assert got is not None
    assert os.path.normcase(os.path.abspath(got)) == os.path.normcase(
        os.path.abspath(str(real_cli))
    )
    # And never the bundled wrapper.
    assert not _within(got, _REPO_ROOT)


def test_repo_wrapper_skipped_returns_none_when_no_real_cli(tmp_path):
    # codex genuinely uninstalled: skipping the wrapper must yield None (so the
    # caller falls back to its vendor-specific install globs), not the wrapper.
    wrapper = os.path.join(_REPO_ROOT, "codex.cmd")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with patch("shutil.which", return_value=wrapper):
        with patch.dict(os.environ, {"PATH": str(empty_dir)}, clear=False):
            assert which_external("codex") is None
