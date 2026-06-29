import os
import json
import uuid
import re
import math
import hashlib
from collections import Counter
from typing import List, Dict, Any

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.abspath(os.path.join(_CURRENT_DIR, "..", ".."))

try:
    import chromadb
    from chromadb.api.types import EmbeddingFunction, Documents, Embeddings
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except Exception:
    SENTENCE_TRANSFORMERS_AVAILABLE = False

class SimpleTFIDFEmbedding:
    """Offline-safe term frequency-based embedding generator."""
    def __init__(self, vector_dim: int = 128):
        if not isinstance(vector_dim, int) or isinstance(vector_dim, bool):
            raise TypeError("vector_dim must be an integer")
        if vector_dim <= 0:
            raise ValueError("vector_dim must be greater than 0")
        self.vector_dim = vector_dim

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r'\w+', text.lower())

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
                    word_hash = int(hashlib.md5(word.encode('utf-8')).hexdigest(), 16)
                    idx = word_hash % self.vector_dim
                    vector[idx] += (count / total)
                norm = math.sqrt(sum(v * v for v in vector))
                if norm > 0:
                    vector = [v / norm for v in vector]
            embeddings.append(vector)
        return embeddings

if CHROMA_AVAILABLE:
    class ChromaEmbeddingFunctionWrapper(EmbeddingFunction):
        def __init__(self, vector_memory):
            self.vector_memory = vector_memory
        def __call__(self, input: Documents) -> Embeddings:
            return self.vector_memory.get_embeddings(input)

class VectorMemory:
    def __init__(self, collection_name: str, use_chroma: bool = False, db_path: str = None, chroma_persist_dir: str = None):
        self.collection_name = collection_name
        self.embedder = SimpleTFIDFEmbedding()
        
        self.sentence_transformer_model = None
        if SENTENCE_TRANSFORMERS_AVAILABLE:
            try:
                self.sentence_transformer_model = SentenceTransformer('all-MiniLM-L6-v2')
            except Exception as e:
                print(f"Warning: Failed to load SentenceTransformer ({e}). Falling back to TF-IDF.")
                
        self.db_path = db_path or os.environ.get("GENIUS_MEMORY_DB_PATH") or os.environ.get("GENIUS_DB_PATH") or os.path.join(_ROOT_DIR, "genius.db")
        
        self.use_chroma = use_chroma and CHROMA_AVAILABLE
        if self.use_chroma:
            try:
                self.chroma_dir = chroma_persist_dir or os.environ.get("GENIUS_CHROMA_DIR") or os.path.join(_ROOT_DIR, ".chroma")
                self.client = chromadb.PersistentClient(path=self.chroma_dir)
                emb_fn = ChromaEmbeddingFunctionWrapper(self)
                self.collection = self.client.get_or_create_collection(
                     name=collection_name, 
                     embedding_function=emb_fn
                )
            except Exception as e:
                print(f"Warning: Failed to initialize Chroma DB ({e}). Falling back to SQLite.")
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
                print(f"Warning: SentenceTransformer encoding failed ({e}). Falling back to TF-IDF.")
        return self.embedder.get_embeddings(texts)

    def _get_connection(self):
        import sqlite3
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_sqlite_db(self):
        conn = self._get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_vector_memory_fallback (
                    id TEXT PRIMARY KEY,
                    collection_name TEXT,
                    text TEXT,
                    metadata TEXT,
                    embedding TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collection ON agent_vector_memory_fallback(collection_name)")
            conn.commit()
        finally:
            conn.close()

    def add(self, text: str, metadata: dict | None = None, doc_id: str = None) -> str:
        doc_id = doc_id or str(uuid.uuid4())
        metadata = metadata or {}
        
        if self.use_chroma:
            self.collection.add(
                documents=[text],
                metadatas=[metadata],
                ids=[doc_id]
            )
        else:
            embedding = self.get_embeddings([text])[0]
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_vector_memory_fallback (id, collection_name, text, metadata, embedding) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, self.collection_name, text, json.dumps(metadata), json.dumps(embedding))
                )
                conn.commit()
            finally:
                conn.close()
        return doc_id

    def query(self, query_text: str, n_results: int = 5) -> List[Dict[str, Any]]:
        if self.use_chroma:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=n_results
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
                    (self.collection_name,)
                )
                rows = cursor.fetchall()
            finally:
                conn.close()
            
            if not rows:
                return []

            query_vector = self.get_embeddings([query_text])[0]
            scored_rows = []
            for uid, text, metadata_str, emb_str in rows:
                try:
                    emb = json.loads(emb_str)
                    if len(query_vector) != len(emb):
                        score = 0.0
                    else:
                        score = sum(q * e for q, e in zip(query_vector, emb))
                except Exception:
                    score = 0.0
                
                try:
                    meta = json.loads(metadata_str) if metadata_str else {}
                except Exception:
                    meta = {}
                scored_rows.append((score, uid, text, meta))

            scored_rows.sort(key=lambda x: x[0], reverse=True)
            return [
                {"id": r[1], "text": r[2], "metadata": r[3], "score": r[0]}
                for r in scored_rows[:n_results]
            ]
