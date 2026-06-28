import os
import sqlite3
from contextlib import contextmanager
from ag_core.utils.logger import logger

DB_PATH = os.environ.get("GENIUS_DB_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "genius.db")))

def get_db_path() -> str:
    """Dynamically resolves the DB_PATH from the environment or module-level fallback."""
    return os.environ.get("GENIUS_DB_PATH", DB_PATH)

def init_db():
    """Initializes the database and creates tables if they do not exist."""
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA auto_vacuum = FULL;")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                prompt TEXT,
                result TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                task_id TEXT,
                agent_name TEXT,
                prompt TEXT,
                result TEXT,
                status TEXT,
                error TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise
    finally:
        conn.close()

@contextmanager
def get_db_connection():
    """Context manager for SQLite connections with timeout and WAL mode enabled."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        yield conn
    finally:
        conn.close()

def log_agent_start(task_id: str, agent_name: str, prompt: str):
    """Logs the start of an agent execution."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO agent_logs (task_id, agent_name, prompt, status) VALUES (?, ?, ?, ?)",
                (task_id, agent_name, prompt, "started")
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error logging agent start for task {task_id}: {e}")

def log_agent_success(task_id: str, result: str):
    """Logs the success of an agent execution."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_logs SET status = ?, result = ? WHERE task_id = ?",
                ("success", result, task_id)
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO agent_logs (task_id, status, result) VALUES (?, ?, ?)",
                    (task_id, "success", result)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Error logging agent success for task {task_id}: {e}")

def log_agent_failure(task_id: str, error: str):
    """Logs the failure of an agent execution."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_logs SET status = ?, error = ? WHERE task_id = ?",
                ("failure", error, task_id)
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO agent_logs (task_id, status, error) VALUES (?, ?, ?)",
                    (task_id, "failure", error)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Error logging agent failure for task {task_id}: {e}")

def log_conversation(prompt: str, result: str):
    """Logs an overall conversation history."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO conversations (prompt, result) VALUES (?, ?)",
                (prompt, result)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error logging conversation: {e}")
