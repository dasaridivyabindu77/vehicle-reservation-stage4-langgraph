"""Verify SqliteSaver implements LangGraph's checkpoint interface correctly.

This test ensures our custom SqliteSaver is fully compatible with LangGraph's
BaseCheckpointSaver contract and can be used as a drop-in replacement.
"""

import tempfile
from pathlib import Path

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.checkpoint_sqlite import SqliteSaver


class TestCheckpointInterfaceCompliance:
    """Verify SqliteSaver implements the required checkpoint interface."""

    def test_inherits_from_base_checkpoint_saver(self):
        """SqliteSaver must inherit from BaseCheckpointSaver."""
        assert issubclass(SqliteSaver, BaseCheckpointSaver)

    def test_implements_required_methods(self):
        """SqliteSaver must implement all required abstract methods."""
        required_methods = [
            'get_tuple',
            'list',
            'put',
            'put_writes',
        ]

        saver = SqliteSaver(':memory:')

        for method_name in required_methods:
            assert hasattr(saver, method_name), f"Missing required method: {method_name}"
            method = getattr(saver, method_name)
            assert callable(method), f"{method_name} is not callable"

        saver.close()

    def test_get_tuple_signature(self):
        """get_tuple must accept config and return Optional[CheckpointTuple]."""
        from langgraph.checkpoint.base import CheckpointTuple

        saver = SqliteSaver(':memory:')

        # Should accept config dict
        config = {"configurable": {"thread_id": "test"}}
        result = saver.get_tuple(config)

        # Should return None for non-existent checkpoint
        assert result is None

        # After putting, should return CheckpointTuple
        checkpoint = {"v": 1, "id": "test-1", "channel_values": {}}
        saver.put(config, checkpoint, {}, {})

        result = saver.get_tuple(config)
        assert isinstance(result, CheckpointTuple)

        saver.close()

    def test_put_signature(self):
        """put must accept config, checkpoint, metadata, new_versions and return config."""
        saver = SqliteSaver(':memory:')

        config = {"configurable": {"thread_id": "test"}}
        checkpoint = {"v": 1, "id": "ckpt-1", "channel_values": {"key": "value"}}
        metadata = {"source": "test", "step": 1}
        new_versions = {}

        result = saver.put(config, checkpoint, metadata, new_versions)

        # Must return a config dict with checkpoint_id
        assert isinstance(result, dict)
        assert "configurable" in result
        assert "thread_id" in result["configurable"]
        assert "checkpoint_id" in result["configurable"]

        saver.close()

    def test_list_signature(self):
        """list must accept config and optional params, return iterator."""
        saver = SqliteSaver(':memory:')

        # Save some checkpoints
        config = {"configurable": {"thread_id": "test"}}
        for i in range(3):
            checkpoint = {"v": 1, "id": f"ckpt-{i}", "channel_values": {}}
            saver.put(config, checkpoint, {}, {})

        # List should return iterator
        result = saver.list(config, limit=10)

        # Should be iterable
        checkpoints = list(result)
        assert len(checkpoints) >= 1

        saver.close()

    def test_put_writes_signature(self):
        """put_writes must accept config, writes, task_id."""
        saver = SqliteSaver(':memory:')

        # First create a checkpoint
        config = {"configurable": {"thread_id": "test"}}
        checkpoint = {"v": 1, "id": "ckpt-1", "channel_values": {}}
        result_config = saver.put(config, checkpoint, {}, {})

        # Now add writes
        checkpoint_id = result_config["configurable"]["checkpoint_id"]
        write_config = {
            "configurable": {
                "thread_id": "test",
                "checkpoint_id": checkpoint_id,
            }
        }

        writes = [
            ("channel-1", {"key": "value1"}),
            ("channel-2", {"key": "value2"}),
        ]

        # Should not raise
        saver.put_writes(write_config, writes, task_id="task-001")

        # Verify writes are stored
        tuple_result = saver.get_tuple(write_config)
        assert len(tuple_result.pending_writes) == 2

        saver.close()


class TestCheckpointSerializationContract:
    """Verify SqliteSaver correctly serializes/deserializes checkpoint data."""

    def test_preserves_checkpoint_structure(self):
        """Checkpoint dict structure must be preserved exactly."""
        saver = SqliteSaver(':memory:')

        config = {"configurable": {"thread_id": "test"}}

        # LangGraph checkpoint structure
        checkpoint = {
            "v": 4,
            "id": "checkpoint-123",
            "ts": "2026-11-17T10:00:00Z",
            "channel_values": {
                "messages": ["msg1", "msg2"],
                "counter": 42,
                "flag": True,
                "nested": {"key": "value"},
            },
            "channel_versions": {
                "messages": 2,
                "counter": 1,
            },
            "versions_seen": {
                "node1": {"messages": 1},
            },
        }

        saver.put(config, checkpoint, {}, {})

        # Retrieve and verify structure
        result = saver.get_tuple(config)
        retrieved = result.checkpoint

        assert isinstance(retrieved, dict), "Checkpoint must be a dict"
        assert retrieved["v"] == 4
        assert retrieved["id"] == "checkpoint-123"
        assert retrieved["ts"] == "2026-11-17T10:00:00Z"

        # Verify nested structures
        assert isinstance(retrieved["channel_values"], dict)
        assert retrieved["channel_values"]["messages"] == ["msg1", "msg2"]
        assert retrieved["channel_values"]["counter"] == 42
        assert retrieved["channel_values"]["flag"] is True
        assert retrieved["channel_values"]["nested"]["key"] == "value"

        saver.close()

    def test_handles_none_values(self):
        """Checkpoint with None values must serialize correctly."""
        saver = SqliteSaver(':memory:')

        config = {"configurable": {"thread_id": "test"}}
        checkpoint = {
            "v": 1,
            "id": "ckpt-1",
            "channel_values": {
                "optional_field": None,
                "present_field": "value",
            },
        }

        saver.put(config, checkpoint, {}, {})
        result = saver.get_tuple(config)

        assert result.checkpoint["channel_values"]["optional_field"] is None
        assert result.checkpoint["channel_values"]["present_field"] == "value"

        saver.close()

    def test_handles_empty_structures(self):
        """Empty lists and dicts must serialize correctly."""
        saver = SqliteSaver(':memory:')

        config = {"configurable": {"thread_id": "test"}}
        checkpoint = {
            "v": 1,
            "id": "ckpt-1",
            "channel_values": {
                "empty_list": [],
                "empty_dict": {},
                "empty_string": "",
            },
        }

        saver.put(config, checkpoint, {}, {})
        result = saver.get_tuple(config)

        assert result.checkpoint["channel_values"]["empty_list"] == []
        assert result.checkpoint["channel_values"]["empty_dict"] == {}
        assert result.checkpoint["channel_values"]["empty_string"] == ""

        saver.close()

    def test_handles_unicode_and_special_chars(self):
        """Unicode and special characters must serialize correctly."""
        saver = SqliteSaver(':memory:')

        config = {"configurable": {"thread_id": "test"}}
        checkpoint = {
            "v": 1,
            "id": "ckpt-1",
            "channel_values": {
                "unicode": "Hello 世界 🌍",
                "quotes": 'Single "double" quotes',
                "newlines": "Line1\nLine2\tTabbed",
            },
        }

        saver.put(config, checkpoint, {}, {})
        result = saver.get_tuple(config)

        assert result.checkpoint["channel_values"]["unicode"] == "Hello 世界 🌍"
        assert result.checkpoint["channel_values"]["quotes"] == 'Single "double" quotes'
        assert result.checkpoint["channel_values"]["newlines"] == "Line1\nLine2\tTabbed"

        saver.close()

    def test_metadata_serialization(self):
        """Metadata must serialize correctly."""
        saver = SqliteSaver(':memory:')

        config = {"configurable": {"thread_id": "test"}}
        checkpoint = {"v": 1, "id": "ckpt-1", "channel_values": {}}
        metadata = {
            "source": "user_interaction",
            "step": 5,
            "timestamp": "2026-11-17T10:00:00Z",
            "nested": {"key": "value"},
        }

        saver.put(config, checkpoint, metadata, {})
        result = saver.get_tuple(config)

        assert result.metadata["source"] == "user_interaction"
        assert result.metadata["step"] == 5
        assert result.metadata["timestamp"] == "2026-11-17T10:00:00Z"
        assert result.metadata["nested"]["key"] == "value"

        saver.close()


class TestCheckpointParentRelationships:
    """Verify checkpoint parent relationships are maintained."""

    def test_parent_checkpoint_relationship(self):
        """Checkpoints must maintain parent relationship."""
        saver = SqliteSaver(':memory:')

        # Create parent checkpoint
        config1 = {"configurable": {"thread_id": "test"}}
        checkpoint1 = {"v": 1, "id": "parent", "channel_values": {"step": 1}}
        result1 = saver.put(config1, checkpoint1, {}, {})

        parent_checkpoint_id = result1["configurable"]["checkpoint_id"]

        # Create child checkpoint with parent reference
        config2 = {
            "configurable": {
                "thread_id": "test",
                "checkpoint_id": parent_checkpoint_id,
            }
        }
        checkpoint2 = {"v": 1, "id": "child", "channel_values": {"step": 2}}
        result2 = saver.put(config2, checkpoint2, {}, {})

        # Retrieve child and verify parent link
        child_tuple = saver.get_tuple(result2)

        assert child_tuple.parent_config is not None
        assert child_tuple.parent_config["configurable"]["checkpoint_id"] == parent_checkpoint_id

        saver.close()


class TestLangGraphIntegration:
    """Verify SqliteSaver works correctly with actual LangGraph."""

    def test_compiles_with_langgraph(self):
        """SqliteSaver must work as checkpointer in LangGraph compilation."""
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict

        class State(TypedDict):
            counter: int

        def increment(state: State) -> State:
            return {"counter": state.get("counter", 0) + 1}

        # Build graph with SqliteSaver
        saver = SqliteSaver(':memory:')

        graph = StateGraph(State)
        graph.add_node("increment", increment)
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)

        # Should compile without error
        compiled = graph.compile(checkpointer=saver)

        assert compiled is not None

        saver.close()

    def test_executes_with_checkpointing(self):
        """Graph execution must create checkpoints."""
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict

        class State(TypedDict):
            value: str

        def process(state: State) -> State:
            return {"value": state.get("value", "") + "X"}

        saver = SqliteSaver(':memory:')

        graph = StateGraph(State)
        graph.add_node("process", process)
        graph.add_edge(START, "process")
        graph.add_edge("process", END)

        compiled = graph.compile(checkpointer=saver)

        # Execute with thread_id
        config = {"configurable": {"thread_id": "test-exec"}}
        result = compiled.invoke({"value": "A"}, config)

        assert result["value"] == "AX"

        # Verify checkpoint was created
        checkpoint_tuple = saver.get_tuple(config)
        assert checkpoint_tuple is not None

        saver.close()

    def test_resumes_from_checkpoint(self):
        """Graph must resume from saved checkpoint."""
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict

        class State(TypedDict):
            counter: int

        def increment(state: State) -> State:
            return {"counter": state.get("counter", 0) + 1}

        saver = SqliteSaver(':memory:')

        graph = StateGraph(State)
        graph.add_node("increment", increment)
        graph.add_edge(START, "increment")
        graph.add_edge("increment", END)

        compiled = graph.compile(checkpointer=saver)

        config = {"configurable": {"thread_id": "test-resume"}}

        # First execution
        result1 = compiled.invoke({"counter": 0}, config)
        assert result1["counter"] == 1

        # Verify checkpoint was saved and can be retrieved
        checkpoint_tuple = saver.get_tuple(config)
        assert checkpoint_tuple is not None
        assert checkpoint_tuple.checkpoint is not None

        # Verify checkpoint contains the counter state
        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
        assert "counter" in channel_values
        assert channel_values["counter"] == 1

        saver.close()
