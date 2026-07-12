"""File-type-aware extract_code + fence_hint (ag_core/utils/code_extract.py).

Regression anchor: a real custom-flow job asked the coder for README.md, got
the whole file inside ONE ```python fence with nested ``` blocks, and the old
non-greedy regex truncated the write at the first nested fence (1485 -> 368
bytes) while the job still reported completed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ag_core.utils.code_extract import extract_code, fence_hint  # noqa: E402


# The real failure shape: full README wrapped in a ```python fence, with
# nested ```python and ```bash blocks inside.
README_IN_PYTHON_FENCE = """```python
# linestat

'linestat' reports the most frequent words in a text file.

## Library usage

The utility exposes the following function:

```python
top_words(text, n=3)
```

## Command-line usage

```bash
python linestat.py PATH [N]
```

## Exit codes

- **0**: On success.
```"""


# --- legacy behavior (no filename / .py) is unchanged ------------------------


def test_legacy_prefers_python_block():
    content = "```\nbig untagged log " + "x" * 100 + "\n```\n```python\ncode\n```"
    assert extract_code(content) == "code"


def test_legacy_largest_python_block_wins():
    content = "```python\nshort\n```\n```python\nmuch_longer_code = 1\n```"
    assert extract_code(content) == "much_longer_code = 1"


def test_legacy_no_fence_returns_stripped():
    assert extract_code("  plain code  ") == "plain code"


def test_py_filename_keeps_legacy_behavior():
    content = "```\nlog\n```\n```python\ncode\n```"
    assert extract_code(content, filename="app.py") == "code"


def test_unknown_extension_keeps_legacy_behavior():
    content = "```python\ncode\n```"
    assert extract_code(content, filename="weird.xyz") == "code"


# --- documentation targets: nested fences never truncate ---------------------


def test_markdown_outer_fence_keeps_nested_blocks():
    out = extract_code(README_IN_PYTHON_FENCE, filename="README.md")
    # The old regex cut everything from the first nested fence onward.
    assert out.startswith("# linestat")
    assert "```python\ntop_words(text, n=3)\n```" in out
    assert "```bash" in out
    assert out.endswith("- **0**: On success.")


def test_markdown_without_filename_still_truncates_documenting_legacy():
    # Documents WHY filename matters: the legacy path stops at the first
    # nested fence (this is the pre-fix behavior, kept for .py callers).
    out = extract_code(README_IN_PYTHON_FENCE)
    assert "Exit codes" not in out


def test_markdown_four_backtick_fence_unwrapped():
    content = (
        "````markdown\n# Title\n\n```python\nexample()\n```\n\nTail text.\n````"
    )
    out = extract_code(content, filename="docs/README.md")
    assert out.startswith("# Title")
    assert "```python\nexample()\n```" in out
    assert out.endswith("Tail text.")


def test_markdown_raw_response_returned_verbatim():
    # No outer fence: the response IS the file; inner fences must survive.
    raw = "# Title\n\n```bash\nrun me\n```\n\ndone"
    assert extract_code(raw, filename="README.md") == raw


def test_markdown_windows_line_endings():
    content = "```markdown\r\n# T\r\n\r\n```bash\r\nx\r\n```\r\n\r\nend\r\n```"
    out = extract_code(content, filename="README.md")
    assert "```bash" in out
    assert out.endswith("end")


# --- .py targets: ``` inside string literals must not truncate ---------------

# The real failure shape: a generated test asserting the README contains
# fenced examples — the ``` inside the string literal ended the regex match
# mid-string and every self-heal attempt died on the SyntaxError.
PY_WITH_FENCE_IN_STRING = (
    "```python\n"
    "def test_readme_mentions_fences():\n"
    "    content = open('README.md').read()\n"
    '    assert "```python" in content\n'
    '    assert "```bash" in content\n'
    "```"
)


def test_py_fence_inside_string_literal_recovered():
    out = extract_code(PY_WITH_FENCE_IN_STRING, filename="test_readme.py")
    assert '"```python" in content' in out
    assert out.endswith('assert "```bash" in content')
    import ast

    ast.parse(out)


def test_py_fence_in_string_with_four_backtick_outer():
    content = "````python\nx = '```'\ny = 1\n````"
    out = extract_code(content, filename="mod.py")
    assert out == "x = '```'\ny = 1"


def test_py_legacy_block_choice_unchanged_when_valid():
    # Log fence + python fence: the historical python-preference still wins
    # (the validated-fallback chain only engages when legacy output is
    # broken Python).
    content = "```\nlog " + "x" * 80 + "\n```\n```python\ncode = 1\n```"
    assert extract_code(content, filename="app.py") == "code = 1"


def test_py_unparseable_everything_falls_back_to_legacy():
    content = "```python\ndef broken(((\n```"
    assert extract_code(content, filename="app.py") == "def broken((("


def test_no_filename_keeps_pure_legacy_even_if_broken():
    # Without a filename there is no validator: historical behavior exactly.
    out = extract_code(PY_WITH_FENCE_IN_STRING)
    assert out.endswith('assert "')


# --- config/code targets: prefer the file's own language tag -----------------


def test_yaml_target_prefers_yaml_block_over_python_example():
    content = (
        "```yaml\nname: ci\non: push\n```\n\nUsage example:\n"
        "```python\nprint('this python example must not win')\n```"
    )
    assert extract_code(content, filename=".github/workflows/ci.yml") == (
        "name: ci\non: push"
    )


def test_dockerfile_target_prefers_dockerfile_block():
    content = "```python\nx = 1\n```\n```dockerfile\nFROM python:3.11\n```"
    assert extract_code(content, filename="Dockerfile") == "FROM python:3.11"


def test_yaml_target_falls_back_to_any_block():
    content = "```\nkey: value\n```"
    assert extract_code(content, filename="config.yaml") == "key: value"


def test_yaml_target_no_fence_returns_stripped():
    assert extract_code("key: value\n", filename="config.yaml") == "key: value"


# --- fence_hint ---------------------------------------------------------------


def test_fence_hint_python_matches_historical_wording():
    assert fence_hint("app.py") == "```python fenced block"
    assert fence_hint(None) == "```python fenced block"


def test_fence_hint_markdown_uses_four_backticks():
    hint = fence_hint("README.md")
    assert hint.startswith("````markdown fenced block")
    assert "four backticks" in hint


def test_fence_hint_known_types():
    assert fence_hint("ci.yml") == "```yaml fenced block"
    assert fence_hint("Dockerfile") == "```dockerfile fenced block"


def test_fence_hint_unknown_type():
    assert "fenced block" in fence_hint("data.bin")
