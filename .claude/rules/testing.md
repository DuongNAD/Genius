---
paths:
  - "test_*.py"
  - "tests/**"
  - "conftest.py"
  - "pytest.ini"
---

# Testing notes

- `pytest.ini` sets `norecursedirs = projects .agents` — generated project output and agent scratch dirs are excluded from collection. Test files live both in the repo root (`test_*.py`) and in `tests/`.
- `conftest.py` pre-seeds mock API keys and sets `SKILL_API_KEY` per test file: most files get `mock-skill-key`, but `test_distributed*`, `*robustness*`, and `*milestone3_adversarial*` get `valid-api-key`.
- Distributed tests exercise the real `CentralHub`/`ClientWorker` through `MockNetworkProtocol` (deterministic latency / dropped-packet / HTTP-error injection) — see `TEST_INFRA.md`.
- `genius.db` and `*.db-wal`/`*.db-shm` are gitignored and can grow large; treat them as disposable local state.
- Two production behaviors are off by default under pytest, for determinism: the Grok↔Claude debate (`--max-debate-rounds` → 0) and the module-level API response cache (re-enable with `ENABLE_GENIUS_CACHE`).
