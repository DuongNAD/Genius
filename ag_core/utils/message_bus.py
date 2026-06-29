import sqlite3
import json
import time
import uuid
import contextlib
import threading
from typing import Any, Dict, Optional, List

class Artifact:
    def __init__(
        self, 
        name: str, 
        content: Any, 
        created_by: str, 
        content_type: str = "text", 
        parent_id: Optional[str] = None, 
        metadata: Optional[Dict] = None,
        artifact_id: Optional[str] = None
    ):
        self.artifact_id = artifact_id or str(uuid.uuid4())
        self.name = name
        self.content = content
        self.created_by = created_by
        if isinstance(content, (dict, list)):
            self.content_type = "json"
        else:
            self.content_type = content_type
        self.timestamp = time.time()
        self.parent_id = parent_id
        self.metadata = metadata or {}

class MessageBus:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self.in_memory_store: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        if db_path:
            self._init_sqlite()

    def _init_sqlite(self):
        with self.lock:
            with contextlib.closing(
                sqlite3.connect(self.db_path, timeout=10.0)
            ) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS artifacts (
                            artifact_id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            content TEXT NOT NULL,
                            content_type TEXT NOT NULL,
                            created_by TEXT NOT NULL,
                            timestamp REAL NOT NULL,
                            parent_id TEXT,
                            metadata TEXT
                        )
                    """)

    def publish(self, artifact: Artifact) -> str:
        """Publishes an artifact. Writes to in-memory store and SQLite if configured."""
        if isinstance(artifact.content, (dict, list)):
            artifact.content_type = "json"

        record = {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "content": artifact.content,
            "content_type": artifact.content_type,
            "created_by": artifact.created_by,
            "timestamp": artifact.timestamp,
            "parent_id": artifact.parent_id,
            "metadata": artifact.metadata
        }
        self.in_memory_store[artifact.artifact_id] = record

        if self.db_path:
            with self.lock:
                while len(self.in_memory_store) > 100:
                    oldest_key = next(iter(self.in_memory_store))
                    self.in_memory_store.pop(oldest_key, None)

                serialized_content = (
                    json.dumps(artifact.content)
                    if artifact.content_type == "json" or isinstance(artifact.content, (dict, list))
                    else str(artifact.content)
                )
                with contextlib.closing(
                    sqlite3.connect(self.db_path, timeout=10.0)
                ) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                artifact.artifact_id,
                                artifact.name,
                                serialized_content,
                                artifact.content_type,
                                artifact.created_by,
                                artifact.timestamp,
                                artifact.parent_id,
                                json.dumps(artifact.metadata)
                            )
                        )
        return artifact.artifact_id

    def retrieve(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves artifact by unique ID."""
        if artifact_id in self.in_memory_store:
            return self.in_memory_store[artifact_id]

        if self.db_path:
            with self.lock:
                with contextlib.closing(
                    sqlite3.connect(self.db_path, timeout=10.0)
                ) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,))
                    row = cursor.fetchone()
                    if row:
                        content = row["content"]
                        if row["content_type"] == "json":
                            try:
                                content = json.loads(content)
                            except Exception:
                                pass
                        return {
                            "artifact_id": row["artifact_id"],
                            "name": row["name"],
                            "content": content,
                            "content_type": row["content_type"],
                            "created_by": row["created_by"],
                            "timestamp": row["timestamp"],
                            "parent_id": row["parent_id"],
                            "metadata": json.loads(row["metadata"]) if row["metadata"] else {}
                        }
        return None

    def retrieve_latest_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieves latest artifact matching a specific key name (e.g. 'design_plan')."""
        matches = [a for a in self.in_memory_store.values() if a["name"] == name]
        if matches:
            return max(matches, key=lambda x: x["timestamp"])

        if self.db_path:
            with self.lock:
                with contextlib.closing(
                    sqlite3.connect(self.db_path, timeout=10.0)
                ) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT * FROM artifacts WHERE name = ? ORDER BY timestamp DESC LIMIT 1", 
                        (name,)
                    )
                    row = cursor.fetchone()
                    if row:
                        content = row["content"]
                        if row["content_type"] == "json":
                            try:
                                content = json.loads(content)
                            except Exception:
                                pass
                        return {
                            "artifact_id": row["artifact_id"],
                            "name": row["name"],
                            "content": content,
                            "content_type": row["content_type"],
                            "created_by": row["created_by"],
                            "timestamp": row["timestamp"],
                            "parent_id": row["parent_id"],
                            "metadata": json.loads(row["metadata"]) if row["metadata"] else {}
                        }
        return None
