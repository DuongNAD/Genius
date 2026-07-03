"""Tiny process-runtime probes shared across the codebase.

Lives at the ag_core top level (not ag_core.utils) so low-level modules like
``ag_core.config`` can import it without triggering ``ag_core.utils``'s
package ``__init__`` (which imports ``security`` -> ``config`` and would
cycle).
"""

import os
import sys


def under_pytest() -> bool:
    """True when running inside the pytest harness.

    Checks both signals used throughout the repo: the ``pytest`` module being
    imported (in-process test runs) and ``PYTEST_CURRENT_TEST`` (exported to
    subprocesses spawned by a test). Several production behaviors are toggled
    off under pytest for determinism — see .claude/rules/testing.md.
    """
    return "pytest" in sys.modules or bool(os.getenv("PYTEST_CURRENT_TEST"))
