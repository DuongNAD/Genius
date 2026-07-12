import ast
import os
import re

# Fence language tags that count as "matching" for a target-file extension.
# Used to prefer the correctly-tagged block when an agent response carries
# several fences (e.g. a ```yaml artifact next to a ```python usage example).
_EXT_FENCE_LANGS = {
    ".py": ("python", "py"),
    ".md": ("markdown", "md"),
    ".markdown": ("markdown", "md"),
    ".rst": ("rst", "restructuredtext"),
    ".txt": ("text", "txt"),
    ".yml": ("yaml", "yml"),
    ".yaml": ("yaml", "yml"),
    ".json": ("json",),
    ".toml": ("toml",),
    ".ini": ("ini",),
    ".cfg": ("ini", "cfg"),
    ".sh": ("bash", "sh", "shell"),
    ".js": ("javascript", "js"),
    ".ts": ("typescript", "ts"),
    ".html": ("html",),
    ".css": ("css",),
    ".sql": ("sql",),
}
# Extension-less files recognized by basename.
_NAME_FENCE_LANGS = {
    "dockerfile": ("dockerfile", "docker"),
    "makefile": ("makefile", "make"),
}
# Documentation-type targets whose CONTENT routinely contains ``` fences of
# its own: a plain non-greedy fence regex truncates such a file at the first
# nested fence (a real run cut a README.md from 1485 to 368 bytes).
_DOC_EXTS = {".md", ".markdown", ".rst", ".txt"}

_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+.\-]*)[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL
)
# 4+-backtick fence: the unambiguous CommonMark form for wrapping content
# that itself contains ``` fences. Closed by the same backtick run (\1).
_LONG_FENCE_RE = re.compile(
    r"(`{4,})([a-zA-Z0-9_+.\-]*)[ \t]*\r?\n(.*?)\r?\n?\1", re.DOTALL
)


def _fence_langs_for(filename):
    """Matching fence tags for a target file, or None when unknown."""
    base = os.path.basename((filename or "").replace("\\", "/")).lower()
    ext = os.path.splitext(base)[1]
    if ext:
        return _EXT_FENCE_LANGS.get(ext)
    return _NAME_FENCE_LANGS.get(base)


def _largest(blocks):
    return max((b.strip() for b in blocks), key=len)


def _legacy_extract(content: str) -> str:
    """The historical per-block extraction (python-tag preference)."""
    blocks = _FENCE_RE.findall(content)
    if blocks:
        python_blocks = [
            b for lang, b in blocks if lang.lower() in ("python", "py")
        ]
        pool = python_blocks or [b for _, b in blocks]
        return _largest(pool)
    return content.strip()


def _parses_as_python(code: str) -> bool:
    try:
        ast.parse(code)
    except (SyntaxError, ValueError):
        return False
    return True


def _unwrap_outer_fence(content: str):
    """Unwrap a response that IS one outer fence: first line opens it (3+
    backticks), last line is a bare 3+-backtick closer. Returns the body, or
    None. Immune to ``` sequences INSIDE the body (nested doc fences, ``` in
    Python string literals) that truncate per-block regexes."""
    stripped = content.strip()
    lines = stripped.splitlines()
    if (
        len(lines) >= 2
        and lines[0].startswith("```")
        and re.fullmatch(r"`{3,}", lines[-1].strip())
    ):
        return "\n".join(lines[1:-1]).strip()
    return None


def _greedy_fence_capture(content: str):
    """First fence opener to the LAST fence in the response (greedy), or
    None. Tolerates prose around the fence; inner ``` sequences survive."""
    m = re.search(
        r"```[a-zA-Z0-9_+.\-]*[ \t]*\r?\n(.*)\r?\n?```", content, re.DOTALL
    )
    return m.group(1).strip() if m else None


def extract_code(content: str, filename: str = None) -> str:
    """Extract the target-file content from an LLM response that may wrap it
    in fenced code blocks.

    Without ``filename`` (or for ``.py`` targets) this starts from the
    long-standing behavior: tolerate a missing trailing newline before the
    closing fence and extra language-tag characters; prefer Python-tagged
    blocks (```python / ```py) over untagged/other blocks — agent responses
    can carry big untagged fences (logs, shell output) alongside the real
    source, and the largest-block rule alone would pick the log. Within the
    preferred set the single LARGEST block wins instead of concatenating
    every block — joining example/usage/shell blocks together produces a
    syntactically broken source file. Falls back to the stripped content when
    no fenced block is present. For ``.py`` targets specifically, a legacy
    result that does NOT parse triggers wider fence interpretations validated
    with ``ast.parse`` (outer-fence unwrap, greedy capture, 4+-backtick
    fences) — ``` sequences inside string literals otherwise truncate the
    block mid-string.

    With a non-Python ``filename`` the extraction is file-type aware:

    - Blocks tagged for the target's own language (```yaml for .yml, ...)
      are preferred over the Python-tagged ones.
    - A 4+-backtick outer fence (````markdown) is recognized first — the
      form the coder is asked to use for docs whose content nests ``` fences.
    - For documentation targets (.md/.rst/.txt), a response that IS one outer
      fence (first line opens it, last line closes it) is unwrapped as a
      whole, keeping any nested ``` blocks intact; a response with no outer
      fence is returned as-is instead of guessing at an inner block. Both
      protect against the first-nested-fence truncation bug.
    """
    langs = _fence_langs_for(filename) if filename else None
    if langs is None or langs == _EXT_FENCE_LANGS[".py"]:
        legacy = _legacy_extract(content)
        if langs is None:
            # Unknown target: no validator available — byte-identical to the
            # historical behavior every existing caller relies on.
            return legacy
        # Python target: the per-block regex truncates at a ``` sequence
        # INSIDE a string literal — a real run generated README checks like
        # `assert "```python" in content` and every self-heal attempt died on
        # the resulting SyntaxError. Keep the legacy result whenever it
        # parses (byte-identical for every input that ever worked), then try
        # progressively wider fence interpretations; the first candidate that
        # IS valid Python wins, else fall back to the legacy result so the
        # failure mode is unchanged.
        if _parses_as_python(legacy):
            return legacy
        candidates = [
            _unwrap_outer_fence(content),
            _greedy_fence_capture(content),
        ]
        long_blocks = _LONG_FENCE_RE.findall(content)
        if long_blocks:
            matching = [
                b
                for _, lang, b in long_blocks
                if not lang or lang.lower() in ("python", "py")
            ]
            candidates.append(_largest(matching or [b for _, _, b in long_blocks]))
        for candidate in candidates:
            if candidate is not None and _parses_as_python(candidate):
                return candidate
        return legacy

    # Non-Python target. 1) A 4+-backtick fence wins outright.
    long_blocks = _LONG_FENCE_RE.findall(content)
    if long_blocks:
        matching = [
            b for _, lang, b in long_blocks if not lang or lang.lower() in langs
        ]
        return _largest(matching or [b for _, _, b in long_blocks])

    stripped = content.strip()
    is_doc = os.path.splitext(
        os.path.basename((filename or "").replace("\\", "/")).lower()
    )[1] in _DOC_EXTS
    if is_doc:
        # Docs legitimately contain ``` fences, so per-block regexes truncate.
        # Only unwrap when the WHOLE response is one outer fence; otherwise
        # treat the response as the raw file content.
        unwrapped = _unwrap_outer_fence(stripped)
        return unwrapped if unwrapped is not None else stripped

    # Config/code targets (yaml, json, sh, dockerfile, ...): nested fences are
    # implausible, so pick per-block — preferring the target's own language
    # tag so a ```python usage example can never shadow the real artifact.
    blocks = _FENCE_RE.findall(content)
    if blocks:
        matching = [b for lang, b in blocks if lang.lower() in langs]
        return _largest(matching or [b for _, b in blocks])
    return stripped


def fence_hint(filename: str) -> str:
    """The fenced-block phrasing the coder must be told to use for this file.

    Python (and unknown) targets keep the historical "```python fenced block"
    wording; documentation targets get a FOUR-backtick fence so their own
    ``` blocks nest safely; other known types get their own language tag.
    """
    base = os.path.basename((filename or "").replace("\\", "/")).lower()
    ext = os.path.splitext(base)[1]
    if not base or ext == ".py":
        return "```python fenced block"
    if ext in _DOC_EXTS:
        lang = (_EXT_FENCE_LANGS.get(ext) or ("markdown",))[0]
        return (
            f"````{lang} fenced block (four backticks, so the file's own "
            "``` blocks nest safely inside)"
        )
    langs = _fence_langs_for(filename)
    if langs:
        return f"```{langs[0]} fenced block"
    return "``` fenced block (tagged with the file's language)"
