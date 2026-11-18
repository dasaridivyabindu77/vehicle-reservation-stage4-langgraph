"""Regression tests for duplicate reservation bug fix.

Bug: Reservation #17/#18 and #19/#20 were duplicates in confirmed_reservations.txt
Root Cause: Both escalation.decide() AND graph resume were writing to MCP
Fix: Skip MCP write in escalation.decide() when LangGraph is used
"""

import pytest
import os
from pathlib import Path
from datetime import datetime, timedelta

from app.config import Settings
from app.chatbot import Chatbot
from app.admin.agent import AdminAgent
from app.admin.escalation import EscalationService
from app.orchestration_v2 import build_production_graph, process_admin_decision_and_resume


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Set up test environment with temp databases."""
    # Set MCP data directory to temp
    mcp_dir = tmp_path / "mcp_data"
    mcp_dir.mkdir()
    monkeypatch.setenv("MCP_DATA_DIR", str(mcp_dir))

    return {
        "sqlite": tmp_path / "test.db",
        "checkpoint": tmp_path / "checkpoint.db",
        "mcp_dir": mcp_dir,
    }


def _future_time(days=7, hour=10):
    """Get future datetime string."""
    dt = datetime.now() + timedelta(days=days)
    return dt.strftime(f"%Y-%m-%d {hour:02d}:00")


def test_skip_mcp_write_flag_prevents_duplicate(test_env):
    """Verify skip_mcp_write=True prevents MCP write in escalation.decide().

    This tests the fix directly: escalation.decide() should NOT write to MCP
    when skip_mcp_write=True.
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(test_env["sqlite"]),
    )

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    # No MCP client - if decide() tries to write, it will fail
    escalation = EscalationService(chatbot.sql, admin, mcp_client=None)

    # Create reservation
    res_id = chatbot.sql.create_reservation(
        first_name="Test",
        last_name="User",
        car_number="SKIP-TEST",
        start_ts=_future_time(7, 10).replace(" ", "T") + ":00",
        end_ts=_future_time(7, 18).replace(" ", "T") + ":00",
        session_id="test-session"
    )

    # Approve with skip_mcp_write=True (should not crash even without MCP client)
    updated, decision = escalation.decide(
        reservation_id=res_id,
        raw_decision="approve",
        reason="Test",
        decided_by="test",
        skip_mcp_write=True  # KEY: Skip MCP write
    )

    assert updated.status == "confirmed"
    print("✓ skip_mcp_write=True prevented MCP write attempt")


def test_admin_routes_flow_uses_skip_flag(test_env):
    """Simulate admin routes.py flow to verify no duplicate writes.

    This tests the complete flow:
    1. User makes reservation (graph creates checkpoint)
    2. Admin approves via escalation.decide(skip_mcp_write=True)
    3. Graph resumes and writes to MCP once
    4. Verify: Only ONE MCP entry
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(test_env["sqlite"]),
        checkpoint_db=str(test_env["checkpoint"]),
        mcp_enabled=True,
    )

    # Mock MCP client that writes locally
    class MockMCP:
        def __init__(self, data_dir):
            self.data_dir = Path(data_dir)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.write_count = 0

        def is_available(self):
            return True

        def write_confirmed_reservation(self, **kwargs):
            self.write_count += 1
            file_path = self.data_dir / "confirmed_reservations.txt"

            entry = f"#{kwargs['reservation_id']} | {kwargs['first_name']} {kwargs['last_name']} | {kwargs['car_number']}\n"

            with open(file_path, 'a') as f:
                f.write(entry)

            return {"success": True, "file_path": str(file_path)}

    mcp = MockMCP(test_env["mcp_dir"])

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)
    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp)

    graph = build_production_graph(
        chatbot=chatbot,
        escalation_service=escalation,
        mcp_client=mcp,
        checkpoint_db=str(test_env["checkpoint"]),
    )

    session_id = "admin-flow-test"
    config = {"configurable": {"thread_id": session_id}}

    # Step 1: User makes reservation
    messages = [
        "I want to book parking",
        "AdminFlow",
        "Test",
        "ADMIN001",
        _future_time(8, 10),
        _future_time(8, 18),
    ]

    for msg in messages:
        graph.invoke({"user_message": msg, "session_id": session_id}, config)

    res_id = escalation.pending()[0].id

    # Step 2: Admin approves (simulating routes.py)
    # This is the FIX: skip_mcp_write=True
    escalation.decide(
        reservation_id=res_id,
        raw_decision="approve",
        reason="Admin approval",
        decided_by="admin-dashboard",
        skip_mcp_write=True  # KEY FIX
    )

    # Step 3: Resume graph (writes to MCP)
    process_admin_decision_and_resume(
        graph=graph,
        thread_id=session_id,
        decision="approved",
        reason="Admin approval",
        decided_by="admin-dashboard"
    )

    # Verify: Only ONE write
    assert mcp.write_count == 1, f"DUPLICATE BUG: MCP written {mcp.write_count} times (expected 1)"

    # Verify file content
    mcp_file = test_env["mcp_dir"] / "confirmed_reservations.txt"
    content = mcp_file.read_text()
    entries = [line for line in content.split('\n') if 'ADMIN001' in line]

    assert len(entries) == 1, f"File has {len(entries)} entries (expected 1)"

    print(f"✓ Admin routes flow: {mcp.write_count} MCP write, {len(entries)} file entry")


def test_legacy_flow_without_graph_writes_once(test_env):
    """Verify legacy flow (no LangGraph) writes to MCP correctly.

    When skip_mcp_write=False (default), escalation.decide() writes to MCP.
    This preserves backward compatibility.
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(test_env["sqlite"]),
    )

    class MockMCP:
        def __init__(self):
            self.write_count = 0

        def is_available(self):
            return True

        def write_confirmed_reservation(self, **kwargs):
            self.write_count += 1
            return {"success": True}

    mcp = MockMCP()

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)
    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp)

    # Create reservation WITHOUT graph (no session_id)
    res_id = chatbot.sql.create_reservation(
        first_name="Legacy",
        last_name="User",
        car_number="LEGACY-001",
        start_ts=_future_time(9, 10).replace(" ", "T") + ":00",
        end_ts=_future_time(9, 18).replace(" ", "T") + ":00",
        session_id=None  # No session = no graph
    )

    # Approve with skip_mcp_write=False (legacy flow)
    escalation.decide(
        reservation_id=res_id,
        raw_decision="approve",
        reason="Legacy",
        decided_by="legacy",
        skip_mcp_write=False  # Default: write to MCP
    )

    assert mcp.write_count == 1, f"Legacy flow: {mcp.write_count} writes (expected 1)"
    print(f"✓ Legacy flow: {mcp.write_count} MCP write (backward compatible)")


def test_multiple_users_no_cross_contamination(test_env):
    """Verify multiple concurrent users don't create duplicates.

    Flow:
    1. User A makes reservation
    2. User B makes reservation
    3. Admin approves both
    4. Verify: Each has exactly ONE MCP entry
    """
    settings = Settings(
        dial_api_key="",
        sqlite_path=str(test_env["sqlite"]),
        checkpoint_db=str(test_env["checkpoint"]),
        mcp_enabled=True,
    )

    class MockMCP:
        def __init__(self):
            self.writes = []  # Track all writes

        def is_available(self):
            return True

        def write_confirmed_reservation(self, **kwargs):
            self.writes.append(kwargs['car_number'])
            return {"success": True}

    mcp = MockMCP()

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)
    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp)

    graph = build_production_graph(
        chatbot=chatbot,
        escalation_service=escalation,
        mcp_client=mcp,
        checkpoint_db=str(test_env["checkpoint"]),
    )

    # User A
    for msg in ["Book", "UserA", "TestA", "MULTIA", _future_time(10, 9), _future_time(10, 17)]:
        graph.invoke(
            {"user_message": msg, "session_id": "user-a"},
            {"configurable": {"thread_id": "user-a"}},
        )

    # User B
    for msg in ["Reserve", "UserB", "TestB", "MULTIB", _future_time(10, 10), _future_time(10, 18)]:
        graph.invoke(
            {"user_message": msg, "session_id": "user-b"},
            {"configurable": {"thread_id": "user-b"}},
        )

    # Get reservations
    pending = escalation.pending()
    res_a = [r for r in pending if r.car_number == "MULTIA"][0]
    res_b = [r for r in pending if r.car_number == "MULTIB"][0]

    # Approve both
    for res, session in [(res_a, "user-a"), (res_b, "user-b")]:
        escalation.decide(res.id, "approve", decided_by="test", skip_mcp_write=True)
        process_admin_decision_and_resume(graph, session, "approved", decided_by="test")

    # Verify: Each written once
    assert mcp.writes.count("MULTIA") == 1, f"User A: {mcp.writes.count('MULTIA')} writes"
    assert mcp.writes.count("MULTIB") == 1, f"User B: {mcp.writes.count('MULTIB')} writes"

    print(f"✓ Multiple users: A={mcp.writes.count('MULTIA')}, B={mcp.writes.count('MULTIB')} (expected 1 each)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
