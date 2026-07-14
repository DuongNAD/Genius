import os
import json
import logging
import uuid
import re
import math
import hashlib
from collections import Counter
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))

import importlib.util


def _module_available(name: str) -> bool:
    """Cheaply probe whether an optional dependency is installed.

    Uses find_spec so we do NOT trigger the heavy import (e.g. torch via
    sentence_transformers) at module load — that import is what makes the MCP
    server boot slowly. The actual import is deferred to first use.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# Availability flags are determined cheaply; the heavy modules are imported
# lazily by the loaders below only when a VectorMemory actually needs them.
CHROMA_AVAILABLE = _module_available("chromadb")
SENTENCE_TRANSFORMERS_AVAILABLE = _module_available("sentence_transformers")

# Populated on first use; kept module-level so tests can still patch them.
SentenceTransformer = None


def _load_sentence_transformer_class():
    """Import and cache the SentenceTransformer class on first use."""
    global SentenceTransformer
    if SentenceTransformer is None:
        from sentence_transformers import SentenceTransformer as _ST

        SentenceTransformer = _ST
    return SentenceTransformer


class SimpleTFIDFEmbedding:
    """Offline-safe term frequency-based embedding generator."""

    def __init__(self, vector_dim: int = 128):
        if not isinstance(vector_dim, int) or isinstance(vector_dim, bool):
            raise TypeError("vector_dim must be an integer")
        if vector_dim <= 0:
            raise ValueError("vector_dim must be greater than 0")
        self.vector_dim = vector_dim

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"\w+", text.lower())

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        embeddings = []
        for text in texts:
            tokens = self._tokenize(text)
            counter = Counter(tokens)
            vector = [0.0] * self.vector_dim
            total = sum(counter.values())
            if total > 0:
                for word, count in counter.items():
                    # Deterministic hash to map word to index
                    # This is a stable feature-bucketing hash, not a security
                    # primitive.  Mark it explicitly so FIPS/security scanners
                    # do not treat the intentional compatibility-preserving MD5
                    # use as cryptography.
                    word_hash = int(
                        hashlib.md5(
                            word.encode("utf-8"), usedforsecurity=False
                        ).hexdigest(),
                        16,
                    )
                    idx = word_hash % self.vector_dim
                    vector[idx] += count / total
                norm = math.sqrt(sum(v * v for v in vector))
                if norm > 0:
                    vector = [v / norm for v in vector]
            embeddings.append(vector)
        return embeddings


def _make_chroma_embedding_fn(vector_memory):
    """Build a Chroma EmbeddingFunction lazily (imports chromadb on first use)."""
    from chromadb.api.types import EmbeddingFunction, Documents, Embeddings

    class ChromaEmbeddingFunctionWrapper(EmbeddingFunction):
        def __init__(self, vm):
            self.vector_memory = vm

        def __call__(self, input: Documents) -> Embeddings:
            return self.vector_memory.get_embeddings(input)

    return ChromaEmbeddingFunctionWrapper(vector_memory)


_cached_sentence_transformer = None
_sentence_transformer_failed = False


class VectorMemory:
    def __init__(
        self,
        collection_name: str,
        use_chroma: bool = False,
        db_path: str = None,
        chroma_persist_dir: str = None,
    ):
        self.collection_name = collection_name
        self.embedder = SimpleTFIDFEmbedding()

        self.sentence_transformer_model = None
        global _cached_sentence_transformer, _sentence_transformer_failed
        if SENTENCE_TRANSFORMERS_AVAILABLE and not _sentence_transformer_failed:
            if _cached_sentence_transformer is not None:
                self.sentence_transformer_model = _cached_sentence_transformer
            else:
                try:
                    _cached_sentence_transformer = _load_sentence_transformer_class()(
                        "all-MiniLM-L6-v2"
                    )
                    self.sentence_transformer_model = _cached_sentence_transformer
                except Exception as e:
                    print(
                        f"Warning: Failed to load SentenceTransformer ({e}). Falling back to TF-IDF."
                    )
                    _sentence_transformer_failed = True

        self.db_path = (
            db_path
            or os.environ.get("GENIUS_MEMORY_DB_PATH")
            or os.environ.get("GENIUS_DB_PATH")
            or os.path.join(_ROOT_DIR, "genius.db")
        )

        self.use_chroma = use_chroma and CHROMA_AVAILABLE
        if self.use_chroma:
            try:
                import chromadb

                self.chroma_dir = (
                    chroma_persist_dir
                    or os.environ.get("GENIUS_CHROMA_DIR")
                    or os.path.join(_ROOT_DIR, ".chroma")
                )
                self.client = chromadb.PersistentClient(path=self.chroma_dir)
                emb_fn = _make_chroma_embedding_fn(self)
                self.collection = self.client.get_or_create_collection(
                    name=collection_name, embedding_function=emb_fn
                )
            except Exception as e:
                print(
                    f"Warning: Failed to initialize Chroma DB ({e}). Falling back to SQLite."
                )
                self.use_chroma = False
                self._init_sqlite_db()
        else:
            self._init_sqlite_db()

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        if self.sentence_transformer_model:
            try:
                embeddings = self.sentence_transformer_model.encode(texts)
                ret = []
                for idx, emb in enumerate(embeddings):
                    text = texts[idx]
                    v = emb.tolist()
                    if not text.strip():
                        v = [0.0] * len(v)
                    else:
                        norm = math.sqrt(sum(x * x for x in v))
                        if norm > 0:
                            v = [x / norm for x in v]
                    ret.append(v)
                return ret
            except Exception as e:
                print(
                    f"Warning: SentenceTransformer encoding failed ({e}). Falling back to TF-IDF."
                )
                # Keep the fallback at the ST model's dimension so an
                # intermittent encode failure can't inject a wrong-dimension
                # vector that poisons a fixed-dim Chroma collection (or silently
                # unranks every row in the SQLite fallback).
                dim = self._st_expected_dim()
                if dim:
                    return [[0.0] * dim for _ in texts]
        return self.embedder.get_embeddings(texts)

    def _st_expected_dim(self):
        """The SentenceTransformer model's output dimension, or None."""
        model = self.sentence_transformer_model
        if model is None:
            return None
        try:
            return model.get_sentence_embedding_dimension()
        except Exception:
            return None

    def _get_connection(self):
        import sqlite3

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_sqlite_db(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        import sqlite3

        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_vector_memory_fallback (
                        id TEXT PRIMARY KEY,
                        collection_name TEXT,
                        text TEXT,
                        metadata TEXT,
                        embedding TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_collection ON agent_vector_memory_fallback(collection_name)"
                )
        finally:
            conn.close()

    def add(self, text: str, metadata: dict | None = None, doc_id: str = None) -> str:
        doc_id = doc_id or str(uuid.uuid4())
        metadata = metadata or {}

        if self.use_chroma:
            self.collection.add(documents=[text], metadatas=[metadata], ids=[doc_id])
        else:
            embedding = self.get_embeddings([text])[0]
            from ag_core.utils.db import enqueue_db_write

            # Bound the fallback store: without a cap `add` never prunes, so a
            # long-lived MCP server / worker grows it forever AND the O(N)
            # pure-Python cosine scan in `query` gets slower every call. Keep
            # only the newest N rows per collection (by rowid = insertion order).
            try:
                cap = int(os.environ.get("GENIUS_MEMORY_MAX_ROWS") or 2000)
                if cap <= 0:
                    cap = 2000
            except (TypeError, ValueError):
                cap = 2000

            def _add_vector_impl(
                conn, doc_id, collection_name, text, metadata_json, embedding_json
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO agent_vector_memory_fallback (id, collection_name, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, collection_name, text, metadata_json, embedding_json),
                )
                conn.execute(
                    "DELETE FROM agent_vector_memory_fallback "
                    "WHERE collection_name = ? AND id NOT IN ("
                    "SELECT id FROM agent_vector_memory_fallback "
                    "WHERE collection_name = ? ORDER BY rowid DESC LIMIT ?)",
                    (collection_name, collection_name, cap),
                )
                conn.commit()

            try:
                enqueue_db_write(
                    _add_vector_impl,
                    doc_id,
                    self.collection_name,
                    text,
                    json.dumps(metadata),
                    json.dumps(embedding),
                    db_path=self.db_path,
                )
            except Exception:
                raise
        return doc_id

    def query(self, query_text: str, n_results: int = 5) -> List[Dict[str, Any]]:
        if self.use_chroma:
            results = self.collection.query(
                query_texts=[query_text], n_results=n_results
            )
            ret = []
            if results and results.get("documents"):
                docs = results["documents"][0]
                metas = results["metadatas"][0]
                ids = results["ids"][0]
                for i in range(len(ids)):
                    doc = docs[i] if docs else ""
                    meta = metas[i] if metas else {}
                    uid = ids[i]
                    ret.append({"id": uid, "text": doc, "metadata": meta})
            return ret
        else:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, text, metadata, embedding FROM agent_vector_memory_fallback WHERE collection_name = ?",
                    (self.collection_name,),
                )
                rows = cursor.fetchall()
            finally:
                conn.close()

            if not rows:
                return []

            query_vector = self.get_embeddings([query_text])[0]
            scored_rows = []
            dim_mismatches = 0
            for uid, text, metadata_str, emb_str in rows:
                try:
                    emb = json.loads(emb_str)
                except Exception:
                    continue
                if len(query_vector) != len(emb):
                    # The stored embedding's dimension differs from the current
                    # query embedding (the embedding backend/model changed). It
                    # can't be compared, so SKIP it instead of scoring it 0.0 and
                    # returning it as a bogus "match" alongside real results.
                    dim_mismatches += 1
                    continue
                score = sum(q * e for q, e in zip(query_vector, emb))

                try:
                    meta = json.loads(metadata_str) if metadata_str else {}
                except Exception:
                    meta = {}
                scored_rows.append((score, uid, text, meta))

            if dim_mismatches:
                logger.warning(
                    "VectorMemory: skipped %d stored embedding(s) whose dimension "
                    "differs from the current query embedding (the embedding "
                    "backend likely changed); they cannot be ranked.",
                    dim_mismatches,
                )

            scored_rows.sort(key=lambda x: x[0], reverse=True)
            return [
                {"id": r[1], "text": r[2], "metadata": r[3], "score": r[0]}
                for r in scored_rows[:n_results]
            ]
