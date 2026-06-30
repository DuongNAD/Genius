import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ag_core.skill_app import create_skill_app

app = create_skill_app("codex")
