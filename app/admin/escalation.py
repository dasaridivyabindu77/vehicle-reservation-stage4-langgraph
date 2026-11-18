"""Escalation service — the bridge between the first and second agents.

The reservation (first) agent finishes slot-filling and submits a completed
booking here; this service hands it to the administrator (second) agent to
notify the human, and records the request as ``pending_admin``. When the
administrator responds — via the dashboard, the admin CLI or an email reply —
the decision comes back through :meth:`decide`, which asks the admin agent to
interpret it and writes the result to the shared SQLite store. The user agent
then reads that status back to the customer.

    reservation agent ──submit()──▶ EscalationService ──escalate()──▶ admin agent ──▶ human
          ▲                               │                                            │
          └──────── decide() ◀────────────┴────────── administrator's reply ◀──────────┘
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ..sql_store import DynamicDataStore, Reservation, STATUS_PENDING, STATUS_CONFIRMED
from .agent import AdminAgent, AdminDecision

logger = logging.getLogger(__name__)

# Stage 3: MCP server integration (optional dependency)
try:
    from mcp_server.client import MCPClient
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logger.warning("MCP server client not available - Stage 3 file writing disabled")


@dataclass
class SubmitResult:
    reservation: Reservation
    message: str  # the approval request delivered to the administrator


class EscalationService:
    def __init__(
        self,
        sql: DynamicDataStore,
        agent: AdminAgent,
        mcp_client: Optional["MCPClient"] = None,
    ) -> None:
        self.sql = sql
        self.agent = agent
        # Stage 3: MCP client for writing confirmed reservations
        self.mcp_client = mcp_client
        if self.mcp_client and self.mcp_client.is_available():
            logger.info("MCP server is available for Stage 3 file writing")
        elif self.mcp_client:
            logger.warning("MCP server configured but not reachable")

    def submit(self, reservation_id: int) -> SubmitResult:
        """Hand a freshly created pending reservation to the administrator."""
        reservation = self.sql.get_reservation(reservation_id)
        if reservation is None:
            raise KeyError(f"reservation #{reservation_id} not found")
        message = self.agent.escalate(reservation.as_dict())
        logger.info("Escalated reservation #%s to administrator", reservation_id)
        return SubmitResult(reservation=reservation, message=message)

    def pending(self) -> List[Reservation]:
        return self.sql.pending_reservations()

    def recent(self, limit: int = 50) -> List[Reservation]:
        return self.sql.recent_reservations(limit)

    def get(self, reservation_id: int) -> Optional[Reservation]:
        return self.sql.get_reservation(reservation_id)

    def decide(
        self,
        reservation_id: int,
        raw_decision: str,
        reason: Optional[str] = None,
        decided_by: str = "admin",
        skip_mcp_write: bool = False,
    ) -> tuple[Reservation, AdminDecision]:
        """Apply the administrator's reply to a pending reservation.

        Returns the updated reservation and the interpreted decision. Raises
        ``KeyError`` if the reservation doesn't exist and ``ValueError`` if it
        has already been decided.

        Stage 3: If skip_mcp_write is False and the decision is 'confirmed' and
        an MCP server is available, writes the reservation details to persistent storage.

        IMPORTANT: When using LangGraph orchestration_v2, set skip_mcp_write=True to avoid
        duplicate writes. The graph's persist_to_mcp_node will handle the write instead.

        Args:
            reservation_id: ID of the reservation to decide
            raw_decision: Admin's decision text
            reason: Optional reason for the decision
            decided_by: Identifier of who made the decision
            skip_mcp_write: If True, skip MCP persistence (let graph handle it)
        """
        reservation = self.sql.get_reservation(reservation_id)
        if reservation is None:
            raise KeyError(f"reservation #{reservation_id} not found")
        if reservation.status != STATUS_PENDING:
            raise ValueError(
                f"reservation #{reservation_id} is already {reservation.status}"
            )

        decision = self.agent.interpret_decision(raw_decision, reason=reason)
        updated = self.sql.set_decision(
            reservation_id, decision.status, decision.reason, decided_by=decided_by
        )
        assert updated is not None  # just written
        logger.info(
            "Reservation #%s %s by %s", reservation_id, decision.status, decided_by
        )

        # Stage 3: Write confirmed reservation to file via MCP server
        # Skip if using LangGraph orchestration (graph will handle persistence)
        if decision.status == STATUS_CONFIRMED and self.mcp_client and not skip_mcp_write:
            self._persist_to_mcp_with_retry(updated, decided_by)

        return updated, decision

    def _persist_to_mcp_with_retry(
        self,
        reservation: Reservation,
        decided_by: str,
        max_retries: int = 3,
    ) -> None:
        """Write confirmed reservation to MCP server with retry logic.

        This method implements a simple retry mechanism for temporary failures.
        If all retries fail, the error is logged but the approval is not rolled back.
        The persistence operation is idempotent, so retries are safe.

        Args:
            reservation: The confirmed reservation to persist
            decided_by: Administrator who approved
            max_retries: Maximum number of retry attempts (default: 3)
        """
        import time

        last_error = None
        for attempt in range(max_retries):
            try:
                result = self.mcp_client.write_confirmed_reservation(
                    reservation_id=reservation.id,
                    first_name=reservation.first_name,
                    last_name=reservation.last_name,
                    car_number=reservation.car_number,
                    start_ts=reservation.start_ts,
                    end_ts=reservation.end_ts,
                    approved_by=decided_by,
                    approval_time=reservation.decided_at,
                )
                logger.info(
                    "Stage 3: Wrote confirmed reservation #%s to file: %s (attempt %d/%d)",
                    reservation.id,
                    result.get("file_path"),
                    attempt + 1,
                    max_retries,
                )
                return  # Success!

            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    # Exponential backoff: 0.5s, 1s, 2s
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        "Stage 3: Failed to write reservation #%s (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        reservation.id,
                        attempt + 1,
                        max_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "Stage 3: Failed to write reservation #%s after %d attempts: %s",
                        reservation.id,
                        max_retries,
                        e,
                        exc_info=True,
                    )

        # All retries exhausted - log critical error
        # The approval is NOT rolled back because the database state is correct
        # An administrator can manually verify and re-trigger persistence if needed
        logger.critical(
            "Stage 3: MCP persistence FAILED for reservation #%s after %d retries. "
            "Database shows status=confirmed but file may not be written. "
            "Manual intervention may be required. Last error: %s",
            reservation.id,
            max_retries,
            last_error,
        )
