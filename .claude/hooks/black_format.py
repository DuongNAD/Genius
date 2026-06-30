"""PostToolUse hook: run black on a .py file Claude just wrote or edited.

Reads the hook JSON from stdin, extracts the edited file path, and formats it
with black if it is a Python file. Always exits 0 so it can never block edits;
no-ops silently when black is not installed.
"""
import json
import subprocess
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response") or {}
    path = tool_input.get("file_path") or tool_response.get("filePath")

    if not path or not path.endswith(".py"):
        return 0

    try:
        subprocess.run(
            [sys.executable, "-m", "black", path],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
