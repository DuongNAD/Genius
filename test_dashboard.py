import os
import sqlite3
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# Force environment variable before importing dashboard
@pytest.fixture(autouse=True)
def setup_temp_db(tmp_path):
    temp_db = tmp_path / "genius_test_dashboard.db"
    original_db_path = os.environ.get("GENIUS_DB_PATH")
    os.environ["GENIUS_DB_PATH"] = str(temp_db)
    
    import ag_core.utils.db
    ag_core.utils.db.DB_PATH = str(temp_db)
    ag_core.utils.db.init_db()
    
    import dashboard
    dashboard.init_db()
    
    yield temp_db
    
    if original_db_path is not None:
        os.environ["GENIUS_DB_PATH"] = original_db_path
        ag_core.utils.db.DB_PATH = original_db_path
    else:
        os.environ.pop("GENIUS_DB_PATH", None)
        ag_core.utils.db.DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "genius.db"))

from dashboard import app
from ag_core.utils.db import get_db_connection

def test_get_root():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Genius Administrative Dashboard" in response.text

def test_api_status():
    client = TestClient(app)
    
    # Insert a log to make grok 'busy'
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agent_logs (task_id, agent_name, prompt, status) VALUES (?, ?, ?, ?)",
            ("task-1", "grok_researcher", "Test Grok prompt", "processing")
        )
        # Insert an idle/completed log for Claude to verify it doesn't report busy
        cursor.execute(
            "INSERT INTO agent_logs (task_id, agent_name, prompt, status) VALUES (?, ?, ?, ?)",
            ("task-2", "claude_architect", "Test Claude prompt", "success")
        )
        conn.commit()
        
    with patch("dashboard.check_port") as mock_check_port:
        # Mock port 8001 (grok) and 8002 (claude) online, others offline
        mock_check_port.side_effect = lambda host, port: port in [8001, 8002]
        
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        
        # Verify Grok
        assert data["grok"]["port"] == 8001
        assert data["grok"]["online"] is True
        assert data["grok"]["status"] == "busy"
        
        # Verify Claude
        assert data["claude"]["port"] == 8002
        assert data["claude"]["online"] is True
        assert data["claude"]["status"] == "idle"
        
        # Verify Codex (offline, idle)
        assert data["codex"]["port"] == 8003
        assert data["codex"]["online"] is False
        assert data["codex"]["status"] == "idle"

def test_api_conversations():
    client = TestClient(app)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO conversations (prompt, result) VALUES (?, ?)",
            ("Conversations Prompt 1", "Conversations Result 1")
        )
        cursor.execute(
            "INSERT INTO conversations (prompt, result) VALUES (?, ?)",
            ("Conversations Prompt 2", "Conversations Result 2")
        )
        conn.commit()
        
    response = client.get("/api/conversations")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    # Ordered DESC by default, so ID 2 should be first
    assert data[0]["prompt"] == "Conversations Prompt 2"
    assert data[0]["result"] == "Conversations Result 2"
    assert data[1]["prompt"] == "Conversations Prompt 1"
    assert data[1]["result"] == "Conversations Result 1"

def test_api_logs():
    client = TestClient(app)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agent_logs (task_id, agent_name, prompt, result, status) VALUES (?, ?, ?, ?, ?)",
            ("task-100", "codex_reviewer", "Review prompt", "Review result", "success")
        )
        cursor.execute(
            "INSERT INTO agent_logs (task_id, agent_name, prompt, error, status) VALUES (?, ?, ?, ?, ?)",
            ("task-200", "tester_agent", "Test prompt", "Compilation error", "failure")
        )
        conn.commit()
        
    response = client.get("/api/logs")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    
    # Ordered DESC, so task-200 is first
    assert data[0]["task_id"] == "task-200"
    assert data[0]["agent_name"] == "tester_agent"
    assert data[0]["status"] == "failure"
    assert data[0]["error"] == "Compilation error"
    
    assert data[1]["task_id"] == "task-100"
    assert data[1]["agent_name"] == "codex_reviewer"
    assert data[1]["status"] == "success"
    assert data[1]["result"] == "Review result"
