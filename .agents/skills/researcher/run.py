import asyncio
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ag_core.skill_app import build_agent


def main():
    prompt = " ".join(sys.argv[1:])
    agent = build_agent("researcher", stateless=False)
    asyncio.run(agent.run(prompt=prompt))


if __name__ == "__main__":
    main()
