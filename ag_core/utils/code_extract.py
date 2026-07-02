import re


def extract_code(content: str) -> str:
    """Extract the source code from an LLM response that may wrap it in fenced
    code blocks.

    Tolerates a missing trailing newline before the closing fence and extra
    language-tag characters. Python-tagged blocks (```python / ```py) are
    preferred over untagged/other blocks — agent responses can carry big
    untagged fences (logs, shell output) alongside the real source, and the
    largest-block rule alone would pick the log. Within the preferred set the
    single LARGEST block wins instead of concatenating every block — joining
    example/usage/shell blocks together produces a syntactically broken source
    file. Falls back to the stripped content when no fenced block is present.
    """
    blocks = re.findall(
        r"```([a-zA-Z0-9_+.\-]*)[ \t]*\r?\n(.*?)\r?\n?```", content, re.DOTALL
    )
    if blocks:
        python_blocks = [b for lang, b in blocks if lang.lower() in ("python", "py")]
        pool = python_blocks or [b for _, b in blocks]
        return max((b.strip() for b in pool), key=len)
    return content.strip()
