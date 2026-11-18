"""Integration tests for Stage 3 - end-to-end reservation flow with MCP server."""

from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from app.chatbot import Chatbot
from app.config import Settings
from app.admin.escalation import EscalationService
from app.admin.agent import AdminAgent
from app.sql_store import DynamicDataStore, STATUS_CONFIRMED, STATUS_REFUSED
from mcp_server.client import MCPClient


def _get_future_timestamp(days_ahead: int, hour: int = 10, minute: int = 0) -> str:
    """Generate a future ISO timestamp for testing.

    Args:
        days_ahead: Number of days in the future
        hour: Hour of day (0-23)
        minute: Minute (0-59)

    Returns:
        ISO timestamp string: "YYYY-MM-DDTHH:MM:SS"
    """
    future_dt = datetime.now() + timedelta(days=days_ahead)
    return future_dt.strftime(f"%Y-%m-%dT{hour:02d}:{minute:02d}:00")


@pytest.fixture
def test_reservations_file(tmp_path, monkeypatch):
    """Use a temporary file for confirmed reservations."""
    test_file = tmp_path / "test_reservations.txt"

    # Monkey-patch the RESERVATIONS_FILE in the server module
    import mcp_server.server as server_module
    original_file = server_module.RESERVATIONS_FILE
    server_module.RESERVATIONS_FILE = test_file

    yield test_file

    # Restore
    server_module.RESERVATIONS_FILE = original_file

    if test_file.exists():
        test_file.unlink()


@pytest.fixture
def settings():
    """Test settings with MCP enabled."""
    return Settings(
        dial_api_key="",  # offline mode
        sqlite_path=":memory:",
        mcp_server_url="http://127.0.0.1:8001",
        mcp_server_token="",
        mcp_enabled=True,
    )


@pytest.fixture
def mock_mcp_client(test_reservations_file, monkeypatch):
    """Mock MCP client that writes to the test file."""

    class MockMCPClient:
        def __init__(self, base_url=None, token=None):
            self.base_url = base_url
            self.token = token
            self.file = test_reservations_file

        def write_confirmed_reservation(
            self,
            reservation_id,
            first_name,
            last_name,
            car_number,
            start_ts,
            end_ts,
            approved_by="admin",
            approval_time=None,
        ):
            approval_time = approval_time or datetime.now(timezone.utc).isoformat()
            full_name = f"{first_name} {last_name}"
            reservation_period = f"{start_ts} to {end_ts}"
            # Updated format: #ID | Name | Car Number | Reservation Period | Approval Time
            entry = f"#{reservation_id} | {full_name} | {car_number} | {reservation_period} | {approval_time}\n"

            with self.file.open("a", encoding="utf-8") as f:
                f.write(entry)

            with self.file.open("r", encoding="utf-8") as f:
                entry_count = len(f.readlines())

            return {
                "success": True,
                "message": f"Reservation #{reservation_id} written successfully",
                "file_path": str(self.file.absolute()),
                "entry_count": entry_count,
            }

        def is_available(self):
            return True

        def health_check(self):
            return {"status": "healthy"}

    return MockMCPClient()


def test_stage3_confirmed_reservation_writes_to_file(
    settings, mock_mcp_client, test_reservations_file
):
    """Test that confirming a reservation writes to file via MCP server."""
    # Create escalation service with mock MCP client
    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()

    admin_agent = AdminAgent(settings, None)  # offline mode
    escalation = EscalationService(sql, admin_agent, mock_mcp_client)

    # Create a reservation with dynamic future dates
    reservation_id = sql.create_reservation(
        first_name="Alice",
        last_name="Anderson",
        car_number="AAA-111",
        start_ts=_get_future_timestamp(days_ahead=7, hour=10),
        end_ts=_get_future_timestamp(days_ahead=7, hour=18),
        session_id="test-session",
    )

    # Approve the reservation
    updated, decision = escalation.decide(
        reservation_id, "approve", reason="Looks good", decided_by="test-admin"
    )

    assert updated.status == STATUS_CONFIRMED
    assert decision.status == STATUS_CONFIRMED

    # Verify file was written
    assert test_reservations_file.exists()
    content = test_reservations_file.read_text()

    # Verify format: #ID | Name | Car Number | Reservation Period | Approval Time
    assert f"#{reservation_id}" in content
    assert "Alice Anderson" in content
    assert "AAA-111" in content
    assert "T10:00:00 to" in content
    assert "T18:00:00" in content

    # Verify the format is correct
    parts = content.strip().split(" | ")
    assert len(parts) == 5  # Now includes reservation_id


def test_stage3_refused_reservation_not_written(
    settings, mock_mcp_client, test_reservations_file
):
    """Test that refused reservations are NOT written to file."""
    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()

    admin_agent = AdminAgent(settings, None)
    escalation = EscalationService(sql, admin_agent, mock_mcp_client)

    # Create a reservation
    reservation_id = sql.create_reservation(
        first_name="Bob",
        last_name="Brown",
        car_number="BBB-222",
        start_ts=_get_future_timestamp(days_ahead=8, hour=10),
        end_ts=_get_future_timestamp(days_ahead=8, hour=18),
        session_id="test-session",
    )

    # Refuse the reservation
    updated, decision = escalation.decide(
        reservation_id,
        "refuse",
        reason="No availability",
        decided_by="test-admin",
    )

    assert updated.status == STATUS_REFUSED
    assert decision.status == STATUS_REFUSED

    # Verify file was NOT created (or is empty)
    if test_reservations_file.exists():
        content = test_reservations_file.read_text()
        assert "Bob Brown" not in content
        assert "BBB-222" not in content


def test_stage3_multiple_confirmed_reservations(
    settings, mock_mcp_client, test_reservations_file
):
    """Test multiple confirmed reservations are all written to file."""
    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()

    admin_agent = AdminAgent(settings, None)
    escalation = EscalationService(sql, admin_agent, mock_mcp_client)

    reservations_data = [
        ("Charlie", "Clark", "CCC-333", 9),
        ("Diana", "Davis", "DDD-444", 10),
        ("Eve", "Evans", "EEE-555", 11),
    ]

    for first, last, car, days_ahead in reservations_data:
        reservation_id = sql.create_reservation(
            first_name=first,
            last_name=last,
            car_number=car,
            start_ts=_get_future_timestamp(days_ahead=days_ahead, hour=10),
            end_ts=_get_future_timestamp(days_ahead=days_ahead, hour=18),
            session_id="test-session",
        )
        escalation.decide(reservation_id, "approve", decided_by="test-admin")

    # Verify all are in the file
    content = test_reservations_file.read_text()
    assert "Charlie Clark" in content
    assert "Diana Davis" in content
    assert "Eve Evans" in content
    assert "CCC-333" in content
    assert "DDD-444" in content
    assert "EEE-555" in content

    # Count entries
    lines = [line for line in content.split("\n") if line.strip()]
    assert len(lines) == 3


def test_stage3_without_mcp_client_still_works(settings):
    """Test that the system works without MCP client (Stage 1+2 still functional)."""
    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()

    admin_agent = AdminAgent(settings, None)
    # No MCP client provided
    escalation = EscalationService(sql, admin_agent, mcp_client=None)

    reservation_id = sql.create_reservation(
        first_name="Frank",
        last_name="Foster",
        car_number="FFF-666",
        start_ts=_get_future_timestamp(days_ahead=12, hour=10),
        end_ts=_get_future_timestamp(days_ahead=12, hour=18),
        session_id="test-session",
    )

    # Should work without MCP client - just won't write to file
    updated, decision = escalation.decide(
        reservation_id, "approve", decided_by="test-admin"
    )

    assert updated.status == STATUS_CONFIRMED
    assert decision.status == STATUS_CONFIRMED
    # File writing is simply skipped - no error


def test_stage3_mcp_failure_doesnt_block_approval(
    settings, test_reservations_file, monkeypatch
):
    """Test that MCP write failures don't prevent reservation approval."""

    class FailingMCPClient:
        def write_confirmed_reservation(self, **kwargs):
            raise Exception("MCP server unreachable")

        def is_available(self):
            return True

    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()

    admin_agent = AdminAgent(settings, None)
    escalation = EscalationService(sql, admin_agent, FailingMCPClient())

    reservation_id = sql.create_reservation(
        first_name="Grace",
        last_name="Green",
        car_number="GGG-777",
        start_ts=_get_future_timestamp(days_ahead=13, hour=10),
        end_ts=_get_future_timestamp(days_ahead=13, hour=18),
        session_id="test-session",
    )

    # Should succeed despite MCP failure
    updated, decision = escalation.decide(
        reservation_id, "approve", decided_by="test-admin"
    )

    assert updated.status == STATUS_CONFIRMED
    # The reservation is approved even though file write failed


def test_stage3_end_to_end_with_chatbot(settings, mock_mcp_client, test_reservations_file):
    """Test full Stage 1+2+3 flow through the chatbot."""
    # Build escalation service with MCP
    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()
    admin_agent = AdminAgent(settings, None)
    escalation = EscalationService(sql, admin_agent, mock_mcp_client)

    # Create chatbot with custom escalation service
    bot = Chatbot(settings=settings, seed=True, escalation=escalation)

    # Simulate reservation flow
    session = "e2e-test"

    # User makes a reservation through chatbot conversation
    # (Simplified - in real flow this goes through multiple turns)
    reservation_id = sql.create_reservation(
        first_name="Hannah",
        last_name="Hill",
        car_number="HHH-888",
        start_ts=_get_future_timestamp(days_ahead=14, hour=10),
        end_ts=_get_future_timestamp(days_ahead=14, hour=18),
        session_id=session,
    )

    # Admin approves via escalation service
    updated, decision = bot.escalation.decide(
        reservation_id, "approve", decided_by="admin"
    )

    assert updated.status == STATUS_CONFIRMED

    # Verify Stage 3: file was written
    assert test_reservations_file.exists()
    content = test_reservations_file.read_text()
    assert "Hannah Hill" in content
    assert "HHH-888" in content


def test_stage3_file_format_exact_compliance(settings, mock_mcp_client, test_reservations_file):
    """Test exact compliance with Stage 3 format specification."""
    sql = DynamicDataStore(settings.sqlite_path)
    sql.seed()

    admin_agent = AdminAgent(settings, None)
    escalation = EscalationService(sql, admin_agent, mock_mcp_client)

    reservation_id = sql.create_reservation(
        first_name="Ian",
        last_name="Ingram",
        car_number="III-999",
        start_ts=_get_future_timestamp(days_ahead=15, hour=14, minute=30),
        end_ts=_get_future_timestamp(days_ahead=15, hour=17, minute=45),
        session_id="test-session",
    )

    escalation.decide(reservation_id, "approve", decided_by="admin")

    content = test_reservations_file.read_text().strip()

    # Updated Stage 3 spec: #ID | Name | Car Number | Reservation Period | Approval Time
    assert " | " in content

    parts = content.split(" | ")
    assert len(parts) == 5, f"Expected 5 parts, got {len(parts)}: {parts}"

    res_id, name, car_number, period, approval_time = parts

    assert res_id == f"#{reservation_id}"
    assert name == "Ian Ingram"
    assert car_number == "III-999"
    assert "T14:30:00" in period
    assert "T17:45:00" in period
    assert "to" in period
    # Approval time should be an ISO timestamp
    assert len(approval_time) > 10  # Basic sanity check for timestamp
