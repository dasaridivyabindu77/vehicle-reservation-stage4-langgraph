"""Tests for duplicate reservation submission and persistence prevention.

This test suite verifies that the Stage 4 duplicate bug is fixed:
- One booking creates only ONE admin escalation
- One approval persists only ONCE to MCP
- Follow-up messages don't trigger re-escalation
- Status checks don't trigger re-escalation
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

from app.config import Settings
from app.chatbot import Chatbot
from app.admin.agent import AdminAgent
from app.admin.escalation import EscalationService
from app.orchestration_v2 import build_production_graph, process_admin_decision_and_resume
from mcp_server.client import MCPClient


@pytest.fixture
def temp_mcp_dir():
    """Temporary directory for MCP data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_databases(tmp_path):
    """Temporary databases for testing."""
    return {
        "sqlite": tmp_path / "test_dup.db",
        "checkpoint": tmp_path / "test_dup_checkpoint.db",
    }


@pytest.fixture
def mcp_client_factory():
    """Factory for creating MCP clients."""
    def _create_client(data_dir: Path):
        """Create a mock MCP client that writes to local directory."""
        # Create a mock client that bypasses HTTP and writes locally
        class MockMCPClient:
            def __init__(self, data_dir: Path):
                self.data_dir = data_dir
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self._is_available = True

            def is_available(self) -> bool:
                return True

            def write_confirmed_reservation(
                self,
                reservation_id: int,
                first_name: str,
                last_name: str,
                car_number: str,
                start_ts: str,
                end_ts: str,
                approved_by: str,
                approval_time: str,
            ) -> dict:
                """Write reservation to local file."""
                mcp_file = self.data_dir / "confirmed_reservations.txt"

                # Format entry
                entry = f"Reservation #{reservation_id} | {first_name} {last_name} | {car_number} | {start_ts} to {end_ts} | {approval_time}\n"

                # Append to file
                with open(mcp_file, 'a') as f:
                    f.write(entry)

                return {
                    "success": True,
                    "file_path": str(mcp_file),
                    "reservation_id": reservation_id
                }

        return MockMCPClient(data_dir)

    return _create_client


def _get_future_datetime(days_ahead: int, hour: int = 10) -> str:
    """Generate future datetime for testing."""
    future = datetime.now() + timedelta(days=days_ahead)
    return future.strftime(f"%Y-%m-%d {hour:02d}:00")


def test_no_duplicate_escalation_on_follow_up_message(temp_databases, temp_mcp_dir, mcp_client_factory):
    """Verify follow-up messages don't create duplicate escalations.

    Flow:
    1. User makes reservation (creates escalation)
    2. User sends follow-up message "What's the status?"
    3. Verify: Only ONE escalation exists
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(temp_databases["sqlite"]),
        checkpoint_db=str(temp_databases["checkpoint"]),
        mcp_enabled=False,
    )

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)
    escalation = EscalationService(chatbot.sql, admin, mcp_client=None)

    graph = build_production_graph(
        chatbot=chatbot,
        escalation_service=escalation,
        mcp_client=mcp_client_factory(temp_mcp_dir),
        checkpoint_db=str(temp_databases["checkpoint"]),
    )

    session_id = "dup-test-1"
    config = {"configurable": {"thread_id": session_id}}

    # Step 1: Complete reservation
    start_time = _get_future_datetime(7, 10)
    end_time = _get_future_datetime(7, 18)

    messages = [
        "I'd like to book a parking spot",
        "John",
        "Doe",
        "DUP-001",
        start_time,
        end_time,
    ]

    for msg in messages:
        state = graph.invoke({"user_message": msg, "session_id": session_id}, config)

    # Check: Exactly one pending reservation
    pending = escalation.pending()
    assert len(pending) == 1, f"Expected 1 pending, got {len(pending)}"
    reservation_id = pending[0].id

    # Step 2: Send follow-up messages
    follow_ups = [
        "What's the status of my booking?",
        "Is it approved yet?",
        "Hello?",
    ]

    for msg in follow_ups:
        graph.invoke({"user_message": msg, "session_id": session_id}, config)

    # Verify: Still only one pending reservation (no duplicates)
    pending_after = escalation.pending()
    assert len(pending_after) == 1, f"Follow-up created duplicates: {len(pending_after)} pending"
    assert pending_after[0].id == reservation_id, "Different reservation ID - duplicate created!"

    print(f"✓ No duplicate escalation: {len(pending_after)} pending (expected 1)")


def test_no_duplicate_mcp_write_on_approval(temp_databases, temp_mcp_dir, mcp_client_factory):
    """Verify approval writes to MCP only ONCE.

    Flow:
    1. User makes reservation
    2. Admin approves via HTTP endpoint
    3. Verify: MCP file contains exactly ONE entry
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(temp_databases["sqlite"]),
        checkpoint_db=str(temp_databases["checkpoint"]),
        mcp_enabled=True,
    )

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    # MCP client with local file write
    mcp_client = mcp_client_factory(temp_mcp_dir)

    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp_client)

    graph = build_production_graph(
        chatbot=chatbot,
        escalation_service=escalation,
        mcp_client=mcp_client,
        checkpoint_db=str(temp_databases["checkpoint"]),
    )

    session_id = "dup-test-2"
    config = {"configurable": {"thread_id": session_id}}

    # Step 1: Complete reservation
    start_time = _get_future_datetime(8, 10)
    end_time = _get_future_datetime(8, 18)

    messages = [
        "I want to reserve parking",
        "Jane",
        "Smith",
        "DUP-002",
        start_time,
        end_time,
    ]

    for msg in messages:
        state = graph.invoke({"user_message": msg, "session_id": session_id}, config)

    reservation_id = escalation.pending()[0].id

    # Step 2: Admin approves (simulating the admin route): record the decision
    # first, skipping the direct MCP write so the graph persistence node is the
    # only writer.
    escalation.decide(
        reservation_id=reservation_id,
        raw_decision="approve",
        reason="Test approval",
        decided_by="test-admin",
        skip_mcp_write=True,
    )

    process_admin_decision_and_resume(
        graph=graph,
        thread_id=session_id,
        decision="approved",
        reason="Test approval",
        decided_by="test-admin"
    )

    # Step 3: Verify MCP file
    mcp_file = temp_mcp_dir / "confirmed_reservations.txt"
    assert mcp_file.exists(), "MCP file not created"

    content = mcp_file.read_text()
    lines = [line for line in content.strip().split('\n') if line and not line.startswith('#')]

    # Count entries for this reservation
    dup_002_entries = [line for line in lines if 'DUP002' in line]

    assert len(dup_002_entries) == 1, f"Expected 1 MCP entry, found {len(dup_002_entries)}: {dup_002_entries}"

    print(f"✓ No duplicate MCP write: {len(dup_002_entries)} entry (expected 1)")
    print(f"  Content: {dup_002_entries[0][:80]}...")


def test_no_duplicate_on_admin_dashboard_approval(temp_databases, temp_mcp_dir, mcp_client_factory):
    """Verify admin dashboard approval doesn't create duplicates.

    This tests the actual bug: admin routes calling both escalation.decide()
    AND process_admin_decision_and_resume(), which was causing double writes.

    Flow:
    1. User makes reservation
    2. Admin approves via escalation.decide() with skip_mcp_write=True
    3. Graph resumes and writes to MCP
    4. Verify: Only ONE MCP entry
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(temp_databases["sqlite"]),
        checkpoint_db=str(temp_databases["checkpoint"]),
        mcp_enabled=True,
    )

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    mcp_client = mcp_client_factory(temp_mcp_dir)

    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp_client)

    graph = build_production_graph(
        chatbot=chatbot,
        escalation_service=escalation,
        mcp_client=mcp_client,
        checkpoint_db=str(temp_databases["checkpoint"]),
    )

    session_id = "dup-test-3"
    config = {"configurable": {"thread_id": session_id}}

    # Step 1: Make reservation
    start_time = _get_future_datetime(9, 10)
    end_time = _get_future_datetime(9, 18)

    messages = [
        "Book parking please",
        "Alice",
        "Johnson",
        "DUP-003",
        start_time,
        end_time,
    ]

    for msg in messages:
        graph.invoke({"user_message": msg, "session_id": session_id}, config)

    reservation_id = escalation.pending()[0].id

    # Step 2: Admin approves via dashboard (simulating routes.py flow)
    # First: escalation.decide() with skip_mcp_write=True
    updated, decision = escalation.decide(
        reservation_id=reservation_id,
        raw_decision="approve",
        reason="Admin dashboard approval",
        decided_by="admin-dashboard",
        skip_mcp_write=True  # KEY FIX: Skip MCP write here
    )

    assert updated.status == "confirmed", "Reservation not confirmed in database"

    # Second: Resume graph (which will write to MCP)
    process_admin_decision_and_resume(
        graph=graph,
        thread_id=session_id,
        decision="approved",
        reason="Admin dashboard approval",
        decided_by="admin-dashboard"
    )

    # Step 3: Verify only ONE MCP entry
    mcp_file = temp_mcp_dir / "confirmed_reservations.txt"
    assert mcp_file.exists(), "MCP file not created"

    content = mcp_file.read_text()
    lines = [line for line in content.strip().split('\n') if line and not line.startswith('#')]

    dup_003_entries = [line for line in lines if 'DUP003' in line]

    assert len(dup_003_entries) == 1, f"DUPLICATE BUG: Expected 1 entry, found {len(dup_003_entries)}"

    print(f"✓ Admin dashboard approval: {len(dup_003_entries)} MCP entry (expected 1)")
    print(f"  Fix verified: skip_mcp_write=True prevents duplicate")


def test_multiple_reservations_no_cross_contamination(temp_databases, temp_mcp_dir, mcp_client_factory):
    """Verify multiple reservations don't interfere with each other.

    Flow:
    1. User A makes reservation
    2. User B makes reservation
    3. Admin approves both
    4. Verify: Each has exactly ONE MCP entry
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(temp_databases["sqlite"]),
        checkpoint_db=str(temp_databases["checkpoint"]),
        mcp_enabled=True,
    )

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    mcp_client = mcp_client_factory(temp_mcp_dir)

    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp_client)

    graph = build_production_graph(
        chatbot=chatbot,
        escalation_service=escalation,
        mcp_client=mcp_client,
        checkpoint_db=str(temp_databases["checkpoint"]),
    )

    # User A reservation
    session_a = "multi-user-a"
    config_a = {"configurable": {"thread_id": session_a}}
    start_a = _get_future_datetime(10, 9)
    end_a = _get_future_datetime(10, 17)

    for msg in ["Book parking", "UserA", "TestA", "MULTI-A", start_a, end_a]:
        graph.invoke({"user_message": msg, "session_id": session_a}, config_a)

    # User B reservation
    session_b = "multi-user-b"
    config_b = {"configurable": {"thread_id": session_b}}
    start_b = _get_future_datetime(10, 10)
    end_b = _get_future_datetime(10, 18)

    for msg in ["Reserve spot", "UserB", "TestB", "MULTI-B", start_b, end_b]:
        graph.invoke({"user_message": msg, "session_id": session_b}, config_b)

    # Get reservation IDs
    pending = escalation.pending()
    assert len(pending) == 2, f"Expected 2 pending, got {len(pending)}"

    res_a = [r for r in pending if r.car_number == "MULTIA"][0]
    res_b = [r for r in pending if r.car_number == "MULTIB"][0]

    # Approve both
    for res, session in [(res_a, session_a), (res_b, session_b)]:
        escalation.decide(
            reservation_id=res.id,
            raw_decision="approve",
            reason=f"Approved {res.car_number}",
            decided_by="test-admin",
            skip_mcp_write=True
        )

        process_admin_decision_and_resume(
            graph=graph,
            thread_id=session,
            decision="approved",
            reason=f"Approved {res.car_number}",
            decided_by="test-admin"
        )

    # Verify MCP entries
    mcp_file = temp_mcp_dir / "confirmed_reservations.txt"
    content = mcp_file.read_text()
    lines = [line for line in content.strip().split('\n') if line and not line.startswith('#')]

    multi_a_entries = [line for line in lines if 'MULTIA' in line]
    multi_b_entries = [line for line in lines if 'MULTIB' in line]

    assert len(multi_a_entries) == 1, f"User A has {len(multi_a_entries)} entries (expected 1)"
    assert len(multi_b_entries) == 1, f"User B has {len(multi_b_entries)} entries (expected 1)"

    print(f"✓ Multiple users: MULTIA={len(multi_a_entries)}, MULTIB={len(multi_b_entries)} (expected 1 each)")


def test_legacy_flow_without_langgraph_still_writes_mcp(temp_databases, temp_mcp_dir, mcp_client_factory):
    """Verify legacy flow (no LangGraph) still writes to MCP.

    This ensures backward compatibility: if escalation.decide() is called
    WITHOUT LangGraph orchestration, it should write to MCP directly.

    Flow:
    1. Create reservation directly in DB (no graph)
    2. Call escalation.decide() with skip_mcp_write=False
    3. Verify: MCP file created
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(temp_databases["sqlite"]),
    )

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    mcp_client = mcp_client_factory(temp_mcp_dir)

    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp_client)

    # Create reservation directly (no graph)
    start = _get_future_datetime(11, 10)
    end = _get_future_datetime(11, 18)

    reservation_id = chatbot.sql.create_reservation(
        first_name="Legacy",
        last_name="User",
        car_number="LEGACY-001",
        start_ts=start.replace(" ", "T") + ":00",
        end_ts=end.replace(" ", "T") + ":00",
        session_id=None  # No session = no graph
    )

    # Approve using legacy flow (skip_mcp_write=False, default)
    updated, decision = escalation.decide(
        reservation_id=reservation_id,
        raw_decision="approve",
        reason="Legacy approval",
        decided_by="legacy-admin",
        skip_mcp_write=False  # Explicit: write to MCP in legacy mode
    )

    # Verify MCP write happened
    mcp_file = temp_mcp_dir / "confirmed_reservations.txt"
    assert mcp_file.exists(), "Legacy flow didn't create MCP file"

    content = mcp_file.read_text()
    legacy_entries = [line for line in content.split('\n') if 'LEGACY-001' in line]

    assert len(legacy_entries) == 1, f"Legacy flow: expected 1 entry, got {len(legacy_entries)}"

    print(f"✓ Legacy flow: {len(legacy_entries)} MCP entry (expected 1)")
    print(f"  Backward compatibility preserved")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
