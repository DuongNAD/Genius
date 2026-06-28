import os
import sqlite3
import pytest
from ag_core.utils.db import (
    init_db,
    log_agent_start,
    log_agent_success,
    log_agent_failure,
    log_conversation,
)

@pytest.fixture(autouse=True)
def setup_temp_db(tmp_path):
    # Set the GENIUS_DB_PATH environment variable to a temp file
    temp_db = tmp_path / "genius_test.db"
    original_db_path = os.environ.get("GENIUS_DB_PATH")
    os.environ["GENIUS_DB_PATH"] = str(temp_db)
    
    # Force update DB_PATH in db module
    import ag_core.utils.db
    ag_core.utils.db.DB_PATH = str(temp_db)
    init_db()
    
    yield temp_db
    
    # Restore original environment variable
    if original_db_path is not None:
        os.environ["GENIUS_DB_PATH"] = original_db_path
        ag_core.utils.db.DB_PATH = original_db_path
    else:
        os.environ.pop("GENIUS_DB_PATH", None)
        # Re-resolve default path
        ag_core.utils.db.DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "genius.db"))

def test_db_initialization(setup_temp_db):
    temp_db = setup_temp_db
    
    # Ensure the DB file does not exist before initialization
    if os.path.exists(temp_db):
        os.remove(temp_db)
        
    assert not os.path.exists(temp_db)
    
    # Call init_db and check if the database was created and tables exist
    init_db()
    assert os.path.exists(temp_db)
    
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    assert "conversations" in tables
    assert "agent_logs" in tables
    conn.close()

def test_log_agent_flow(setup_temp_db):
    temp_db = setup_temp_db
    
    task_id = "test-task-123"
    agent_name = "test-agent"
    prompt = "Hello World"
    
    # Log agent start
    log_agent_start(task_id, agent_name, prompt)
    
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM agent_logs WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    # Columns: id, timestamp, task_id, agent_name, prompt, result, status, error
    assert row[2] == task_id
    assert row[3] == agent_name
    assert row[4] == prompt
    assert row[6] == "started"
    
    # Log agent success
    result = "Success output"
    log_agent_success(task_id, result)
    
    cursor.execute("SELECT * FROM agent_logs WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row[6] == "success"
    assert row[5] == result
    
    # Log agent failure
    error = "Some error occurred"
    log_agent_failure(task_id, error)
    
    cursor.execute("SELECT * FROM agent_logs WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row[6] == "failure"
    assert row[7] == error
    
    conn.close()

def test_log_agent_success_without_start(setup_temp_db):
    task_id = "orphan-task-success"
    log_agent_success(task_id, "orphan result")
    
    conn = sqlite3.connect(str(setup_temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM agent_logs WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row[6] == "success"
    assert row[5] == "orphan result"
    conn.close()

def test_log_agent_failure_without_start(setup_temp_db):
    task_id = "orphan-task-failure"
    log_agent_failure(task_id, "orphan error")
    
    conn = sqlite3.connect(str(setup_temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM agent_logs WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row[6] == "failure"
    assert row[7] == "orphan error"
    conn.close()

def test_log_conversation(setup_temp_db):
    prompt = "User prompt"
    result = "AI response"
    log_conversation(prompt, result)
    
    conn = sqlite3.connect(str(setup_temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM conversations")
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][2] == prompt
    assert rows[0][3] == result
    conn.close()
