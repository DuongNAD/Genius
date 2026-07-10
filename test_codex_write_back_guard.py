"""Regression test for the reviewer self-heal write-back guard.

The codex reviewer's self-heal loop regenerates the reviewed file and writes it
back. A retry that returns no extractable code (prose, a refusal, or an empty
FallbackProvider result) makes ``extract_code`` return ``""`` — writing that
would truncate the user's source file to zero bytes. ``_safe_write_back`` must
skip the write in that case and leave the original file intact.
"""

from ag_core.agents.codex_reviewer import _safe_write_back
from ag_core.utils.code_extract import extract_code


def test_empty_content_does_not_truncate_file(tmp_path):
    target = tmp_path / "app.py"
    original = "def add(a, b):\n    return a + b\n"
    target.write_text(original, encoding="utf-8")

    wrote = _safe_write_back(str(target), "")

    assert wrote is False
    assert target.read_text(encoding="utf-8") == original  # NOT truncated


def test_whitespace_only_content_does_not_truncate_file(tmp_path):
    target = tmp_path / "app.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")

    assert _safe_write_back(str(target), "   \n\t  ") is False
    assert target.read_text(encoding="utf-8") == original


def test_real_code_is_written(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old contents\n", encoding="utf-8")
    new_code = "def add(a, b):\n    return a + b\n"

    assert _safe_write_back(str(target), new_code) is True
    assert target.read_text(encoding="utf-8") == new_code


def test_code_less_model_reply_is_the_destructive_case(tmp_path):
    # The real failure mode end-to-end: an empty model reply yields "" from
    # extract_code, and that must not destroy the reviewed file.
    assert extract_code("") == ""

    target = tmp_path / "service.py"
    original = "def handler():\n    return 200\n"
    target.write_text(original, encoding="utf-8")

    _safe_write_back(str(target), extract_code(""))

    assert target.read_text(encoding="utf-8") == original
