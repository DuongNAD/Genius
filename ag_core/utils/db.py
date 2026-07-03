import asyncio
import os
import sqlite3
import queue
import threading
from contextlib import contextmanager
from ag_core.utils.logger import logger

# `or` (not a get() default) so the blank GENIUS_DB_PATH shipped in
# .env.example (and put into os.environ as "" by python-dotenv) falls back to
# the in-repo default. sqlite3.connect("") opens a fresh temporary database
# per connection, so tables created by init_db would be invisible to every
# later connection (e.g. the seen_jtis anti-replay table used by decode_jwt).
_DEFAULT_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "genius.db")
)
DB_PATH = os.environ.get("GENIUS_DB_PATH") or _DEFAULT_DB_PATH


def get_db_path() -> str:
    """Dynamically resolves the DB_PATH from the environment or module-level fallback."""
    return os.environ.get("GENIUS_DB_PATH") or DB_PATH or _DEFAULT_DB_PATH


def init_db():
    """Initializes the database and creates tables if they do not exist."""
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        # No auto_vacuum pragma: it only applies before the first table exists
        # (silent no-op afterwards), and FULL would move pages on every commit.
        # The pruned tables (seen_jtis) stay bounded by their DELETEs instead.
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                prompt TEXT,
                result TEXT
            )
        """
        )
        cursor.execute(
            """
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
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_jtis (
                jti TEXT PRIMARY KEY,
                exp REAL
            )
        """
        )
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


# --- Single Writer SQLite Thread Queue Implementation ---

_db_write_queue = queue.Queue()
_db_writer_thread = None


class WriteTask:
    def __init__(self, func, args, kwargs, db_path=None):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.db_path = db_path
        self.event = threading.Event()
        self.exception = None
        self.result = None


def _db_writer_worker():
    conn = None
    current_conn_path = None

    while True:
        task = _db_write_queue.get()
        if task is None:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            break

        db_path = task.db_path or get_db_path()
        if conn is None or db_path != current_conn_path:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                conn = sqlite3.connect(db_path, timeout=30.0)
                conn.execute("PRAGMA journal_mode=WAL;")
                current_conn_path = db_path
            except Exception as e:
                conn = None
                current_conn_path = None
                task.exception = e
                task.event.set()
                _db_write_queue.task_done()
                continue

        try:
            task.result = task.func(conn, *task.args, **task.kwargs)
        except Exception as e:
            task.exception = e
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
            current_conn_path = None
        finally:
            task.event.set()
            _db_write_queue.task_done()


def _start_writer_thread():
    global _db_writer_thread
    if _db_writer_thread is None or not _db_writer_thread.is_alive():
        _db_writer_thread = threading.Thread(
            target=_db_writer_worker, daemon=True, name="SQLiteWriterThread"
        )
        _db_writer_thread.start()


def stop_writer_thread():
    global _db_writer_thread
    if _db_writer_thread and _db_writer_thread.is_alive():
        _db_write_queue.put(None)
        _db_writer_thread.join(timeout=2.0)
        _db_writer_thread = None


def _submit_write(func, *args, **kwargs):
    db_path = kwargs.pop("db_path", None)
    _start_writer_thread()
    task = WriteTask(func, args, kwargs, db_path=db_path)
    _db_write_queue.put(task)
    task.event.wait()
    if task.exception:
        raise task.exception
    return task.result


# --- Internal DB Write Implementations ---


def _log_agent_start_impl(conn, task_id: str, agent_name: str, prompt: str):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agent_logs (task_id, agent_name, prompt, status) VALUES (?, ?, ?, ?)",
        (task_id, agent_name, prompt, "started"),
    )
    conn.commit()


def _log_agent_success_impl(conn, task_id: str, result: str):
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE agent_logs SET status = ?, result = ? WHERE task_id = ?",
        ("success", result, task_id),
    )
    if cursor.rowcount == 0:
        cursor.execute(
            "INSERT INTO agent_logs (task_id, status, result) VALUES (?, ?, ?)",
            (task_id, "success", result),
        )
    conn.commit()


def _log_agent_failure_impl(conn, task_id: str, error: str):
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE agent_logs SET status = ?, error = ? WHERE task_id = ?",
        ("failure", error, task_id),
    )
    if cursor.rowcount == 0:
        cursor.execute(
            "INSERT INTO agent_logs (task_id, status, error) VALUES (?, ?, ?)",
            (task_id, "failure", error),
        )
    conn.commit()


def _log_conversation_impl(conn, prompt: str, result: str):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO conversations (prompt, result) VALUES (?, ?)", (prompt, result)
    )
    conn.commit()


# --- Public API Functions ---


def enqueue_db_write(func, *args, **kwargs):
    """Enqueues a database write function to be run by the writer thread."""
    return _submit_write(func, *args, **kwargs)


def log_agent_start(task_id: str, agent_name: str, prompt: str):
    """Logs the start of an agent execution."""
    try:
        _submit_write(_log_agent_start_impl, task_id, agent_name, prompt)
    except Exception as e:
        logger.error(f"Error logging agent start for task {task_id}: {e}")


def log_agent_success(task_id: str, result: str):
    """Logs the success of an agent execution."""
    try:
        _submit_write(_log_agent_success_impl, task_id, result)
    except Exception as e:
        logger.error(f"Error logging agent success for task {task_id}: {e}")


def log_agent_failure(task_id: str, error: str):
    """Logs the failure of an agent execution."""
    try:
        _submit_write(_log_agent_failure_impl, task_id, error)
    except Exception as e:
        logger.error(f"Error logging agent failure for task {task_id}: {e}")


def log_conversation(prompt: str, result: str):
    """Logs an overall conversation history."""
    try:
        _submit_write(_log_conversation_impl, prompt, result)
    except Exception as e:
        logger.error(f"Error logging conversation: {e}")


async def log_conversation_async(prompt: str, result: str):
    """Async wrapper for :func:`log_conversation`. The underlying write blocks
    on the single SQLite writer thread; offload it to a worker thread so async
    pipeline code doesn't stall the event loop (and, in --auto-pilot where the
    servers share the loop, every skill server with it)."""
    await asyncio.to_thread(log_conversation, prompt, result)
