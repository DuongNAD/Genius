---
name: check
description: Run the full quality gate for Genius — the complete pytest suite then flake8 — mirroring CI and the self-healing loop. Use before committing or when asked to verify the code is green.
---

Run the project's quality gate and report results.

1. Ensure tooling is present (black/flake8 are pre-commit-only, not in requirements.txt):
   `pip install flake8` if `flake8` is not found.

2. Run the full test suite exactly as CI does, from the repo root:
   ```
   python -m pytest
   ```
   Tests live in both the repo root (`test_*.py`) and `tests/`. `pytest.ini` already excludes `projects` and `.agents`. Do not pass extra args unless narrowing to a failure.

3. Run the linter:
   ```
   flake8
   ```
   (flake8 uses default settings — there is no config file.)

4. Report a concise summary in Vietnamese (per the repo language rule): pass/fail counts, and for any failure the test id + the key line of the traceback or flake8 message. Do NOT hardcode or fabricate results — run the commands and report what actually happened (see the integrity norm in `TEST_INFRA.md`).

If the user passed arguments, treat them as a pytest node id or `-k` pattern to scope the run (e.g. `/check test_db.py`).
