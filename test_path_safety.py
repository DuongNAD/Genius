"""Tests for the path-safety helpers added to harden the orchestrator against
malicious model-supplied file paths (write-anywhere) and basename collisions."""

import os
import tempfile

import pytest

from orchestrator import safe_join, flatten_rel_path, PipelineError


def test_safe_join_allows_normal_relative_path():
    base = tempfile.gettempdir()
    result = safe_join(base, "src/main.py")
    assert result.startswith(os.path.realpath(base))
    assert result.endswith(os.path.join("src", "main.py"))


@pytest.mark.parametrize(
    "bad",
    [
        "../evil.py",
        "../../etc/passwd",
        "src/../../escape.py",
        "/etc/passwd",
        "C:\\Windows\\system32\\x.py",
        "",
    ],
)
def test_safe_join_rejects_unsafe_paths(bad):
    base = tempfile.gettempdir()
    with pytest.raises(PipelineError):
        safe_join(base, bad)


def test_flatten_rel_path_avoids_basename_collisions():
    # Two files with the same basename in different dirs must flatten differently.
    assert flatten_rel_path("src/a/util.py") == "src_a_util"
    assert flatten_rel_path("src/b/util.py") == "src_b_util"
    assert flatten_rel_path("main.py") == "main"
    assert flatten_rel_path("pkg\\mod\\file.py") == "pkg_mod_file"
