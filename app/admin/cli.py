"""Administrator CLI — a terminal interface for approving/refusing reservations.

Run with::

    python -m app.admin.cli

Lists pending reservations and prompts the administrator to approve/refuse each
one interactively. Decisions flow through the same :class:`EscalationService` as
the dashboard, so they're recorded consistently and reflected back to users who
ask for their booking status.
"""

from __future__ import annotations

import sys

from ..admin.agent import AdminAgent
from ..admin.escalation import EscalationService
from ..config import get_settings
from ..llm import build_chat_model
from ..sql_store import DynamicDataStore


def main() -> int:
    settings = get_settings()
    sql = DynamicDataStore(settings.sqlite_path)
    chat_model = build_chat_model(settings)
    agent = AdminAgent(settings, chat_model)

    # Stage 3: Initialize MCP client if configured
    mcp_client = None
    if settings.mcp_configured:
        try:
            from mcp_server.client import MCPClient
            mcp_client = MCPClient(
                base_url=settings.mcp_server_url,
                token=settings.mcp_server_token,
            )
            if mcp_client.is_available():
                print("Stage 3: MCP server connected for file persistence")
            else:
                print("Stage 3: MCP server configured but not reachable")
        except Exception as e:
            print(f"Stage 3: Failed to initialize MCP client: {e}")

    escalation = EscalationService(sql, agent, mcp_client)

    print("CityPark Administrator CLI")
    mode = "DIAL" if settings.llm_configured else "offline"
    print(f"Backend: llm={mode}, db={settings.sqlite_path}\n")

    try:
        while True:
            pending = escalation.pending()
            if not pending:
                print("No pending reservations. All caught up!")
                break

            print(f"{len(pending)} reservation(s) awaiting approval:\n")
            for i, r in enumerate(pending, start=1):
                print(f"[{i}] #{r.id} — {r.full_name}")
                print(f"    Plate: {r.car_number}")
                print(f"    From:  {r.start_ts}")
                print(f"    To:    {r.end_ts}")
                print(f"    Created: {r.created_at}")
                print()

            try:
                choice = input("Enter number to decide (or 'q' to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if choice.lower() in {"q", "quit", "exit"}:
                break

            try:
                idx = int(choice) - 1
                if idx < 0 or idx >= len(pending):
                    print("Invalid selection.\n")
                    continue
            except ValueError:
                print("Invalid selection.\n")
                continue

            reservation = pending[idx]
            print(f"\nReservation #{reservation.id} — {reservation.full_name}")
            print(f"  Plate: {reservation.car_number}")
            print(f"  {reservation.start_ts} → {reservation.end_ts}")

            decision = input("Decision (approve / refuse): ").strip().lower()
            if decision not in {"approve", "refuse", "a", "r", "yes", "no"}:
                print("Please enter 'approve' or 'refuse'.\n")
                continue

            reason = input("Reason (optional): ").strip() or None

            normalized = "approve" if decision in {"approve", "a", "yes"} else "refuse"

            try:
                updated, parsed = escalation.decide(
                    reservation.id, normalized, reason=reason, decided_by="admin-cli"
                )
                print(f"✓ Reservation #{updated.id} {updated.status}: {parsed.reason}\n")
            except Exception as exc:  # noqa: BLE001
                print(f"✗ Error: {exc}\n")

    finally:
        sql.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
