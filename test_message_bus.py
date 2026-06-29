from ag_core.utils.message_bus import MessageBus, Artifact


def test_artifact_content_type_coercion():
    # Test coercion during Artifact instantiation
    art_dict = Artifact(
        name="test_dict", content={"key": "val"}, created_by="test"
    )
    assert art_dict.content_type == "json"

    art_list = Artifact(
        name="test_list", content=[1, 2, 3], created_by="test"
    )
    assert art_list.content_type == "json"

    art_text = Artifact(name="test_text", content="hello", created_by="test")
    assert art_text.content_type == "text"


def test_message_bus_publish_coercion():
    # Test coercion during publish
    bus = MessageBus()
    art = Artifact(name="test_coerced", content="temp", created_by="test")
    # Mutate content to dict after instantiation
    art.content = {"new": "dict"}
    art.content_type = "text"

    bus.publish(art)
    assert art.content_type == "json"

    retrieved = bus.retrieve(art.artifact_id)
    assert retrieved is not None
    assert retrieved["content_type"] == "json"
    assert retrieved["content"] == {"new": "dict"}


def test_message_bus_fifo_eviction(tmp_path):
    db_file = tmp_path / "message_bus_test.db"
    bus = MessageBus(db_path=str(db_file))

    # Publish 105 artifacts
    published_ids = []
    for i in range(105):
        art = Artifact(
            name=f"art_{i}", content=f"content_{i}", created_by="test"
        )
        art_id = bus.publish(art)
        published_ids.append((art_id, f"content_{i}"))

    # In-memory store should have exactly 100 entries
    assert len(bus.in_memory_store) == 100

    # The first 5 entries should be evicted from in-memory store
    for i in range(5):
        evicted_id = published_ids[i][0]
        assert evicted_id not in bus.in_memory_store

    # The remaining 100 entries should still be in-memory
    for i in range(5, 105):
        remaining_id = published_ids[i][0]
        assert remaining_id in bus.in_memory_store

    # We can retrieve the evicted entries from persistent DB
    for i in range(5):
        evicted_id, expected_content = published_ids[i]
        retrieved = bus.retrieve(evicted_id)
        assert retrieved is not None
        assert retrieved["content"] == expected_content


def test_message_bus_no_eviction_without_db():
    bus = MessageBus()

    # Publish 105 artifacts without persistent DB
    published_ids = []
    for i in range(105):
        art = Artifact(
            name=f"art_{i}", content=f"content_{i}", created_by="test"
        )
        art_id = bus.publish(art)
        published_ids.append(art_id)

    # In-memory store should contain all 105 entries
    assert len(bus.in_memory_store) == 105
    for art_id in published_ids:
        assert art_id in bus.in_memory_store
