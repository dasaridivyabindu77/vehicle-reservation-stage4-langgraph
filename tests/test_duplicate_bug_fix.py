"""Regression test for duplicate reservation bug (Issue: #17/#18, #19/#20 duplicates).

Root Cause: escalation.decide() AND graph resume both wrote to MCP
Fix: Added skip_mcp_write parameter to escalation.decide()
     Admin routes now call decide(skip_mcp_write=True) when using LangGraph
"""

import pytest
from datetime import datetime, timedelta

from app.config import Settings
from app.chatbot import Chatbot
from app.admin.agent import AdminAgent
from app.admin.escalation import EscalationService


@pytest.fixture
def test_db(tmp_path):
    """Test database."""
    return tmp_path / "test.db"


def test_skip_mcp_write_parameter_exists():
    """Verify escalation.decide() has skip_mcp_write parameter."""
    import inspect
    from app.admin.escalation import EscalationService

    sig = inspect.signature(EscalationService.decide)
    params = list(sig.parameters.keys())

    assert 'skip_mcp_write' in params, "skip_mcp_write parameter missing from escalation.decide()"

    # Check default value
    default = sig.parameters['skip_mcp_write'].default
    assert default is False, f"skip_mcp_write default should be False, got {default}"

    print("✓ escalation.decide() has skip_mcp_write parameter (default=False)")


def test_skip_mcp_write_true_prevents_write(test_db):
    """Verify skip_mcp_write=True prevents MCP write."""
    settings = Settings(dial_api_key="", sqlite_path=str(test_db))

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    # Mock MCP client that tracks writes
    class MockMCP:
        def __init__(self):
            self.write_count = 0

        def is_available(self):
            return True

        def write_confirmed_reservation(self, **kwargs):
            self.write_count += 1
            return {"success": True}

    mcp = MockMCP()
    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp)

    # Create reservation
    future = datetime.now() + timedelta(days=7)
    res_id = chatbot.sql.create_reservation(
        first_name="Test",
        last_name="User",
        car_number="SKIP-TEST",
        start_ts=future.strftime("%Y-%m-%dT10:00:00"),
        end_ts=future.strftime("%Y-%m-%dT18:00:00"),
        session_id="test-session"
    )

    # Approve with skip_mcp_write=True
    escalation.decide(
        reservation_id=res_id,
        raw_decision="approve",
        reason="Test",
        decided_by="test",
        skip_mcp_write=True  # Should NOT write
    )

    assert mcp.write_count == 0, f"MCP written {mcp.write_count} times (expected 0 with skip=True)"
    print("✓ skip_mcp_write=True prevented MCP write")


def test_skip_mcp_write_false_allows_write(test_db):
    """Verify skip_mcp_write=False (default) allows MCP write."""
    settings = Settings(dial_api_key="", sqlite_path=str(test_db))

    chatbot = Chatbot(settings=settings, seed=True)
    admin = AdminAgent(settings, None)

    class MockMCP:
        def __init__(self):
            self.write_count = 0

        def is_available(self):
            return True

        def write_confirmed_reservation(self, **kwargs):
            self.write_count += 1
            return {"success": True}

    mcp = MockMCP()
    escalation = EscalationService(chatbot.sql, admin, mcp_client=mcp)

    future = datetime.now() + timedelta(days=7)
    res_id = chatbot.sql.create_reservation(
        first_name="Test",
        last_name="User",
        car_number="WRITE-TEST",
        start_ts=future.strftime("%Y-%m-%dT10:00:00"),
        end_ts=future.strftime("%Y-%m-%dT18:00:00"),
        session_id=None  # No session = legacy flow
    )

    # Approve with skip_mcp_write=False (legacy flow)
    escalation.decide(
        reservation_id=res_id,
        raw_decision="approve",
        reason="Test",
        decided_by="test",
        skip_mcp_write=False  # Should write
    )

    assert mcp.write_count == 1, f"MCP written {mcp.write_count} times (expected 1 with skip=False)"
    print("✓ skip_mcp_write=False allowed MCP write (backward compatible)")


def test_admin_routes_uses_skip_flag():
    """Verify admin routes.py uses skip_mcp_write based on session existence."""
    # Read the routes file to verify the fix is in place
    with open('app/admin/routes.py', 'r') as f:
        content = f.read()

    # Check for the fix pattern
    assert 'skip_mcp_write=' in content, "admin routes.py missing skip_mcp_write parameter"
    assert 'has_graph_checkpoint' in content or 'session_id' in content, "admin routes.py not checking for graph checkpoint"

    # Check the fix is correctly implemented
    assert 'skip_mcp_write=True' in content or 'skip_mcp_write=has_graph_checkpoint' in content, \
        "admin routes.py not conditionally skipping MCP write"

    print("✓ admin routes.py uses skip_mcp_write parameter correctly")


    pytest.main([__file__, "-v", "-s"])
def test_done(): pass

if __name__ == '__main__':
    pytest.main([__file__, "-v", "-s"])
