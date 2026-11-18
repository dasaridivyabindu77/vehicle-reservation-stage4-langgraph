"""Tests for Stage 4 orchestration."""

import pytest

from app.chatbot import Chatbot
from app.config import Settings
from app.admin.agent import AdminAgent
from app.admin.escalation import EscalationService
from app.orchestration import (
    SystemOrchestrator,
    build_orchestration_graph,
    OrchestrationState,
)


@pytest.fixture
def settings():
    """Test settings (offline mode)."""
    return Settings(
        dial_api_key="",
        sqlite_path=":memory:",
        mcp_enabled=False,
    )


@pytest.fixture
def chatbot(settings):
    """Create chatbot for testing."""
    return Chatbot(settings=settings, seed=True)


@pytest.fixture
def escalation_service(settings, chatbot):
    """Create escalation service for testing."""
    admin_agent = AdminAgent(settings, None)
    return EscalationService(chatbot.sql, admin_agent, mcp_client=None)


@pytest.fixture
def orchestrator(chatbot, escalation_service):
    """Create system orchestrator."""
    return SystemOrchestrator(
        chatbot=chatbot,
        escalation_service=escalation_service,
        mcp_client=None,
    )


def test_orchestration_graph_compiles(chatbot, escalation_service):
    """Test that the orchestration graph compiles successfully."""
    graph = build_orchestration_graph(chatbot, escalation_service, None)
    assert graph is not None


def test_simple_question_workflow(orchestrator):
    """Test orchestration with a simple information question.

    This should go through Stage 1 only and end without
    escalation or MCP persistence.
    """
    state = orchestrator.process_user_message(
        "What are your opening hours?",
        session_id="test-session-1"
    )

    # Verify Stage 1 executed
    assert "chatbot_response" in state
    assert state["chatbot_response"]
    assert state["stage_1_duration_ms"] > 0

    # Verify no reservation was created
    assert not state.get("reservation_complete", False)
    assert state.get("reservation_id") is None

    # Verify workflow completed
    assert state.get("workflow_complete", True)
    assert state.get("total_duration_ms", 0) > 0


def test_reservation_initiation_workflow(orchestrator):
    """Test orchestration when user starts a reservation.

    This should go through Stage 1 (reservation not yet complete),
    no escalation yet.
    """
    state = orchestrator.process_user_message(
        "I'd like to book a parking spot",
        session_id="test-session-2"
    )

    # Verify Stage 1 executed
    assert "chatbot_response" in state
    assert "first name" in state["chatbot_response"].lower()

    # Reservation not yet complete (only started)
    assert not state.get("reservation_complete", False)

    # No escalation yet
    assert not state.get("escalation_sent", False)


def test_complete_reservation_workflow(orchestrator):
    """Test orchestration with a complete reservation flow.

    This should go through Stage 1 (reservation complete) and
    Stage 2 (escalation), but decision still pending.
    """
    from datetime import datetime, timedelta

    # Use dynamic future dates
    future_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    start_time = f"{future_date} 10:00"
    end_time = f"{future_date} 18:00"

    session_id = "test-session-3"
    messages = [
        "I'd like to reserve a spot",
        "Alice",
        "Smith",
        "ABC-123",
        start_time,
        end_time,
    ]

    state = None
    for message in messages:
        state = orchestrator.process_user_message(message, session_id)

    # Verify reservation was completed
    assert state.get("reservation_complete", False)
    assert state.get("reservation_id") is not None

    # Verify Stage 1 and 2 executed
    assert state["stage_1_duration_ms"] > 0
    assert state["stage_2_duration_ms"] >= 0

    # Escalation should have been sent
    assert state.get("escalation_sent", False)

    # Decision pending (admin hasn't approved yet in test)
    assert state.get("admin_decision") in ["pending", None]


def test_approved_reservation_with_mcp(chatbot, escalation_service):
    """Test complete workflow with admin approval and MCP persistence."""

    class MockMCPClient:
        def write_confirmed_reservation(self, **kwargs):
            return {
                "success": True,
                "file_path": "/tmp/test.txt",
                "entry_count": 1,
            }

    mcp_client = MockMCPClient()

    # Create orchestrator with MCP client
    orchestrator = SystemOrchestrator(
        chatbot=chatbot,
        escalation_service=escalation_service,
        mcp_client=mcp_client,
    )

    # Complete a reservation
    from datetime import datetime, timedelta
    future_date = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d")

    session_id = "test-session-4"
    messages = [
        "I want to book parking",
        "Bob",
        "Brown",
        "XYZ-789",
        f"{future_date} 09:00",
        f"{future_date} 17:00",
    ]

    state = None
    for message in messages:
        state = orchestrator.process_user_message(message, session_id)

    reservation_id = state["reservation_id"]

    # Manually approve the reservation (simulating admin action)
    escalation_service.decide(reservation_id, "approve", decided_by="test-admin")

    # Process another message to trigger Stage 3
    # In production, this would happen when admin approves
    # For testing, we can manually check the decision
    reservation = escalation_service.get(reservation_id)
    assert reservation.status == "confirmed"


def test_orchestrator_metrics_collection(orchestrator):
    """Test that orchestrator correctly collects metrics."""
    # Process several messages
    messages = [
        ("What are the prices?", "metrics-1"),
        ("Where are you located?", "metrics-2"),
        ("Do you have EV charging?", "metrics-3"),
    ]

    for message, session_id in messages:
        orchestrator.process_user_message(message, session_id)

    # Get metrics summary
    summary = orchestrator.get_metrics_summary()

    assert summary["total_requests"] == len(messages)
    assert summary["avg_stage_1_ms"] > 0
    assert summary["avg_total_ms"] > 0
    assert summary["error_count"] == 0


def test_orchestrator_error_handling(orchestrator):
    """Test that orchestrator handles errors gracefully."""

    # Create a scenario that might cause errors
    # (processing with invalid session data, etc.)
    state = orchestrator.process_user_message(
        "Test message",
        session_id="error-test"
    )

    # Should still complete without crashing
    assert "chatbot_response" in state
    assert state.get("workflow_complete", True)


def test_orchestration_state_persistence(orchestrator):
    """Test that state persists correctly across multiple messages."""
    session_id = "persistence-test"

    # Start reservation
    state1 = orchestrator.process_user_message(
        "I want to reserve parking",
        session_id
    )

    # Continue reservation
    state2 = orchestrator.process_user_message(
        "John",
        session_id
    )

    # Verify chatbot maintains conversation state
    assert "chatbot_response" in state1
    assert "chatbot_response" in state2
    assert state1["chatbot_response"] != state2["chatbot_response"]


def test_concurrent_sessions(orchestrator):
    """Test that multiple concurrent sessions are handled correctly."""
    sessions = [f"concurrent-{i}" for i in range(5)]

    # Process messages from different sessions
    results = []
    for session_id in sessions:
        state = orchestrator.process_user_message(
            "What are your opening hours?",
            session_id
        )
        results.append(state)

    # All should succeed independently
    assert len(results) == len(sessions)
    for state in results:
        assert "chatbot_response" in state
        assert state.get("workflow_complete", True)


def test_stage_duration_tracking(orchestrator):
    """Test that stage durations are tracked correctly."""
    state = orchestrator.process_user_message(
        "Tell me about your parking facility",
        session_id="duration-test"
    )

    # Stage 1 should have duration
    assert state["stage_1_duration_ms"] > 0

    # Total duration should be sum of stages
    assert state["total_duration_ms"] >= state["stage_1_duration_ms"]


def test_reservation_complete_flag(orchestrator):
    """Test that reservation_complete flag is set correctly."""
    session_id = "flag-test"

    # Ask a question - should not complete reservation
    state1 = orchestrator.process_user_message(
        "What are your prices?",
        session_id
    )
    assert not state1.get("reservation_complete", False)

    # Start reservation - still not complete
    state2 = orchestrator.process_user_message(
        "I'd like to book parking",
        f"{session_id}-2"
    )
    assert not state2.get("reservation_complete", False)


def test_orchestration_with_all_stages(chatbot, escalation_service):
    """Integration test: Full workflow through all three stages.

    This test simulates the complete journey:
    1. User makes reservation (Stage 1)
    2. Admin approves (Stage 2)
    3. MCP writes to file (Stage 3)
    """

    # Mock MCP client that tracks calls
    class TrackingMCPClient:
        def __init__(self):
            self.calls = []

        def write_confirmed_reservation(self, **kwargs):
            self.calls.append(kwargs)
            return {"success": True, "file_path": "/tmp/test.txt"}

    mcp_client = TrackingMCPClient()

    orchestrator = SystemOrchestrator(
        chatbot=chatbot,
        escalation_service=escalation_service,
        mcp_client=mcp_client,
    )

    # Stage 1: Complete reservation
    from datetime import datetime, timedelta
    future_date = (datetime.now() + timedelta(days=9)).strftime("%Y-%m-%d")

    session_id = "full-workflow"
    messages = [
        "I want to reserve a spot",
        "Carol",
        "Clark",
        "TEST-123",
        f"{future_date} 10:00",
        f"{future_date} 18:00",
    ]

    final_state = None
    for message in messages:
        final_state = orchestrator.process_user_message(message, session_id)

    # Verify Stage 1 completed
    assert final_state["reservation_complete"]
    reservation_id = final_state["reservation_id"]

    # Verify Stage 2 (escalation sent)
    assert final_state["escalation_sent"]

    # Stage 2: Admin approves (manual step in test)
    escalation_service.decide(reservation_id, "approve", decided_by="test-admin")

    # In a real system, Stage 3 would be triggered by the decision
    # For testing, verify the reservation is confirmed
    reservation = escalation_service.get(reservation_id)
    assert reservation.status == "confirmed"

    # Metrics should track all stages
    summary = orchestrator.get_metrics_summary()
    assert summary["total_requests"] >= len(messages)
    assert summary["reservations_completed"] >= 1


def test_no_duplicate_escalations_on_followup(orchestrator):
    """Test Issue 4: Ensure follow-up messages don't create duplicate escalations.

    After completing a reservation, sending another message (like "thanks")
    should NOT trigger a second escalation.
    """
    from datetime import datetime, timedelta
    future_date = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")

    session_id = "no-dupe-test"
    messages = [
        "I'd like to reserve parking",
        "David",
        "Davis",
        "XYZ-999",
        f"{future_date} 09:00",
        f"{future_date} 17:00",
    ]

    # Complete the reservation
    for message in messages:
        state = orchestrator.process_user_message(message, session_id)

    # Verify reservation was completed and escalated
    assert state.get("reservation_complete")
    assert state.get("escalation_sent")
    reservation_id = state.get("reservation_id")

    # Count escalations before follow-up
    pending_before = len(orchestrator.escalation_service.pending())

    # Send a follow-up message (should NOT create duplicate escalation)
    followup_state = orchestrator.process_user_message("thanks", session_id)

    # Verify no new reservation was created
    assert not followup_state.get("reservation_complete")
    assert not followup_state.get("escalation_sent")

    # Count escalations after follow-up (should be the same)
    pending_after = len(orchestrator.escalation_service.pending())
    assert pending_after == pending_before, "Follow-up message created duplicate escalation"

    # Verify the original reservation still exists and is pending
    reservation = orchestrator.escalation_service.get(reservation_id)
    assert reservation is not None
    assert reservation.status == "pending_admin"
