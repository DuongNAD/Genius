import os
import sqlite3
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from ag_core.memory.vector_store import SimpleTFIDFEmbedding, VectorMemory
from ag_core.config import Config, load_config
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.codex_reviewer import CodexReviewerAgent
from ag_core.interfaces.base_provider import BaseProvider

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "memory_test.db"
    original_env = os.environ.get("GENIUS_MEMORY_DB_PATH")
    os.environ["GENIUS_MEMORY_DB_PATH"] = str(db_file)
    yield str(db_file)
    if original_env is not None:
        os.environ["GENIUS_MEMORY_DB_PATH"] = original_env
    else:
        os.environ.pop("GENIUS_MEMORY_DB_PATH", None)

def test_simple_tfidf_embedding():
    embedder = SimpleTFIDFEmbedding(vector_dim=64)
    texts = [
        "Deploy a docker container with python script",
        "Python script for database backup",
        "Something completely different"
    ]
    embeddings = embedder.get_embeddings(texts)
    
    assert len(embeddings) == 3
    assert len(embeddings[0]) == 64
    assert len(embeddings[1]) == 64
    
    # Check that identical texts produce identical embeddings
    emb1 = embedder.get_embeddings(["test message"])[0]
    emb2 = embedder.get_embeddings(["test message"])[0]
    assert emb1 == emb2
    
    # Test normalization (sum of squares should be close to 1.0)
    import math
    norm = math.sqrt(sum(v*v for v in emb1))
    assert pytest.approx(norm, rel=1e-5) == 1.0

def test_sqlite_fallback_store(temp_db):
    memory = VectorMemory(collection_name="test_collection", use_chroma=False, db_path=temp_db)
    
    # Insert items
    doc_id1 = memory.add("Deploy docker containers and kubernetes pods", metadata={"category": "ops"})
    doc_id2 = memory.add("Python script to fetch data from API", metadata={"category": "dev"})
    
    assert doc_id1 is not None
    assert doc_id2 is not None
    
    # Query database directly to check persistence
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT id, collection_name, text, metadata FROM agent_vector_memory_fallback")
    rows = cursor.fetchall()
    conn.close()
    
    assert len(rows) == 2
    ids = [r[0] for r in rows]
    assert doc_id1 in ids
    assert doc_id2 in ids
    
    # Query memory and verify relevance
    results = memory.query("docker pods", n_results=1)
    assert len(results) == 1
    assert results[0]["id"] == doc_id1
    assert "docker" in results[0]["text"]
    assert results[0]["metadata"]["category"] == "ops"
    
    results_dev = memory.query("python script API", n_results=1)
    assert len(results_dev) == 1
    assert results_dev[0]["id"] == doc_id2

def test_chroma_store_skip_or_run(tmp_path):
    # Test if Chroma initializes properly if available, else gracefully skip
    try:
        import chromadb
        chroma_available = True
    except ImportError:
        chroma_available = False
        
    if not chroma_available:
        pytest.skip("chromadb is not available in the current environment")
        
    persist_dir = tmp_path / ".chroma"
    memory = VectorMemory(
        collection_name="test_chroma_collection",
        use_chroma=True,
        db_path=None,
        chroma_persist_dir=str(persist_dir)
    )
    
    assert memory.use_chroma is True
    doc_id = memory.add("Chroma test document", metadata={"source": "test"})
    assert doc_id is not None
    
    results = memory.query("Chroma test", n_results=1)
    assert len(results) == 1
    assert results[0]["id"] == doc_id
    assert results[0]["text"] == "Chroma test document"

@pytest.mark.asyncio
async def test_agent_integration(temp_db):
    # Mock Provider
    mock_provider = MagicMock(spec=BaseProvider)
    mock_provider.model_name = "mock-model"
    mock_provider.send_prompt = AsyncMock(return_value={
        "content": "Architectural design response",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20}
    })
    
    # Create configuration with memory enabled
    config = load_config()
    config.memory.enabled = True
    config.memory.use_chroma = False
    config.memory.db_path = temp_db
    
    agent = ClaudeArchitectAgent(provider=mock_provider, config=config)
    
    # First execution
    response1 = await agent.run(prompt="Setup microservice project", context_data={})
    assert response1 == "Architectural design response"
    
    # Check that interaction was saved to memory
    past_memories = agent.retrieve_memory("Setup microservice", limit=1)
    assert len(past_memories) == 1
    assert "Setup microservice project" in past_memories[0]["text"]
    assert "Architectural design response" in past_memories[0]["text"]
    
    # Second execution - mock send_prompt should receive the historical memory context
    mock_provider.send_prompt.reset_mock()
    mock_provider.send_prompt.return_value = {
        "content": "Second response",
        "usage": {"prompt_tokens": 15, "completion_tokens": 25}
    }
    
    await agent.run(prompt="Setup microservice project again", context_data={})
    
    # Verify that send_prompt was called with memory context included in the prompt
    args, kwargs = mock_provider.send_prompt.call_args
    sent_prompt = args[0]
    assert "Relevant Historical Memory Context" in sent_prompt
    assert "Setup microservice project" in sent_prompt
    assert "Architectural design response" in sent_prompt
