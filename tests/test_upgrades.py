import os
import threading
import json
import time
import uuid
import pytest
import asyncio
import hashlib
import logging
from typing import Any, Dict

from ag_core.utils.security import calculate_checksum, verify_checksum, verify_raw_body_checksum
from ag_core.utils.jwt import encode_jwt, decode_jwt
from ag_core.utils.logger import log_structured
from serve import BoundedPendingTasks
from ag_core.distributed.hub import BoundedTasks
from ag_core.utils.db import init_db, log_agent_start, log_agent_success, log_agent_failure, log_conversation
import sqlite3

def test_checksum_serialization_and_hmac():
    payload = {"b": 2, "a": 1}
    secret = "my_secret"
    
    # Calculate HMAC-SHA256 checksum
    chk = calculate_checksum(payload, secret)
    assert isinstance(chk, str)
    assert len(chk) == 64
    
    # Verification using HMAC-SHA256
    assert verify_checksum(payload, chk, secret)
    
    # Verify plain SHA-256 is NOT accepted anymore (canonical)
    plain_canonical_data = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    plain_canonical_chk = hashlib.sha256(plain_canonical_data).hexdigest()
    assert not verify_checksum(payload, plain_canonical_chk, secret)
    
    # Verify plain SHA-256 is NOT accepted anymore (spaced sort_keys)
    plain_spaced_data = json.dumps(payload, sort_keys=True).encode('utf-8')
    plain_spaced_chk = hashlib.sha256(plain_spaced_data).hexdigest()
    assert not verify_checksum(payload, plain_spaced_chk, secret)

    # Verify plain SHA-256 is NOT accepted anymore (un-sorted/no-space)
    plain_unsorted_nospace_data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    plain_unsorted_nospace_chk = hashlib.sha256(plain_unsorted_nospace_data).hexdigest()
    assert not verify_checksum(payload, plain_unsorted_nospace_chk, secret)

    # Verify plain SHA-256 is NOT accepted anymore (un-sorted/spaced)
    plain_unsorted_spaced_data = json.dumps(payload).encode('utf-8')
    plain_unsorted_spaced_chk = hashlib.sha256(plain_unsorted_spaced_data).hexdigest()
    assert not verify_checksum(payload, plain_unsorted_spaced_chk, secret)


def test_verify_raw_body_checksum():
    body = b"hello world"
    secret = "secret"
    
    # HMAC-SHA256
    import hmac
    hmac_chk = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    is_valid, is_plain = verify_raw_body_checksum(body, hmac_chk, secret)
    assert is_valid
    assert not is_plain
    
    # Plain SHA-256 (should be invalid/rejected)
    plain_chk = hashlib.sha256(body).hexdigest()
    is_valid, is_plain = verify_raw_body_checksum(body, plain_chk, secret)
    assert not is_valid
    
    # Invalid
    is_valid, is_plain = verify_raw_body_checksum(body, "invalid_checksum", secret)
    assert not is_valid


def test_jwt_jti_and_replay_protection():
    secret = "test_secret"
    payload = {"sub": "user123", "exp": time.time() + 10}
    
    # encode_jwt generates jti
    token = encode_jwt(payload, secret)
    parts = token.split('.')
    assert len(parts) == 3
    
    # decode_jwt successfully verifies first time
    decoded = decode_jwt(token, secret)
    assert "jti" in decoded
    
    # second time decode_jwt raises ValueError (replay attack protection)
    with pytest.raises(ValueError, match="replay"):
        decode_jwt(token, secret)


def test_bounded_pending_tasks_cache():
    cache = BoundedPendingTasks()
    
    class MockFuture:
        def __init__(self, done_status=False):
            self._done = done_status
        def done(self):
            return self._done
            
    # Add 10000 completed items
    for i in range(10000):
        cache[f"task_{i}"] = MockFuture(done_status=True)
        
    assert len(cache) == 10000
    
    # Adding a new one should trigger pruning of completed items
    cache["new_task"] = MockFuture(done_status=False)
    # Since all 10000 were completed, they should all be pruned, leaving only 1 item (or very few)
    assert len(cache) == 1
    assert "new_task" in cache


def test_bounded_tasks_cache():
    cache = BoundedTasks()
    
    # Add 10000 completed/failed/running tasks
    for i in range(10000):
        status = "completed" if i % 2 == 0 else "failed"
        cache[f"task_{i}"] = {"status": status}
        
    assert len(cache) == 10000
    
    # Adding a new task triggers eviction of completed/failed tasks
    cache["task_new"] = {"status": "pending"}
    # They should all be evicted
    assert len(cache) == 1
    assert "task_new" in cache


@pytest.fixture
def setup_temp_db_local(tmp_path):
    temp_db = tmp_path / "genius_upgrade_test.db"
    original_db_path = os.environ.get("GENIUS_DB_PATH")
    os.environ["GENIUS_DB_PATH"] = str(temp_db)
    
    import ag_core.utils.db
    original_path_module = ag_core.utils.db.DB_PATH
    ag_core.utils.db.DB_PATH = str(temp_db)
    init_db()
    
    yield temp_db
    
    if original_db_path is not None:
        os.environ["GENIUS_DB_PATH"] = original_db_path
        ag_core.utils.db.DB_PATH = original_db_path
    else:
        os.environ.pop("GENIUS_DB_PATH", None)
        ag_core.utils.db.DB_PATH = original_path_module


def test_sqlite_write_queue(setup_temp_db_local):
    # Test concurrent writes to the database through multiple threads
    # using log_agent_start, log_agent_success, log_agent_failure, log_conversation
    
    threads = []
    
    def worker_run(i):
        log_agent_start(f"task_{i}", f"agent_{i}", f"prompt_{i}")
        log_agent_success(f"task_{i}", f"success_{i}")
        log_conversation(f"user_prompt_{i}", f"ai_response_{i}")
        log_agent_failure(f"failed_task_{i}", f"error_{i}")
        
    for i in range(20):
        t = threading.Thread(target=worker_run, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    # Check database contents
    conn = sqlite3.connect(str(setup_temp_db_local))
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM agent_logs WHERE status = 'success'")
    success_count = cursor.fetchone()[0]
    assert success_count == 20
    
    cursor.execute("SELECT COUNT(*) FROM conversations")
    conversation_count = cursor.fetchone()[0]
    assert conversation_count == 20
    
    conn.close()


def test_structured_logging(caplog):
    with caplog.at_level(logging.INFO):
        log_structured(event_type="test_event", data={"key": "value"})
        
    # Check if logged message is a valid JSON structured log
    found = False
    for record in caplog.records:
        if "test_event" in record.message:
            log_data = json.loads(record.message)
            assert log_data["event_type"] == "test_event"
            assert log_data["data"]["key"] == "value"
            found = True
            break
    assert found
