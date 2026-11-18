"""Tests for custom SqliteSaver checkpoint implementation.

These tests verify that the SqliteSaver:
1. Persists checkpoints to SQLite database
2. Retrieves checkpoints correctly
3. Survives database reconnection (simulating restart)
4. Isolates threads properly
5. Handles checkpoint lineage (parent relationships)
"""

import tempfile
from pathlib import Path

import pytest

from app.checkpoint_sqlite import SqliteSaver


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup - add small delay to ensure connections are fully closed
    import time
    time.sleep(0.1)
    try:
        Path(db_path).unlink(missing_ok=True)
    except PermissionError:
        # On Windows, SQLite may hold the file briefly even after close()
        pass


class TestSqliteSaverBasics:
    """Tests for basic SqliteSaver functionality."""

    def test_saver_initializes(self, temp_db):
        """SqliteSaver should initialize with database file."""
        saver = SqliteSaver(db_path=temp_db)
        assert saver is not None
        assert saver.db_path == Path(temp_db)
        assert saver.conn is not None
        saver.close()

    def test_saver_creates_tables(self, temp_db):
        """SqliteSaver should create required tables."""
        saver = SqliteSaver(db_path=temp_db)

        # Check checkpoints table exists
        cursor = saver.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        )
        assert cursor.fetchone() is not None

        # Check checkpoint_writes table exists
        cursor = saver.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoint_writes'"
        )
        assert cursor.fetchone() is not None

        saver.close()


class TestCheckpointPersistence:
    """Tests for checkpoint persistence across restarts."""

    def test_put_and_get_checkpoint(self, temp_db):
        """Should save and retrieve checkpoint."""
        saver = SqliteSaver(db_path=temp_db)

        config = {
            "configurable": {
                "thread_id": "test-thread-1",
                "checkpoint_ns": "",
            }
        }

        checkpoint = {
            "v": 1,
            "id": "checkpoint-001",
            "ts": "2026-11-17T10:00:00Z",
            "channel_values": {
                "user_message": "Hello",
                "session_id": "test-thread-1",
            },
        }

        metadata = {
            "source": "test",
            "step": 1,
        }

        # Save checkpoint
        result_config = saver.put(config, checkpoint, metadata, {})

        assert result_config["configurable"]["thread_id"] == "test-thread-1"
        assert "checkpoint_id" in result_config["configurable"]

        # Retrieve checkpoint
        retrieved_config = {
            "configurable": {
                "thread_id": "test-thread-1",
                "checkpoint_ns": "",
            }
        }

        tuple_result = saver.get_tuple(retrieved_config)

        assert tuple_result is not None
        assert tuple_result.checkpoint["id"] == "checkpoint-001"
        assert tuple_result.checkpoint["channel_values"]["user_message"] == "Hello"
        assert tuple_result.metadata["source"] == "test"

        saver.close()

    def test_checkpoint_survives_reconnection(self, temp_db):
        """Checkpoint should survive database reconnection (simulates restart)."""
        # First connection: save checkpoint
        saver1 = SqliteSaver(db_path=temp_db)

        config = {
            "configurable": {
                "thread_id": "restart-test",
                "checkpoint_ns": "",
            }
        }

        checkpoint = {
            "v": 1,
            "id": "persistent-checkpoint",
            "ts": "2026-11-17T10:00:00Z",
            "channel_values": {
                "reservation_id": 42,
                "waiting_for_admin": True,
            },
        }

        metadata = {"step": 2}

        saver1.put(config, checkpoint, metadata, {})
        saver1.close()

        # Second connection: retrieve checkpoint (simulates restart)
        saver2 = SqliteSaver(db_path=temp_db)

        retrieved_config = {
            "configurable": {
                "thread_id": "restart-test",
                "checkpoint_ns": "",
            }
        }

        tuple_result = saver2.get_tuple(retrieved_config)

        assert tuple_result is not None
        assert tuple_result.checkpoint["id"] == "persistent-checkpoint"
        assert tuple_result.checkpoint["channel_values"]["reservation_id"] == 42
        assert tuple_result.checkpoint["channel_values"]["waiting_for_admin"] is True

        saver2.close()


class TestThreadIsolation:
    """Tests for thread isolation."""

    def test_different_threads_are_isolated(self, temp_db):
        """Different threads should have isolated checkpoints."""
        saver = SqliteSaver(db_path=temp_db)

        # Thread 1
        config1 = {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
            }
        }
        checkpoint1 = {
            "v": 1,
            "id": "checkpoint-thread-1",
            "channel_values": {"message": "Thread 1 data"},
        }
        saver.put(config1, checkpoint1, {}, {})

        # Thread 2
        config2 = {
            "configurable": {
                "thread_id": "thread-2",
                "checkpoint_ns": "",
            }
        }
        checkpoint2 = {
            "v": 1,
            "id": "checkpoint-thread-2",
            "channel_values": {"message": "Thread 2 data"},
        }
        saver.put(config2, checkpoint2, {}, {})

        # Retrieve thread 1
        tuple1 = saver.get_tuple(config1)
        assert tuple1 is not None
        assert tuple1.checkpoint["channel_values"]["message"] == "Thread 1 data"

        # Retrieve thread 2
        tuple2 = saver.get_tuple(config2)
        assert tuple2 is not None
        assert tuple2.checkpoint["channel_values"]["message"] == "Thread 2 data"

        # Verify isolation
        assert tuple1.checkpoint["id"] != tuple2.checkpoint["id"]
        assert tuple1.checkpoint["channel_values"]["message"] != tuple2.checkpoint["channel_values"]["message"]

        saver.close()


class TestPendingWrites:
    """Tests for pending writes functionality."""

    def test_put_and_get_writes(self, temp_db):
        """Should save and retrieve pending writes."""
        saver = SqliteSaver(db_path=temp_db)

        # First save a checkpoint
        config = {
            "configurable": {
                "thread_id": "writes-test",
                "checkpoint_ns": "",
            }
        }

        checkpoint = {
            "v": 1,
            "id": "checkpoint-with-writes",
            "channel_values": {},
        }

        result_config = saver.put(config, checkpoint, {}, {})
        checkpoint_id = result_config["configurable"]["checkpoint_id"]

        # Add pending writes
        writes = [
            ("channel-1", {"key": "value1"}),
            ("channel-2", {"key": "value2"}),
        ]

        write_config = {
            "configurable": {
                "thread_id": "writes-test",
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }

        saver.put_writes(write_config, writes, task_id="task-001")

        # Retrieve checkpoint with writes
        tuple_result = saver.get_tuple(write_config)

        assert tuple_result is not None
        assert len(tuple_result.pending_writes) == 2
        assert tuple_result.pending_writes[0][0] == "task-001"
        assert tuple_result.pending_writes[0][1] == "channel-1"
        assert tuple_result.pending_writes[0][2] == {"key": "value1"}

        saver.close()


class TestCheckpointList:
    """Tests for listing checkpoints."""

    def test_list_checkpoints(self, temp_db):
        """Should list checkpoints for a thread."""
        saver = SqliteSaver(db_path=temp_db)

        # Create multiple checkpoints
        for i in range(3):
            config = {
                "configurable": {
                    "thread_id": "list-test",
                    "checkpoint_ns": "",
                }
            }
            checkpoint = {
                "v": 1,
                "id": f"checkpoint-{i}",
                "channel_values": {"step": i},
            }
            saver.put(config, checkpoint, {}, {})

        # List checkpoints
        list_config = {
            "configurable": {
                "thread_id": "list-test",
                "checkpoint_ns": "",
            }
        }

        checkpoints = list(saver.list(list_config, limit=10))

        # Should have at least one (latest)
        assert len(checkpoints) >= 1

        saver.close()


class TestCleanup:
    """Tests for checkpoint cleanup."""

    def test_cleanup_removes_old_checkpoints(self, temp_db):
        """Cleanup should remove old checkpoints."""
        saver = SqliteSaver(db_path=temp_db)

        # Create checkpoint
        config = {
            "configurable": {
                "thread_id": "cleanup-test",
                "checkpoint_ns": "",
            }
        }
        checkpoint = {
            "v": 1,
            "id": "old-checkpoint",
            "channel_values": {},
        }
        saver.put(config, checkpoint, {}, {})

        # Manually update created_at to be old
        saver.conn.execute(
            "UPDATE checkpoints SET created_at = datetime('now', '-10 days') WHERE thread_id = ?",
            ("cleanup-test",)
        )
        saver.conn.commit()

        # Cleanup checkpoints older than 7 days
        deleted = saver.cleanup(before_days=7)

        assert deleted >= 1

        # Verify checkpoint is gone
        tuple_result = saver.get_tuple(config)
        assert tuple_result is None

        saver.close()


class TestIntegrationWithLangGraph:
    """Integration tests with LangGraph-like usage patterns."""

    def test_interrupt_resume_pattern(self, temp_db):
        """Test checkpoint pattern for interrupt/resume workflow."""
        saver = SqliteSaver(db_path=temp_db)

        thread_id = "interrupt-resume-test"

        # Step 1: Save checkpoint before interrupt
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }

        checkpoint_before_interrupt = {
            "v": 1,
            "id": "before-interrupt",
            "channel_values": {
                "reservation_id": 123,
                "waiting_for_admin": True,
                "escalation_sent": True,
            },
        }

        result_config = saver.put(config, checkpoint_before_interrupt, {"step": "escalate"}, {})
        checkpoint_id = result_config["configurable"]["checkpoint_id"]

        # Step 2: Simulate application restart - close and reopen
        saver.close()
        saver = SqliteSaver(db_path=temp_db)

        # Step 3: Retrieve checkpoint after restart
        retrieve_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }

        tuple_result = saver.get_tuple(retrieve_config)

        assert tuple_result is not None
        assert tuple_result.checkpoint["channel_values"]["reservation_id"] == 123
        assert tuple_result.checkpoint["channel_values"]["waiting_for_admin"] is True

        # Step 4: Resume with decision - save new checkpoint
        checkpoint_after_resume = {
            "v": 1,
            "id": "after-resume",
            "channel_values": {
                "reservation_id": 123,
                "waiting_for_admin": False,
                "admin_decision": "approved",
                "escalation_sent": True,
            },
        }

        resume_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,  # Parent checkpoint
            }
        }

        saver.put(resume_config, checkpoint_after_resume, {"step": "process_decision"}, {})

        # Step 5: Verify latest checkpoint has the decision
        latest = saver.get_tuple(retrieve_config)

        assert latest is not None
        assert latest.checkpoint["id"] == "after-resume"
        assert latest.checkpoint["channel_values"]["admin_decision"] == "approved"
        assert latest.checkpoint["channel_values"]["waiting_for_admin"] is False

        saver.close()
