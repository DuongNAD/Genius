# test_adversarial_challenger_m3_2.py
import os
import sqlite3
import json
import pytest
import time
import threading
from unittest.mock import patch, MagicMock

from ag_core.memory.vector_store import SimpleTFIDFEmbedding, VectorMemory
import orchestrator

# --- 1. Bounds Checks on vector_dim ---

def test_vector_dim_bounds_checks():
    """Verify that vector_dim bounds checks throw early ValueError/TypeError on invalid dimensions."""
    # Zero or negative integers must throw ValueError
    with pytest.raises(ValueError, match="vector_dim must be greater than 0"):
        SimpleTFIDFEmbedding(vector_dim=0)
    with pytest.raises(ValueError, match="vector_dim must be greater than 0"):
        SimpleTFIDFEmbedding(vector_dim=-128)

    # Non-integers (float, string, list, dict, bool) must throw TypeError
    with pytest.raises(TypeError, match="vector_dim must be an integer"):
        SimpleTFIDFEmbedding(vector_dim=128.5)
    with pytest.raises(TypeError, match="vector_dim must be an integer"):
        SimpleTFIDFEmbedding(vector_dim="128")
    with pytest.raises(TypeError, match="vector_dim must be an integer"):
        SimpleTFIDFEmbedding(vector_dim=[128])
    with pytest.raises(TypeError, match="vector_dim must be an integer"):
        SimpleTFIDFEmbedding(vector_dim=True)
    with pytest.raises(TypeError, match="vector_dim must be an integer"):
        SimpleTFIDFEmbedding(vector_dim=False)

    # Valid dimension must work
    embedder = SimpleTFIDFEmbedding(vector_dim=64)
    assert embedder.vector_dim == 64


# --- 2. SQLite WAL Concurrency Under Load ---

def test_sqlite_wal_concurrency_load(tmp_path):
    """Verify SQLite WAL concurrency under multi-threaded read/write load."""
    db_file = str(tmp_path / "wal_load.db")
    memory = VectorMemory(collection_name="wal_load", use_chroma=False, db_path=db_file)

    # Force SQLite DB initialization and ensure WAL mode is active
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode;")
    mode = cursor.fetchone()[0]
    conn.close()
    assert mode.lower() == "wal", f"Expected WAL mode, got {mode}"

    errors = []
    num_threads = 20
    iterations_per_thread = 50

    def writer_thread(t_idx):
        for i in range(iterations_per_thread):
            try:
                memory.add(
                    text=f"Thread {t_idx} document {i} with some content",
                    metadata={"thread": t_idx, "iteration": i}
                )
            except Exception as e:
                errors.append((t_idx, "write", type(e).__name__, str(e)))

    def reader_thread(t_idx):
        for i in range(iterations_per_thread):
            try:
                # Query with random variations to hit search score calculations
                res = memory.query(f"Thread {t_idx} document {i}", n_results=5)
                # Just verify it returns list
                assert isinstance(res, list)
            except Exception as e:
                errors.append((t_idx, "read", type(e).__name__, str(e)))

    threads = []
    # Create alternating writer and reader threads to mix locks and reads
    for idx in range(num_threads):
        if idx % 2 == 0:
            threads.append(threading.Thread(target=writer_thread, args=(idx,)))
        else:
            threads.append(threading.Thread(target=reader_thread, args=(idx,)))

    # Start all threads
    for t in threads:
        t.start()

    # Wait for completion
    for t in threads:
        t.join()

    # Check for concurrency errors
    assert len(errors) == 0, f"Encountered concurrent access errors: {errors}"


# --- 3. Missing/Unavailable E: Drive Simulation ---

def test_missing_e_drive_simulation():
    """Mock filesystem and sqlite3 functions to assert that no crashes occur when the E: drive is unavailable."""
    # Define a helper function to raise OSError for any path on E: drive
    def mock_fs_raise(path, *args, **kwargs):
        path_str = str(path)
        if "E:" in path_str.upper() or "E:/" in path_str.upper() or "E:\\" in path_str.upper():
            raise OSError(21, "Device not ready (E: drive is simulated as unavailable)")
        return mock_fs_raise.original_exists(path)

    # Save original os.path.exists
    mock_fs_raise.original_exists = os.path.exists

    # We mock:
    # 1. os.makedirs
    # 2. sqlite3.connect
    # 3. builtins.open
    # 4. os.path.exists
    # to raise OSError when accessed with E: drive path

    def mock_makedirs(path, *args, **kwargs):
        path_str = str(path)
        if "E:" in path_str.upper():
            raise OSError(21, "Device not ready (E: drive is simulated as unavailable)")
        return None

    def mock_sqlite_connect(path, *args, **kwargs):
        path_str = str(path)
        if "E:" in path_str.upper():
            raise OSError(21, "Device not ready (E: drive is simulated as unavailable)")
        return sqlite3.connect(":memory:")

    import builtins
    original_open = builtins.open

    def mock_open(file, *args, **kwargs):
        file_str = str(file)
        if "E:" in file_str.upper():
            raise OSError(21, "Device not ready (E: drive is simulated as unavailable)")
        return original_open(file, *args, **kwargs)

    with patch("os.path.exists", side_effect=mock_fs_raise), \
         patch("os.makedirs", side_effect=mock_makedirs), \
         patch("sqlite3.connect", side_effect=mock_sqlite_connect), \
         patch("builtins.open", side_effect=mock_open):

        # Test A: VectorMemory fails cleanly when database is on E:
        with pytest.raises(OSError) as excinfo:
            VectorMemory(collection_name="missing_e", use_chroma=False, db_path="E:\\genius.db")
        assert "Device not ready" in str(excinfo.value)

        # Test B: Orchestrator run_pipeline fails cleanly when workspace is on E:
        # Check that it handles the OS failure cleanly by propagating the OSError rather than crashing/silently passing
        with pytest.raises(OSError) as excinfo_orch:
            # We mock the http client post to bypass actual network requests since we are in CODE_ONLY
            with patch("httpx.AsyncClient.post") as mock_post:
                import asyncio
                asyncio.run(orchestrator.run_pipeline("build my microservice", workspace="E:\\missing_e_workspace"))
        assert "Device not ready" in str(excinfo_orch.value)
