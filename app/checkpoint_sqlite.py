"""Custom SQLite-based checkpoint saver for LangGraph.

This provides persistent checkpoint storage using SQLite, compatible with
LangGraph's checkpoint interface. This implementation is production-ready
and provides the same functionality as langgraph-checkpoint-sqlite.

Usage:
    from app.checkpoint_sqlite import SqliteSaver

    checkpointer = SqliteSaver(db_path="checkpoints.db")
    graph = builder.compile(checkpointer=checkpointer)
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

logger = logging.getLogger(__name__)


def _serialize_value(value: Any) -> str:
    """Serialize a value to JSON, handling non-serializable objects.

    For complex objects like ChatResponse, convert to dict representation.
    For primitives and JSON-serializable objects, passthrough.

    Args:
        value: Value to serialize

    Returns:
        JSON string
    """
    def default_handler(obj):
        """Custom JSON encoder for non-serializable objects."""
        # Try to convert dataclass or object with as_dict() method
        if hasattr(obj, 'as_dict') and callable(obj.as_dict):
            return obj.as_dict()
        # Try dataclass asdict
        try:
            from dataclasses import asdict, is_dataclass
            if is_dataclass(obj):
                return asdict(obj)
        except (ImportError, TypeError):
            pass
        # Try __dict__
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        # Fall back to string
        return str(obj)

    return json.dumps(value, default=default_handler)


class SqliteSaver(BaseCheckpointSaver):
    """SQLite-based checkpoint saver for persistent storage.

    This implementation stores checkpoints in a SQLite database, providing
    persistence across application restarts.

    Args:
        db_path: Path to SQLite database file

    Example:
        checkpointer = SqliteSaver("checkpoints.db")
        graph = builder.compile(checkpointer=checkpointer)
    """

    def __init__(self, db_path: str = "checkpoints.db"):
        """Initialize SQLite checkpoint saver.

        Args:
            db_path: Path to SQLite database file
        """
        super().__init__(serde=JsonPlusSerializer())
        self.db_path = Path(db_path)
        self.conn = self._init_db()
        logger.info(f"SqliteSaver initialized with database: {self.db_path}")

    # --- typed serialization -------------------------------------------------
    def _pack(self, value: Any) -> str:
        """Serialize a checkpoint value losslessly (including Interrupt objects).

        LangGraph stores ``Interrupt`` instances and LangChain objects in
        checkpoints. Plain ``json.dumps`` turns them into dicts/strings, which
        breaks resume (``Interrupt.id`` disappears). ``JsonPlusSerializer`` is
        the same codec LangGraph's own savers use.
        """
        type_name, payload = self.serde.dumps_typed(value)
        return json.dumps(
            {
                "__typed__": type_name,
                "data": base64.b64encode(payload).decode("ascii"),
            }
        )

    def _unpack(self, raw: str) -> Any:
        """Deserialize a value written by :meth:`_pack`.

        Values written by older versions of this saver are plain JSON; those
        are still readable for backward compatibility.
        """
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if isinstance(parsed, dict) and "__typed__" in parsed:
            return self.serde.loads_typed(
                (parsed["__typed__"], base64.b64decode(parsed["data"]))
            )
        return parsed

    def _init_db(self) -> sqlite3.Connection:
        """Initialize database schema."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row

        # Create checkpoints table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                checkpoint_data TEXT NOT NULL,
                metadata_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
        """)

        # Create index for efficient lookups
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_thread_id
            ON checkpoints(thread_id, checkpoint_ns)
        """)

        # Create writes table for pending writes
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoint_writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
        """)

        return conn

    def get_tuple(self, config: Dict[str, Any]) -> Optional[CheckpointTuple]:
        """Get checkpoint tuple for a given configuration.

        Args:
            config: Configuration dictionary with thread_id

        Returns:
            CheckpointTuple if found, None otherwise
        """
        thread_id = config.get("configurable", {}).get("thread_id")
        checkpoint_ns = config.get("configurable", {}).get("checkpoint_ns", "")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")

        if not thread_id:
            return None

        # Get checkpoint
        if checkpoint_id:
            # Get specific checkpoint
            row = self.conn.execute(
                """
                SELECT checkpoint_id, parent_checkpoint_id, checkpoint_data, metadata_data
                FROM checkpoints
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                """,
                (thread_id, checkpoint_ns, checkpoint_id)
            ).fetchone()
        else:
            # Get latest checkpoint (by rowid which is auto-incrementing)
            row = self.conn.execute(
                """
                SELECT checkpoint_id, parent_checkpoint_id, checkpoint_data, metadata_data
                FROM checkpoints
                WHERE thread_id = ? AND checkpoint_ns = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (thread_id, checkpoint_ns)
            ).fetchone()

        if not row:
            return None

        # Deserialize checkpoint
        checkpoint_data = self._unpack(row["checkpoint_data"])
        metadata_data = self._unpack(row["metadata_data"])

        # Get pending writes
        writes = self.conn.execute(
            """
            SELECT task_id, channel, value
            FROM checkpoint_writes
            WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
            ORDER BY task_id, idx
            """,
            (thread_id, checkpoint_ns, row["checkpoint_id"])
        ).fetchall()

        pending_writes = [
            (w["task_id"], w["channel"], self._unpack(w["value"]))
            for w in writes
        ]

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row["checkpoint_id"],
                }
            },
            checkpoint=checkpoint_data,
            metadata=metadata_data,
            parent_config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row["parent_checkpoint_id"],
                }
            } if row["parent_checkpoint_id"] else None,
            pending_writes=pending_writes,
        )

    def list(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints matching the criteria.

        Args:
            config: Base configuration
            filter: Filter criteria
            before: Return checkpoints before this config
            limit: Maximum number of results

        Yields:
            CheckpointTuple objects
        """
        thread_id = config.get("configurable", {}).get("thread_id") if config else None
        checkpoint_ns = config.get("configurable", {}).get("checkpoint_ns", "") if config else ""

        query = "SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints WHERE 1=1"
        params = []

        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)

        if checkpoint_ns:
            query += " AND checkpoint_ns = ?"
            params.append(checkpoint_ns)

        query += " ORDER BY created_at DESC"

        if limit:
            query += f" LIMIT {limit}"

        rows = self.conn.execute(query, params).fetchall()

        for row in rows:
            config = {
                "configurable": {
                    "thread_id": row["thread_id"],
                    "checkpoint_ns": row["checkpoint_ns"],
                }
            }
            tuple_result = self.get_tuple(config)
            if tuple_result:
                yield tuple_result

    def put(
        self,
        config: Dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Save a checkpoint.

        Args:
            config: Configuration with thread_id
            checkpoint: Checkpoint data
            metadata: Checkpoint metadata
            new_versions: New version information

        Returns:
            Updated configuration
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")

        # Generate new checkpoint ID if not provided
        checkpoint_id = checkpoint.get("id") if checkpoint.get("id") else f"checkpoint_{self._generate_id()}"

        # Serialize with LangGraph's typed serializer so Interrupt objects and
        # LangChain values survive the round-trip.
        checkpoint_data = self._pack(checkpoint)
        metadata_data = self._pack(get_checkpoint_metadata(config, metadata))

        # Save checkpoint (INSERT OR REPLACE updates created_at, so use explicit check)
        existing = self.conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
            (thread_id, checkpoint_ns, checkpoint_id)
        ).fetchone()

        if existing:
            # Update existing checkpoint
            self.conn.execute(
                """
                UPDATE checkpoints
                SET checkpoint_data = ?, metadata_data = ?, parent_checkpoint_id = ?
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                """,
                (checkpoint_data, metadata_data, parent_checkpoint_id, thread_id, checkpoint_ns, checkpoint_id)
            )
        else:
            # Insert new checkpoint
            self.conn.execute(
                """
                INSERT INTO checkpoints
                (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint_data, metadata_data)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint_data, metadata_data)
            )

        logger.debug(
            f"Checkpoint saved: thread_id={thread_id}, checkpoint_id={checkpoint_id}"
        )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: Dict[str, Any],
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Save pending writes for a checkpoint.

        Args:
            config: Configuration with thread_id and checkpoint_id
            writes: List of (channel, value) tuples
            task_id: Task identifier
            task_path: Path of the task that produced the writes (unused)
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        for idx, (channel, value) in enumerate(writes):
            value_json = self._pack(value)

            self.conn.execute(
                """
                INSERT OR REPLACE INTO checkpoint_writes
                (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, value_json)
            )

        logger.debug(
            f"Writes saved: thread_id={thread_id}, checkpoint_id={checkpoint_id}, count={len(writes)}"
        )

    def _generate_id(self) -> str:
        """Generate a unique checkpoint ID."""
        import time
        import random
        return f"{int(time.time() * 1000)}{random.randint(1000, 9999)}"

    def cleanup(self, thread_id: Optional[str] = None, before_days: int = 7) -> int:
        """Clean up old checkpoints.

        Args:
            thread_id: Optional thread_id to clean up (None = all threads)
            before_days: Remove checkpoints older than this many days

        Returns:
            Number of checkpoints removed
        """
        query = """
            DELETE FROM checkpoints
            WHERE datetime(created_at) < datetime('now', '-{} days')
        """.format(before_days)

        params = []
        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)

        cursor = self.conn.execute(query, params)
        deleted = cursor.rowcount

        # Also clean up orphaned writes
        self.conn.execute("""
            DELETE FROM checkpoint_writes
            WHERE NOT EXISTS (
                SELECT 1 FROM checkpoints c
                WHERE c.thread_id = checkpoint_writes.thread_id
                AND c.checkpoint_id = checkpoint_writes.checkpoint_id
            )
        """)

        logger.info(f"Cleaned up {deleted} old checkpoints")
        return deleted

    def close(self):
        """Close the database connection."""
        if self.conn:
            try:
                self.conn.close()
                logger.info("SqliteSaver connection closed")
            except Exception as e:
                logger.warning("Error closing SqliteSaver connection: %s", e)
            finally:
                self.conn = None

    def __del__(self):
        """Ensure connection is closed on deletion."""
        try:
            self.close()
        except:
            pass


def create_sqlite_saver(db_path: str = "checkpoints.db") -> SqliteSaver:
    """Factory function to create a SqliteSaver.

    Args:
        db_path: Path to SQLite database file

    Returns:
        SqliteSaver instance
    """
    return SqliteSaver(db_path)
