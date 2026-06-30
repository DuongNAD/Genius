"""Tests for the hardened extract_code parser used to materialize agent output
into source files (Phase 2 B3)."""

from orchestrator import extract_code


def test_single_block():
    assert (
        extract_code("```python\ndef f():\n    return 1\n```")
        == "def f():\n    return 1"
    )


def test_returns_largest_block_not_joined():
    # An example/usage block plus the real implementation must not be glued
    # together into one broken file — the largest block wins.
    content = (
        "Here is usage:\n```python\nf()\n```\n"
        "And the implementation:\n```python\ndef f():\n    return 42\n    # real impl\n```"
    )
    result = extract_code(content)
    assert result == "def f():\n    return 42\n    # real impl"
    assert (
        "f()\n" not in result.splitlines()[0]
    )  # the tiny usage block is not prepended


def test_tolerates_missing_trailing_newline():
    assert extract_code("```python\ndef f(): pass```") == "def f(): pass"


def test_tolerates_language_tag_variations():
    assert extract_code("```py\nx = 1\n```") == "x = 1"
    assert extract_code("```\nraw = 1\n```") == "raw = 1"


def test_no_fence_falls_back_to_raw():
    assert extract_code("def f():\n    return 1") == "def f():\n    return 1"
