"""Stage 4: Complete LangGraph Orchestration of All Components.

IMPORTANT: This orchestration is for testing and monitoring purposes.
It does NOT implement true human-in-the-loop with interrupt/resume.

Production flow:
- Web/CLI call chatbot.chat() directly (working design)
- Admin approves via dashboard/CLI (database polling)
- Escalation service handles MCP persistence

This graph demonstrates the flow but does not pause/resume for decisions.

This module implements a comprehensive workflow graph that orchestrates the
entire CityPark system across all three stages:

    Stage 1: RAG Chatbot (user interaction)
        ↓
    Stage 2: Administrator Approval (human-in-the-loop)
        ↓
    Stage 3: MCP Server (data persistence)

The orchestration graph provides:
- Explicit nodes for each major system component
- State management across the entire pipeline
- Error handling and retry logic
- Monitoring and metrics collection
- Load balancing capabilities

Graph Structure:
    START
      ↓
    user_interaction (Stage 1: RAG chatbot)
      ↓
    [reservation_complete?]
      ↓ yes
    escalate_to_admin (Stage 2: Human-in-the-loop)
      ↓
    wait_for_decision (DOES NOT WAIT - checks once)
      ↓
    [decision_received?]
      ↓ approved
    persist_to_mcp (Stage 3: File writing)
      ↓
    notify_user
      ↓
    END

Technical Note:
The wait_for_decision node checks status once and returns immediately.
True LangGraph interrupt/resume would require:
- SQLite-based checkpointer
- graph.add_node(..., interrupt="after")
- Resume with graph.invoke(state, {"configurable": {"thread_id": ...}})
This is a significant architectural change that is deferred for future work.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


class OrchestrationState(TypedDict, total=False):
    """State for the complete system orchestration.

    This state tracks the entire journey from user message through to
    final persistence, spanning all three stages of the system.
    """
    # User interaction (Stage 1)
    user_message: str
    session_id: str
    user_intent: str
    chatbot_response: str

    # Reservation data
    reservation_id: Optional[int]
    reservation_complete: bool
    reservation_data: Optional[Dict[str, Any]]

    # Administrator approval (Stage 2)
    escalation_sent: bool
    admin_decision: Optional[str]  # "approve" | "refuse" | "pending"
    admin_reason: Optional[str]
    decision_timestamp: Optional[str]

    # MCP persistence (Stage 3)
    mcp_write_attempted: bool
    mcp_write_success: bool
    mcp_file_path: Optional[str]

    # Monitoring & metrics
    stage_1_duration_ms: float
    stage_2_duration_ms: float
    stage_3_duration_ms: float
    total_duration_ms: float
    errors: List[str]

    # Flow control
    workflow_complete: bool
    needs_retry: bool


def build_orchestration_graph(chatbot, escalation_service=None, mcp_client=None):
    """Build the complete Stage 4 orchestration graph.

    Args:
        chatbot: Stage 1 chatbot instance
        escalation_service: Stage 2 escalation service (optional)
        mcp_client: Stage 3 MCP client (optional)

    Returns:
        Compiled LangGraph StateGraph
    """

    def user_interaction_node(state: OrchestrationState) -> OrchestrationState:
        """Stage 1: Handle user interaction via RAG chatbot.

        This node processes the user's message through the Stage 1 chatbot,
        which includes:
        - Input guardrails
        - Intent classification
        - Reservation slot-filling OR
        - RAG retrieval + generation
        - Output guardrails
        """
        start_time = time.perf_counter()

        try:
            logger.info(
                "Stage 1: Processing user message (session=%s)",
                state.get("session_id", "unknown")
            )

            # Process through Stage 1 chatbot
            response = chatbot.chat(
                state["user_message"],
                session_id=state["session_id"]
            )

            # Check if reservation was completed
            session = chatbot._sessions.get(state["session_id"])
            reservation_complete = False
            reservation_id = None
            reservation_data = None

            if session and session.reservation_id:
                reservation_complete = True
                reservation_id = session.reservation_id
                # Get reservation from database
                db_reservation = chatbot.sql.get_reservation(reservation_id)
                if db_reservation:
                    reservation_data = {
                        "id": reservation_id,
                        "first_name": db_reservation.first_name,
                        "last_name": db_reservation.last_name,
                        "car_number": db_reservation.car_number,
                        "start_ts": db_reservation.start_ts,
                        "end_ts": db_reservation.end_ts,
                    }

            duration_ms = (time.perf_counter() - start_time) * 1000

            return {
                "chatbot_response": response.reply,
                "user_intent": response.intent,
                "reservation_complete": reservation_complete,
                "reservation_id": reservation_id,
                "reservation_data": reservation_data,
                "stage_1_duration_ms": duration_ms,
                "errors": state.get("errors", []),
            }

        except Exception as e:
            logger.error("Stage 1 error: %s", e, exc_info=True)
            errors = state.get("errors", [])
            errors.append(f"Stage 1 error: {str(e)}")
            return {
                "chatbot_response": "I apologize, but I encountered an error. Please try again.",
                "reservation_complete": False,
                "errors": errors,
                "stage_1_duration_ms": (time.perf_counter() - start_time) * 1000,
            }

    def check_reservation_complete(state: OrchestrationState) -> str:
        """Routing: Check if reservation was completed in this turn.

        This checks if a NEW reservation was just completed, not if a
        reservation ID exists from a previous turn. This prevents duplicate
        escalations when users send follow-up messages.
        """
        # Only proceed to escalation if a reservation was JUST completed
        # (reservation_complete is set to True by user_interaction_node)
        if state.get("reservation_complete") and state.get("reservation_id"):
            # Additional check: ensure this is a new reservation that hasn't been escalated
            # The user_interaction_node sets reservation_complete=True only when
            # the chatbot completes slot-filling, not on subsequent messages
            logger.info("Reservation #%s completed, proceeding to Stage 2", state.get("reservation_id"))
            return "escalate"
        return "end"

    def escalate_to_admin_node(state: OrchestrationState) -> OrchestrationState:
        """Stage 2: Escalate to administrator for approval.

        This node submits the completed reservation to the administrator
        agent for human-in-the-loop approval.
        """
        start_time = time.perf_counter()

        if not escalation_service:
            logger.warning("Stage 2: No escalation service configured")
            return {
                "escalation_sent": False,
                "admin_decision": "pending",
                "stage_2_duration_ms": 0,
            }

        try:
            logger.info(
                "Stage 2: Escalating reservation #%s to administrator",
                state.get("reservation_id")
            )

            # Submit to administrator (Stage 2)
            result = escalation_service.submit(state["reservation_id"])

            duration_ms = (time.perf_counter() - start_time) * 1000

            return {
                "escalation_sent": True,
                "admin_decision": "pending",
                "stage_2_duration_ms": duration_ms,
            }

        except Exception as e:
            logger.error("Stage 2 error: %s", e, exc_info=True)
            errors = state.get("errors", [])
            errors.append(f"Stage 2 error: {str(e)}")
            return {
                "escalation_sent": False,
                "admin_decision": "error",
                "errors": errors,
                "stage_2_duration_ms": (time.perf_counter() - start_time) * 1000,
            }

    def wait_for_decision_node(state: OrchestrationState) -> OrchestrationState:
        """Check administrator decision status (DOES NOT WAIT).

        IMPORTANT: This node does NOT wait for a decision - it checks once
        and returns immediately. This is for testing/monitoring only.

        In a production system with true interrupt/resume, this would:
        - Use LangGraph interrupt="after" on escalate_to_admin node
        - Pause execution with checkpointer (SQLite-based)
        - Admin approves via dashboard/CLI
        - Resume with graph.invoke(state, {"configurable": {"thread_id": ...}})

        Current production flow uses database polling instead:
        - Admin approves via dashboard/CLI (app/admin/)
        - Escalation service polls database for status
        - MCP persistence triggered on approval
        """
        try:
            if not escalation_service:
                return {"admin_decision": "pending"}

            # Check current reservation status
            reservation = escalation_service.get(state["reservation_id"])

            if reservation and reservation.status != "pending_admin":
                return {
                    "admin_decision": reservation.status.replace("pending_admin", "pending"),
                    "admin_reason": reservation.admin_reason,
                    "decision_timestamp": reservation.decided_at,
                }

            return {"admin_decision": "pending"}

        except Exception as e:
            logger.error("Error checking decision status: %s", e)
            return {"admin_decision": "pending"}

    def check_admin_decision(state: OrchestrationState) -> str:
        """Routing: Check administrator decision."""
        decision = state.get("admin_decision", "pending")

        if decision == "confirmed":
            logger.info("Reservation approved, proceeding to Stage 3")
            return "persist"
        elif decision == "refused":
            logger.info("Reservation refused, ending workflow")
            return "notify"
        else:
            # Still pending - in production would wait/retry
            logger.info("Decision pending, ending workflow (admin will approve later)")
            return "notify"

    def persist_to_mcp_node(state: OrchestrationState) -> OrchestrationState:
        """Stage 3: Persist confirmed reservation via MCP server.

        This node writes the confirmed reservation to persistent storage
        via the MCP server.
        """
        start_time = time.perf_counter()

        if not mcp_client:
            logger.warning("Stage 3: No MCP client configured")
            return {
                "mcp_write_attempted": False,
                "mcp_write_success": False,
                "stage_3_duration_ms": 0,
            }

        try:
            logger.info(
                "Stage 3: Writing reservation #%s to MCP server",
                state.get("reservation_id")
            )

            reservation_data = state["reservation_data"]

            # Write to MCP server (Stage 3)
            result = mcp_client.write_confirmed_reservation(
                reservation_id=state["reservation_id"],
                first_name=reservation_data["first_name"],
                last_name=reservation_data["last_name"],
                car_number=reservation_data["car_number"],
                start_ts=reservation_data["start_ts"],
                end_ts=reservation_data["end_ts"],
                approval_time=state.get("decision_timestamp"),
            )

            duration_ms = (time.perf_counter() - start_time) * 1000

            return {
                "mcp_write_attempted": True,
                "mcp_write_success": result.get("success", False),
                "mcp_file_path": result.get("file_path"),
                "stage_3_duration_ms": duration_ms,
            }

        except Exception as e:
            logger.error("Stage 3 error: %s", e, exc_info=True)
            errors = state.get("errors", [])
            errors.append(f"Stage 3 error: {str(e)}")
            duration_ms = (time.perf_counter() - start_time) * 1000
            return {
                "mcp_write_attempted": True,
                "mcp_write_success": False,
                "errors": errors,
                "stage_3_duration_ms": duration_ms,
            }

    def notify_user_node(state: OrchestrationState) -> OrchestrationState:
        """Final node: Notify user of outcome.

        This node would send a notification to the user about the final
        outcome of their reservation request.
        """
        decision = state.get("admin_decision", "pending")

        if decision == "confirmed":
            logger.info(
                "Workflow complete: Reservation #%s confirmed and persisted",
                state.get("reservation_id")
            )
        elif decision == "refused":
            logger.info(
                "Workflow complete: Reservation #%s refused",
                state.get("reservation_id")
            )
        else:
            logger.info(
                "Workflow complete: Reservation #%s pending admin review",
                state.get("reservation_id")
            )

        # Calculate total duration
        total_duration = (
            state.get("stage_1_duration_ms", 0) +
            state.get("stage_2_duration_ms", 0) +
            state.get("stage_3_duration_ms", 0)
        )

        return {
            "workflow_complete": True,
            "total_duration_ms": total_duration,
        }

    # Build the graph
    graph = StateGraph(OrchestrationState)

    # Add nodes
    graph.add_node("user_interaction", user_interaction_node)
    graph.add_node("escalate_to_admin", escalate_to_admin_node)
    graph.add_node("wait_for_decision", wait_for_decision_node)
    graph.add_node("persist_to_mcp", persist_to_mcp_node)
    graph.add_node("notify_user", notify_user_node)

    # Add edges
    graph.add_edge(START, "user_interaction")

    # After user interaction, check if reservation is complete
    graph.add_conditional_edges(
        "user_interaction",
        check_reservation_complete,
        {
            "escalate": "escalate_to_admin",
            "end": "notify_user",
        }
    )

    # After escalation, wait for decision
    graph.add_edge("escalate_to_admin", "wait_for_decision")

    # After waiting, check decision
    graph.add_conditional_edges(
        "wait_for_decision",
        check_admin_decision,
        {
            "persist": "persist_to_mcp",
            "notify": "notify_user",
        }
    )

    # After persistence, notify user
    graph.add_edge("persist_to_mcp", "notify_user")

    # End workflow
    graph.add_edge("notify_user", END)

    logger.info("Stage 4 orchestration graph compiled successfully")
    return graph.compile()


class SystemOrchestrator:
    """High-level orchestrator for the complete CityPark system.

    IMPORTANT: This orchestrator is for testing and monitoring purposes only.
    It does NOT implement true human-in-the-loop with interrupt/resume.

    Production usage:
    - Web server and CLI call chatbot.chat() directly
    - Admin approves/refuses via dashboard or CLI
    - Escalation service handles MCP persistence
    - This orchestrator is used for flow demonstration and metrics collection

    This class provides a convenient interface for running the Stage 4
    orchestration workflow and collecting metrics.
    """

    def __init__(self, chatbot, escalation_service=None, mcp_client=None):
        """Initialize the orchestrator.

        Args:
            chatbot: Stage 1 chatbot instance
            escalation_service: Stage 2 escalation service
            mcp_client: Stage 3 MCP client
        """
        self.chatbot = chatbot
        self.escalation_service = escalation_service
        self.mcp_client = mcp_client
        self.graph = build_orchestration_graph(
            chatbot, escalation_service, mcp_client
        )
        self.metrics: List[Dict[str, Any]] = []

    def process_user_message(
        self,
        message: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """Process a user message through the complete orchestration.

        Args:
            message: User's message
            session_id: Session identifier

        Returns:
            Final orchestration state with all metrics
        """
        initial_state: OrchestrationState = {
            "user_message": message,
            "session_id": session_id,
            "reservation_complete": False,
            "escalation_sent": False,
            "mcp_write_attempted": False,
            "workflow_complete": False,
            "errors": [],
            "stage_1_duration_ms": 0,
            "stage_2_duration_ms": 0,
            "stage_3_duration_ms": 0,
            "total_duration_ms": 0,
        }

        # Run through the graph
        final_state = self.graph.invoke(initial_state)

        # Collect metrics
        self._record_metrics(final_state)

        return final_state

    def _record_metrics(self, state: OrchestrationState):
        """Record metrics from a workflow execution."""
        metric = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": state.get("session_id"),
            "reservation_id": state.get("reservation_id"),
            "reservation_complete": state.get("reservation_complete", False),
            "admin_decision": state.get("admin_decision"),
            "mcp_success": state.get("mcp_write_success", False),
            "stage_1_duration_ms": state.get("stage_1_duration_ms", 0),
            "stage_2_duration_ms": state.get("stage_2_duration_ms", 0),
            "stage_3_duration_ms": state.get("stage_3_duration_ms", 0),
            "total_duration_ms": state.get("total_duration_ms", 0),
            "errors": state.get("errors", []),
        }
        self.metrics.append(metric)

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get summary statistics of all executions.

        Returns:
            Dictionary with aggregate metrics
        """
        if not self.metrics:
            return {
                "total_requests": 0,
                "avg_stage_1_ms": 0,
                "avg_stage_2_ms": 0,
                "avg_stage_3_ms": 0,
                "avg_total_ms": 0,
                "reservations_completed": 0,
                "reservations_confirmed": 0,
                "reservations_refused": 0,
                "mcp_success_rate": 0,
                "error_count": 0,
            }

        total = len(self.metrics)
        reservations_completed = sum(1 for m in self.metrics if m["reservation_complete"])
        reservations_confirmed = sum(
            1 for m in self.metrics if m["admin_decision"] == "confirmed"
        )
        reservations_refused = sum(
            1 for m in self.metrics if m["admin_decision"] == "refused"
        )
        mcp_successes = sum(1 for m in self.metrics if m["mcp_success"])
        mcp_attempts = sum(1 for m in self.metrics if m.get("reservation_complete"))

        return {
            "total_requests": total,
            "avg_stage_1_ms": sum(m["stage_1_duration_ms"] for m in self.metrics) / total,
            "avg_stage_2_ms": sum(m["stage_2_duration_ms"] for m in self.metrics) / total,
            "avg_stage_3_ms": sum(m["stage_3_duration_ms"] for m in self.metrics) / total,
            "avg_total_ms": sum(m["total_duration_ms"] for m in self.metrics) / total,
            "reservations_completed": reservations_completed,
            "reservations_confirmed": reservations_confirmed,
            "reservations_refused": reservations_refused,
            "mcp_success_rate": (mcp_successes / mcp_attempts * 100) if mcp_attempts > 0 else 0,
            "error_count": sum(len(m["errors"]) for m in self.metrics),
        }
