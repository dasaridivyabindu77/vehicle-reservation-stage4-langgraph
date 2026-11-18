"""Test MCP end-to-end flow with actual MCP server - in process for debugging."""

import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import requests

from app.admin.agent import AdminAgent
from app.admin.escalation import EscalationService
from app.config import Settings
from app.orchestration_v2 import build_production_graph, process_admin_decision_and_resume
from app.sql_store import DynamicDataStore


@pytest.fixture(scope="module")
def mcp_server():
    """Start MCP server."""
    mcp_port = 8095
    mcp_data_dir = tempfile.mkdtemp(prefix="mcp_test_")

    process = subprocess.Popen(
        [
            "python", "-m", "uvicorn",
            "mcp_server.server:app",
            "--host", "127.0.0.1",
            "--port", str(mcp_port),
            "--log-level", "info",
        ],
        env={
            **subprocess.os.environ,
            "MCP_DATA_DIR": mcp_data_dir,
            "MCP_SERVER_TOKEN": "test-mcp-token",
        },
    )

    # Wait for server
    for i in range(30):
        try:
            response = requests.get(f"http://127.0.0.1:{mcp_port}/health", timeout=1)
            if response.status_code == 200:
                print(f"\n✓ MCP server started on port {mcp_port}")
                break
        except:
            time.sleep(0.5)
    else:
        process.kill()
        pytest.skip("MCP server failed to start")

    yield {
        "url": f"http://127.0.0.1:{mcp_port}",
        "data_dir": mcp_data_dir,
    }

    process.terminate()
    process.wait(timeout=5)


def test_mcp_e2e_in_process(mcp_server):
    """Test complete MCP flow in-process with real MCP server."""
    print("\n" + "="*70)
    print("IN-PROCESS MCP E2E TEST")
    print("="*70)

    # Setup
    checkpoint_db = tempfile.NamedTemporaryFile(suffix="_checkpoints.db", delete=False).name
    sqlite_db = tempfile.NamedTemporaryFile(suffix="_citypark.db", delete=False).name

    settings = Settings(
        llm_provider="dial",
        dial_api_key="",
        sqlite_path=sqlite_db,
        checkpoint_db=checkpoint_db,
        mcp_server_url=mcp_server["url"],
        mcp_server_token="test-mcp-token",
        mcp_enabled=True,
    )

    sql_store = DynamicDataStore(sqlite_db)
    sql_store.seed()

    session_id = "test-mcp-e2e-1"

    # Phase 1: Create reservation and escalate
    print("\n--- Phase 1: Create and Escalate ---")

    reservation_id = sql_store.create_reservation(
        first_name="MCP",
        last_name="Test",
        car_number="MCP-E2E-001",
        start_ts="2026-11-19T10:00:00Z",
        end_ts="2026-11-19T12:00:00Z",
        zone_id="A",
        status="pending_admin",
        session_id=session_id,
    )
    print(f"✓ Reservation #{reservation_id} created")

    # Build graph with MCP enabled
    graph = build_production_graph(settings, checkpointer_db=checkpoint_db)
    config = {"configurable": {"thread_id": session_id}}

    # Invoke graph - should hit interrupt
    try:
        state = graph.invoke(
            {"user_message": "Confirm booking", "session_id": session_id},
            config=config
        )
        print(f"⚠ No interrupt raised - unexpected")
    except Exception as e:
        if "GraphInterrupt" in str(type(e)):
            print(f"✓ Graph interrupted as expected")
        else:
            raise

    # Phase 2: Admin approves
    print("\n--- Phase 2: Admin Approval ---")

    admin_agent = AdminAgent(settings)
    escalation_service = EscalationService(sql_store, admin_agent)

    updated_res, decision = escalation_service.decide(
        reservation_id,
        "approve",
        reason="Test approval",
        decided_by="test-admin"
    )
    print(f"✓ Admin approved: status={updated_res.status}")

    # Phase 3: Resume graph
    print("\n--- Phase 3: Resume Graph ---")

    final_state = process_admin_decision_and_resume(
        graph=graph,
        thread_id=session_id,
        decision="approved",
        reason="Test approval",
        decided_by="test-admin"
    )

    print(f"✓ Graph resumed")
    print(f"  admin_decision: {final_state.get('admin_decision')}")
    print(f"  mcp_write_attempted: {final_state.get('mcp_write_attempted')}")
    print(f"  mcp_write_success: {final_state.get('mcp_write_success')}")

    # Phase 4: Verify MCP file
    print("\n--- Phase 4: Verify MCP File ---")

    mcp_file = Path(mcp_server["data_dir"]) / "confirmed_reservations.txt"

    # Give MCP time to write
    time.sleep(1)

    if mcp_file.exists():
        content = mcp_file.read_text(encoding="utf-8")
        print(f"✓ MCP file exists!")
        print(f"  Content: {content[:200]}...")

        assert "MCP-E2E-001" in content, "Car number not in file"
        assert str(reservation_id) in content, "Reservation ID not in file"
        print(f"✓ MCP file contains correct reservation")
    else:
        print(f"❌ MCP file NOT found at: {mcp_file}")
        print(f"  Files in directory: {list(Path(mcp_server['data_dir']).iterdir())}")
        assert False, "MCP file not created"

    sql_store.close()

    print("\n" + "="*70)
    print("TEST PASSED: MCP E2E works correctly!")
    print("="*70)
