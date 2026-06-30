import re


def extract_code(text: str) -> str:
    """Extract fenced code blocks from LLM output.

    Returns the concatenated contents of all ```...``` fenced blocks, or the
    stripped raw text when no fenced block is present.
    """
    blocks = re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)\n```", text, re.DOTALL)
    if blocks:
        return "\n".join(blocks).strip()
    return text.strip()
