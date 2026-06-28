import os
import sqlite3
import json
import pytest
import time
import threading
from ag_core.memory.vector_store import SimpleTFIDFEmbedding, VectorMemory

@pytest.fixture
def temp_db_path(tmp_path):
    db_file = tmp_path / "challenger_memory.db"
    return str(db_file)

# --- 1. Edge Case: Empty query/texts, extremely long documents, special characters ---

def test_edge_cases(temp_db_path):
    memory = VectorMemory(collection_name="edges", use_chroma=False, db_path=temp_db_path)
    
    # Empty text
    empty_id = memory.add("", metadata={"type": "empty"})
    assert empty_id is not None
    
    # Query with empty text
    results = memory.query("", n_results=5)
    # Check if querying empty text runs without crash
    assert isinstance(results, list)
    
    # Check that query score for empty is correct (dot product of zero vectors)
    for r in results:
        if r["id"] == empty_id:
            assert r["score"] == 0.0
            
    # Special characters
    special_texts = [
        "😀😃😄😁😆",  # Emoji only
        "こんにちは世界", # Japanese
        "Hello ' OR '1'='1 --", # SQL Injection payload
        "{\"key\": \"val\", [value]: 1}", # JSON-like syntax
        "Line 1\nLine 2\r\nLine 3\tTabbed", # Control characters
        "A" * 10000 # Long run of single char
    ]
    
    ids = []
    for i, t in enumerate(special_texts):
        doc_id = memory.add(t, metadata={"idx": i})
        assert doc_id is not None
        ids.append(doc_id)
        
    # Querying with special characters
    res = memory.query("こんにちは", n_results=1)
    assert len(res) >= 1
    
    # Extremely long document
    long_doc = "word " * 50000  # 50,000 words, ~250KB
    start_time = time.time()
    long_id = memory.add(long_doc, metadata={"type": "long"})
    add_duration = time.time() - start_time
    assert long_id is not None
    print(f"\nExtremely long document added in {add_duration:.4f}s")
    
    start_time = time.time()
    results = memory.query("word", n_results=1)
    query_duration = time.time() - start_time
    print(f"Extremely long document queried in {query_duration:.4f}s")
    assert len(results) == 1
    assert results[0]["id"] == long_id

# --- 2. Scale Testing: Hundreds of documents ---

def test_scale_performance(temp_db_path):
    memory = VectorMemory(collection_name="scale", use_chroma=False, db_path=temp_db_path)
    
    num_docs = 500
    print(f"\nInserting {num_docs} documents for scale testing...")
    
    start_time = time.time()
    for i in range(num_docs):
        text = f"This is document number {i} containing some random words like apple banana cherry and index {i}."
        memory.add(text, metadata={"index": i})
    insert_duration = time.time() - start_time
    print(f"Inserted {num_docs} documents in {insert_duration:.4f}s (avg {(insert_duration/num_docs)*1000:.2f}ms/doc)")
    
    # Query performance
    start_time = time.time()
    results = memory.query("document apple banana index 250", n_results=10)
    query_duration = time.time() - start_time
    print(f"Queried database with {num_docs} docs in {query_duration:.4f}s")
    
    assert len(results) == 10
    best_match_idx = results[0]["metadata"]["index"]
    print(f"Best match index: {best_match_idx} with score: {results[0]['score']}")

# --- 3. Hashing Collisions & Index Bounds ---

def test_hashing_collisions(temp_db_path):
    embedder = SimpleTFIDFEmbedding(vector_dim=128)
    
    # Find two words that collide
    import hashlib
    def get_bin(word):
        word_hash = int(hashlib.md5(word.encode('utf-8')).hexdigest(), 16)
        return word_hash % 128

    buckets = {}
    colliders = []
    for i in range(1000):
        w = f"word{i}"
        b = get_bin(w)
        if b in buckets:
            colliders.append((w, buckets[b], b))
            break
        buckets[b] = w
        
    assert len(colliders) > 0
    w1, w2, bucket = colliders[0]
    print(f"\nFound collision: '{w1}' and '{w2}' both map to index {bucket}")
    
    emb1 = embedder.get_embeddings([w1])[0]
    emb2 = embedder.get_embeddings([w2])[0]
    assert emb1 == emb2
    
    memory = VectorMemory(collection_name="collision", use_chroma=False, db_path=temp_db_path)
    memory.add(w2, doc_id="target")
    results = memory.query(w1, n_results=1)
    assert results[0]["id"] == "target"
    assert pytest.approx(results[0]["score"], rel=1e-5) == 1.0
    print(f"Collision query score verified: {results[0]['score']}")

def test_invalid_vector_dimensions():
    # Test zero vector_dim raises ValueError
    with pytest.raises(ValueError):
        SimpleTFIDFEmbedding(vector_dim=0)
        
    # Test negative vector_dim raises ValueError
    with pytest.raises(ValueError):
        SimpleTFIDFEmbedding(vector_dim=-5)

    # Test non-integer vector_dim raises TypeError
    with pytest.raises(TypeError):
        SimpleTFIDFEmbedding(vector_dim="invalid")
    with pytest.raises(TypeError):
        SimpleTFIDFEmbedding(vector_dim=3.14)

# --- 4. Multithreaded/Concurrent Access & WAL Mode ---

def test_concurrent_access(temp_db_path):
    memory = VectorMemory(collection_name="concurrency", use_chroma=False, db_path=temp_db_path)
    
    conn = sqlite3.connect(temp_db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode;")
    mode = cursor.fetchone()[0]
    conn.close()
    print(f"\nInitial SQLite journal mode (before WAL): {mode}")
    
    errors = []
    num_threads = 10
    iterations_per_thread = 20
    
    def worker_writer(thread_idx):
        for i in range(iterations_per_thread):
            try:
                memory.add(
                    f"Thread {thread_idx} document {i} with unique content",
                    metadata={"thread": thread_idx, "iter": i}
                )
            except Exception as e:
                errors.append((thread_idx, "write", type(e).__name__, str(e)))
                
    def worker_reader(thread_idx):
        for i in range(iterations_per_thread):
            try:
                memory.query(f"Thread {thread_idx} document", n_results=5)
            except Exception as e:
                errors.append((thread_idx, "read", type(e).__name__, str(e)))

    threads = []
    for i in range(num_threads):
        if i % 2 == 0:
            t = threading.Thread(target=worker_writer, args=(i,))
        else:
            t = threading.Thread(target=worker_reader, args=(i,))
        threads.append(t)
        
    start_time = time.time()
    for t in threads:
        t.start()
        
    for t in threads:
        t.join()
    duration = time.time() - start_time
    
    print(f"Concurrent execution of {num_threads} threads took {duration:.4f}s")
    print(f"Number of errors encountered: {len(errors)}")

def test_wal_mode_concurrency_comparison(tmp_path):
    db_default = str(tmp_path / "default.db")
    memory_def = VectorMemory(collection_name="default", use_chroma=False, db_path=db_default)
    
    # Configure comparison default database to DELETE mode explicitly
    conn_def = sqlite3.connect(db_default)
    conn_def.execute("PRAGMA journal_mode=DELETE;")
    conn_def.commit()
    conn_def.close()
    
    barrier = threading.Barrier(2)
    reader_results = []
    reader_errors = []
    
    def write_slow():
        conn = sqlite3.connect(db_default)
        try:
            conn.execute("BEGIN IMMEDIATE TRANSACTION;")
            conn.execute(
                "INSERT INTO agent_vector_memory_fallback (id, collection_name, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                ("slow-id", "default", "slow text", "{}", "[]")
            )
            barrier.wait()
            time.sleep(1.0)
            conn.commit()
        finally:
            conn.close()
            
    def read_while_writing():
        barrier.wait()
        start = time.time()
        try:
            res = memory_def.query("slow text", n_results=1)
            reader_results.append(res)
        except Exception as e:
            reader_errors.append(e)
        reader_results.append(time.time() - start)
        
    t1 = threading.Thread(target=write_slow)
    t2 = threading.Thread(target=read_while_writing)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    print(f"\n[Default DELETE Mode] Reader duration: {reader_results[-1]:.4f}s, errors: {reader_errors}")
    
    # WAL Mode
    db_wal = str(tmp_path / "wal.db")
    memory_wal = VectorMemory(collection_name="wal", use_chroma=False, db_path=db_wal)
    
    conn = sqlite3.connect(db_wal)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.commit()
    conn.close()
    
    barrier_wal = threading.Barrier(2)
    reader_results_wal = []
    reader_errors_wal = []
    
    def write_slow_wal():
        conn = sqlite3.connect(db_wal)
        try:
            conn.execute("BEGIN IMMEDIATE TRANSACTION;")
            conn.execute(
                "INSERT INTO agent_vector_memory_fallback (id, collection_name, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                ("slow-id-wal", "wal", "slow text wal", "{}", "[]")
            )
            barrier_wal.wait()
            time.sleep(1.0)
            conn.commit()
        finally:
            conn.close()
            
    def read_while_writing_wal():
        barrier_wal.wait()
        start = time.time()
        try:
            res = memory_wal.query("slow text wal", n_results=1)
            reader_results_wal.append(res)
        except Exception as e:
            reader_errors_wal.append(e)
        reader_results_wal.append(time.time() - start)
        
    t1 = threading.Thread(target=write_slow_wal)
    t2 = threading.Thread(target=read_while_writing_wal)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    print(f"[WAL Mode] Reader duration: {reader_results_wal[-1]:.4f}s, errors: {reader_errors_wal}")
    
    # EXCLUSIVE Mode
    db_exclusive = str(tmp_path / "exclusive.db")
    memory_exc = VectorMemory(collection_name="exclusive", use_chroma=False, db_path=db_exclusive)
    
    # Configure comparison exclusive database to DELETE mode explicitly
    conn_exc = sqlite3.connect(db_exclusive)
    conn_exc.execute("PRAGMA journal_mode=DELETE;")
    conn_exc.commit()
    conn_exc.close()
    
    barrier_exc = threading.Barrier(2)
    reader_results_exc = []
    reader_errors_exc = []
    
    def write_exclusive():
        conn = sqlite3.connect(db_exclusive)
        try:
            conn.execute("BEGIN EXCLUSIVE TRANSACTION;")
            conn.execute(
                "INSERT INTO agent_vector_memory_fallback (id, collection_name, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                ("slow-id-exc", "exclusive", "slow text exc", "{}", "[]")
            )
            barrier_exc.wait()
            time.sleep(1.0)
            conn.commit()
        finally:
            conn.close()
            
    def read_while_writing_exc():
        barrier_exc.wait()
        start = time.time()
        try:
            res = memory_exc.query("slow text exc", n_results=1)
            reader_results_exc.append(res)
        except Exception as e:
            reader_errors_exc.append(e)
        reader_results_exc.append(time.time() - start)
        
    t1 = threading.Thread(target=write_exclusive)
    t2 = threading.Thread(target=read_while_writing_exc)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    print(f"[Default DELETE Mode - EXCLUSIVE Write Lock] Reader duration: {reader_results_exc[-1]:.4f}s, errors: {reader_errors_exc}")
    
    # WAL Mode + EXCLUSIVE lock
    db_wal_exc = str(tmp_path / "wal_exc.db")
    memory_wal_exc = VectorMemory(collection_name="wal_exc", use_chroma=False, db_path=db_wal_exc)
    
    conn = sqlite3.connect(db_wal_exc)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.commit()
    conn.close()
    
    barrier_wal_exc = threading.Barrier(2)
    reader_results_wal_exc = []
    reader_errors_wal_exc = []
    
    def write_wal_exclusive():
        conn = sqlite3.connect(db_wal_exc)
        try:
            conn.execute("BEGIN EXCLUSIVE TRANSACTION;")
            conn.execute(
                "INSERT INTO agent_vector_memory_fallback (id, collection_name, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                ("slow-id-wal-exc", "wal_exc", "slow text wal exc", "{}", "[]")
            )
            barrier_wal_exc.wait()
            time.sleep(1.0)
            conn.commit()
        finally:
            conn.close()
            
    def read_while_writing_wal_exc():
        barrier_wal_exc.wait()
        start = time.time()
        try:
            res = memory_wal_exc.query("slow text wal exc", n_results=1)
            reader_results_wal_exc.append(res)
        except Exception as e:
            reader_errors_wal_exc.append(e)
        reader_results_wal_exc.append(time.time() - start)
        
    t1 = threading.Thread(target=write_wal_exclusive)
    t2 = threading.Thread(target=read_while_writing_wal_exc)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    print(f"[WAL Mode - EXCLUSIVE Write Lock] Reader duration: {reader_results_wal_exc[-1]:.4f}s, errors: {reader_errors_wal_exc}")
    
    assert reader_results_exc[-1] >= 0.9, f"Reader in EXCLUSIVE rollback mode didn't block! took {reader_results_exc[-1]}s"
    assert reader_results_wal_exc[-1] < 0.2, f"Reader in WAL mode blocked! took {reader_results_wal_exc[-1]}s"


# --- 5. Filesystem Simulation: Unavailable E: Drive ---

def test_drive_offline_simulation():
    from unittest.mock import patch
    import sqlite3
    from ag_core.utils import db as db_utils
    from ag_core.memory.vector_store import VectorMemory
    
    original_makedirs = os.makedirs
    original_connect = sqlite3.connect

    def mock_makedirs(name, mode=0o777, exist_ok=False):
        if str(name).lower().startswith("e:"):
            raise OSError("[Errno 21] Device not ready: 'E:'")
        return original_makedirs(name, mode, exist_ok)

    def mock_connect(database, *args, **kwargs):
        if str(database).lower().startswith("e:"):
            raise sqlite3.OperationalError("unable to open database file")
        return original_connect(database, *args, **kwargs)

    # 1. Verify db log functions do not crash (they catch exception internally)
    with patch("os.makedirs", side_effect=mock_makedirs), \
         patch("sqlite3.connect", side_effect=mock_connect), \
         patch.dict(os.environ, {"GENIUS_DB_PATH": "E:\\Project\\Genius\\genius.db"}):
         
         # The logging functions catch all exceptions, so they must not raise
         try:
             db_utils.log_agent_start("test-task-offline", "security_agent", "some prompt")
             db_utils.log_agent_success("test-task-offline", "some result")
             db_utils.log_agent_failure("test-task-offline", "some error")
             db_utils.log_conversation("some prompt", "some result")
         except Exception as e:
             pytest.fail(f"Logging functions crashed when E: drive is offline: {e}")
             
         # 2. Verify that if VectorMemory's database is on E: and fails,
         # we can assert that it raises OperationalError or OSError (as expected).
         # We check if we can catch it to prevent crashes in the calling application.
         with pytest.raises((sqlite3.OperationalError, OSError)):
             VectorMemory(collection_name="offline_col", use_chroma=False, db_path="E:\\Project\\Genius\\offline_col.db")

