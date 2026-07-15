"""WAL enablement must be best-effort.

``PRAGMA journal_mode=WAL`` can raise ``database is locked`` when several
processes open the same fresh SQLite file simultaneously (SQLite returns
SQLITE_BUSY without invoking the busy handler on deadlock-prone lock
upgrades) — this used to kill two of three concurrently started agent CLIs
inside VectorMemory init. ``ag_core.utils.db.enable_wal`` retries briefly and
then falls back to the default journal mode instead of failing startup.
"""

import sqlite3
import threading

import pytest

from ag_core.utils.db import enable_wal


class _FlakyConn:
    """conn.execute raises 'database is locked' for the first N WAL attempts."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def execute(self, sql):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return None


def test_enable_wal_retries_through_transient_lock(monkeypatch):
    monkeypatch.setattr("ag_core.utils.db.time.sleep", lambda s: None)
    conn = _FlakyConn(failures=2)
    assert enable_wal(conn, retries=5) is True
    assert conn.calls == 3


def test_enable_wal_gives_up_without_raising(monkeypatch):
    monkeypatch.setattr("ag_core.utils.db.time.sleep", lambda s: None)
    conn = _FlakyConn(failures=100)
    assert enable_wal(conn, retries=4) is False
    assert conn.calls == 4


def test_enable_wal_non_lock_error_is_not_retried(monkeypatch):
    class _BadConn:
        calls = 0

        def execute(self, sql):
            self.calls += 1
            raise sqlite3.OperationalError("unable to open database file")

    conn = _BadConn()
    assert enable_wal(conn, retries=5) is False
    assert conn.calls == 1


def test_vector_memory_init_survives_wal_failure(tmp_path, monkeypatch):
    """VectorMemory's SQLite fallback must fully initialize (tables usable)
    even when WAL can never be enabled."""
    from ag_core.memory import vector_store as vs

    monkeypatch.setattr(vs, "SENTENCE_TRANSFORMERS_AVAILABLE", False)
    monkeypatch.setattr("ag_core.utils.db.enable_wal", lambda conn, retries=5: False)
    mem = vs.VectorMemory(
        collection_name="wal_test",
        db_path=str(tmp_path / "wal_test.db"),
        use_chroma=False,
    )
    doc_id = mem.add("hello world", {"k": "v"})
    results = mem.query("hello", n_results=1)
    assert doc_id
    assert results and results[0]["text"] == "hello world"


@pytest.mark.timeout(60)
def test_concurrent_vector_memory_inits_do_not_crash(tmp_path, monkeypatch):
    """The reproduced failure mode: several processes/threads initializing
    VectorMemory against the same fresh DB at once. All must survive."""
    from ag_core.memory import vector_store as vs

    monkeypatch.setattr(vs, "SENTENCE_TRANSFORMERS_AVAILABLE", False)
    db_path = str(tmp_path / "concurrent.db")
    errors = []
    barrier = threading.Barrier(3)

    def _boot():
        try:
            barrier.wait(timeout=10)
            vs.VectorMemory(
                collection_name="boot",
                db_path=db_path,
                use_chroma=False,
            )
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=_boot) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
