import re


def extract_code(content: str) -> str:
    """Extract the source code from an LLM response that may wrap it in fenced
    code blocks.

    Tolerates a missing trailing newline before the closing fence and extra
    language-tag characters. Returns the single LARGEST block instead of
    concatenating every block — joining example/usage/shell blocks together
    produces a syntactically broken source file. Falls back to the stripped
    content when no fenced block is present.
    """
    blocks = re.findall(
        r"```[a-zA-Z0-9_+.\-]*[ \t]*\r?\n(.*?)\r?\n?```", content, re.DOTALL
    )
    if blocks:
        return max((b.strip() for b in blocks), key=len)
    return content.strip()
