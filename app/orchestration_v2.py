"""Stage 4 Production: LangGraph Orchestration with True Interrupt/Resume.

This module implements PRODUCTION-READY LangGraph orchestration with:
- Custom SqliteSaver checkpointer for persistent state across restarts
- interrupt() for true human-in-the-loop
- Resume capability after admin decisions
- Checkpoint recovery after application restart
- Thread isolation using session_id as thread_id

Graph Structure:
    START
      ↓
    user_interaction (Stage 1: RAG chatbot)
      ↓
    [reservation_complete?]
      ↓ yes
    escalate_to_admin (Stage 2: Submit escalation + INTERRUPT)
      ↓
    *** EXECUTION PAUSES HERE ***
      ↓
    [Admin approves/refuses via dashboard/CLI]
      ↓
    *** RESUME with decision ***
      ↓
    process_decision (Stage 2: Handle approval/refusal)
      ↓
    [approved?]
      ↓ yes
    persist_to_mcp (Stage 3: File writing)
      ↓
    notify_user
      ↓
    END

Key Differences from orchestration.py:
- Uses SqliteSaver for checkpoint persistence
- Implements interrupt() after escalation
- Resume happens when admin provides decision
- State survives application restarts
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .admin.escalation import EscalationService
from .chatbot import Chatbot
from .checkpoint_sqlite import SqliteSaver
from .config import Settings, get_settings
from .sql_store import DynamicDataStore

logger = logging.getLogger(__name__)


def _as_text(value: Any) -> str:
    """Coerce a chat response / serialised response into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    reply = getattr(value, "reply", None)
    if isinstance(reply, str):
        return reply
    if isinstance(value, dict):
        reply = value.get("reply")
        if isinstance(reply, str):
            return reply
    return str(value)


class OrchestrationState(TypedDict, total=False):
    """State for the complete system orchestration with interrupt/resume."""

    # User interaction (Stage 1)
    user_message: str
    session_id: str
    user_intent: str
    chatbot_response: str
    refused: bool
    blocked: bool
    sources: List[Dict[str, Any]]

    # Reservation data
    reservation_id: Optional[int]
    reservation_complete: bool
    reservation_data: Optional[Dict[str, Any]]

    # Administrator approval (Stage 2)
    escalation_sent: bool
    waiting_for_admin: bool  # NEW: Indicates graph is paused at interrupt
    admin_decision: Optional[str]  # "approved" | "refused" | "pending"
    admin_reason: Optional[str]
    decision_timestamp: Optional[str]
    decided_by: Optional[str]  # "admin-dashboard" | "admin-cli"

    # MCP persistence (Stage 3)
    mcp_write_attempted: bool
    mcp_write_success: bool
    mcp_file_path: Optional[str]

    # Monitoring & metrics
    stage_1_duration_ms: float
    stage_2_duration_ms: float
    stage_3_duration_ms: float
    total_duration_ms: float

    # Error tracking
    errors: List[str]

    # Final response to user
    final_response: Optional[str]


def build_production_graph(
    settings: Optional[Settings] = None,
    checkpointer_db: str = "checkpoints.db",
    chatbot: Optional["Chatbot"] = None,
    escalation_service: Optional[EscalationService] = None,
    mcp_client: Optional[Any] = None,
    checkpoint_db: Optional[str] = None,
) -> StateGraph:
    """Build the production orchestration graph with interrupt/resume.

    Args:
        settings: Application settings (defaults to the chatbot's settings)
        checkpointer_db: Path to SQLite database for checkpoints
        chatbot: Optional pre-initialized chatbot instance (prevents duplicates)
        escalation_service: Optional pre-built escalation service (tests and
            deployments that inject their own MCP client)
        mcp_client: Optional MCP client used by the persistence node

    Returns:
        Compiled graph with SqliteSaver checkpointer
    """

    # Initialize services
    if checkpoint_db is not None:
        checkpointer_db = checkpoint_db
    if settings is None:
        settings = chatbot.settings if chatbot is not None else get_settings()
    sql_store = DynamicDataStore(settings.sqlite_path)
    if chatbot is None:
        chatbot = Chatbot(settings)

    # Initialize MCP client if configured (unless one was injected)
    if mcp_client is None and settings.mcp_configured:
        try:
            from mcp_server.client import MCPClient
            mcp_client = MCPClient(
                base_url=settings.mcp_server_url,
                token=settings.mcp_server_token,
            )
            if mcp_client.is_available():
                logger.info("Production graph: MCP client initialized")
        except Exception as e:
            logger.warning("Production graph: MCP client unavailable: %s", e)

    # Initialize escalation service (unless one was injected)
    if escalation_service is None:
        # Offline mode is when DIAL is configured but no API key provided
        is_offline = not settings.llm_configured
        if settings.llm_configured or is_offline:
            try:
                from .admin.agent import AdminAgent
                from .llm import build_chat_model

                chat_model = build_chat_model(settings)
                admin_agent = AdminAgent(settings, chat_model)
                escalation_service = EscalationService(sql_store, admin_agent, mcp_client)
                logger.info("Production graph: Escalation service initialized")
            except Exception as e:
                logger.warning("Production graph: Escalation service unavailable: %s", e)

    # Define graph nodes

    def user_interaction_node(state: OrchestrationState) -> OrchestrationState:
        """Stage 1: Process user message through RAG chatbot."""
        start_time = time.perf_counter()

        try:
            user_message = state.get("user_message", "")
            session_id = state.get("session_id", "default")

            logger.info("Stage 1: Processing message for session %s", session_id)

            # Remember the booking state before this turn so we only escalate
            # when *this* message completed a new reservation.
            session_before = chatbot.reservation_session(session_id)
            prior_reservation_id = (
                session_before.reservation_id if session_before else None
            )

            # Call chatbot
            response = chatbot.chat(user_message, session_id=session_id)

            duration_ms = (time.perf_counter() - start_time) * 1000

            result = {
                "chatbot_response": response.reply,
                "user_intent": response.intent,
                "refused": response.refused,
                "blocked": response.blocked,
                "sources": response.sources,
                "stage_1_duration_ms": duration_ms,
                # A fresh user turn clears last turn's decision/final message;
                # notify_user_node derives the reply from this turn's response.
                "final_response": None,
                "admin_decision": None,
                "reservation_complete": False,
            }

            # Check if a NEW reservation was just completed for this session.
            # Merely *having* a pending reservation (e.g. a status question, a
            # follow-up after approval, or a restart) must not re-escalate it.
            session_after = chatbot.reservation_session(session_id)
            latest_reservation = sql_store.get_latest_reservation_for_session(session_id)
            newly_completed = bool(
                session_after
                and session_after.completed
                and session_after.reservation_id
                and session_after.reservation_id != prior_reservation_id
                and latest_reservation
                and latest_reservation.id == session_after.reservation_id
                and latest_reservation.status == "pending_admin"
                # A thread that has already escalated this reservation must not
                # send a second request (e.g. repeated "confirm my booking").
                and state.get("reservation_id") != latest_reservation.id
            )

            if newly_completed:
                logger.info(
                    "Stage 1: NEW reservation #%s completed, ready for escalation",
                    latest_reservation.id
                )
                result["reservation_id"] = latest_reservation.id
                result["reservation_complete"] = True
                result["reservation_data"] = {
                    "first_name": latest_reservation.first_name,
                    "last_name": latest_reservation.last_name,
                    "car_number": latest_reservation.car_number,
                    "start_time": latest_reservation.start_ts,
                    "end_time": latest_reservation.end_ts,
                }
            elif latest_reservation:
                logger.info(
                    "Stage 1: Reservation #%s already known (status: %s), not re-escalating",
                    latest_reservation.id,
                    latest_reservation.status,
                )

            return result

        except Exception as e:
            logger.error("Stage 1 error: %s", e, exc_info=True)
            errors = state.get("errors", [])
            errors.append(f"Stage 1 error: {str(e)}")
            return {
                "chatbot_response": "I encountered an error processing your request.",
                "errors": errors,
                "stage_1_duration_ms": (time.perf_counter() - start_time) * 1000,
                "reservation_complete": False,
                "final_response": None,
            }

    def check_reservation_complete(state: OrchestrationState) -> Literal["escalate", "end"]:
        """Route to escalation if reservation just completed."""
        if state.get("reservation_complete") and state.get("reservation_id"):
            return "escalate"
        return "end"

    def escalate_to_admin_node(state: OrchestrationState) -> Command[Literal["process_decision"]]:
        """Stage 2: Escalate to admin and INTERRUPT for human approval.

        This node:
        1. Submits escalation to admin
        2. Marks session as escalated (prevent duplicates)
        3. Calls interrupt() to pause execution
        4. Waits for admin decision via resume

        Returns:
            Command with goto="process_decision" to resume after approval
        """
        start_time = time.perf_counter()

        if not escalation_service:
            logger.warning("Stage 2: No escalation service configured")
            return Command(
                goto="process_decision",
                update={
                    "escalation_sent": False,
                    "admin_decision": "error",
                    "errors": state.get("errors", []) + ["No escalation service configured"],
                    "stage_2_duration_ms": (time.perf_counter() - start_time) * 1000,
                }
            )

        try:
            reservation_id = state["reservation_id"]
            logger.info("Stage 2: Escalating reservation #%s to administrator", reservation_id)

            # Check if reservation already has a decision (resume after restart)
            sql_store = DynamicDataStore(settings.sqlite_path)
            reservation = sql_store.get_reservation(reservation_id)
            sql_store.close()

            logger.info(
                "Stage 2: Checking reservation #%s status: %s",
                reservation_id,
                reservation.status if reservation else "NOT FOUND"
            )

            if reservation and reservation.status in ("confirmed", "refused"):
                # Reservation already decided - this is a resume after restart
                logger.info(
                    "Stage 2: Reservation #%s already decided (status: %s) - skipping interrupt",
                    reservation_id, reservation.status
                )

                # Extract decision from database
                decision = "approved" if reservation.status == "confirmed" else "refused"
                reason = reservation.admin_reason or "No reason provided"
                decided_by = reservation.decided_by or "admin"

                duration_ms = (time.perf_counter() - start_time) * 1000

                return Command(
                    goto="process_decision",
                    update={
                        "escalation_sent": True,
                        "waiting_for_admin": False,
                        "admin_decision": decision,
                        "admin_reason": reason,
                        "decided_by": decided_by,
                        "decision_timestamp": datetime.now(timezone.utc).isoformat(),
                        "stage_2_duration_ms": duration_ms,
                    }
                )

            # Submit escalation
            result = escalation_service.submit(reservation_id)

            # Note: Session escalation tracking is handled via reservation status
            # Reservation status is "pending_admin" which prevents re-escalation

            duration_ms = (time.perf_counter() - start_time) * 1000

            logger.info("Stage 2: INTERRUPT - Waiting for admin decision on reservation #%s", reservation_id)

            # INTERRUPT: Pause execution until admin provides decision
            # This saves a checkpoint and stops graph execution
            # Resume happens when process_admin_decision_and_resume() is called
            admin_decision_input = interrupt(
                {
                    "type": "human_approval_required",
                    "reservation_id": reservation_id,
                    "message": f"Administrator approval required for reservation #{reservation_id}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            # Execution resumes here after admin decision
            logger.info(
                "Stage 2: RESUMED with decision: %s",
                admin_decision_input.get("decision") if admin_decision_input else "None"
            )

            # Extract decision from resume input
            decision = "pending"
            reason = None
            decided_by = "unknown"

            if admin_decision_input and isinstance(admin_decision_input, dict):
                decision = admin_decision_input.get("decision", "pending")
                reason = admin_decision_input.get("reason")
                decided_by = admin_decision_input.get("decided_by", "admin")

            return Command(
                goto="process_decision",
                update={
                    "escalation_sent": True,
                    "waiting_for_admin": False,
                    "admin_decision": decision,
                    "admin_reason": reason,
                    "decided_by": decided_by,
                    "decision_timestamp": datetime.now(timezone.utc).isoformat(),
                    "stage_2_duration_ms": duration_ms,
                }
            )

        except GraphInterrupt:
            # ``interrupt()`` pauses the graph by raising GraphInterrupt. It
            # must never be swallowed by the generic error handler, otherwise
            # the human-in-the-loop pause silently turns into an error state.
            raise
        except Exception as e:
            logger.error("Stage 2 error: %s", e, exc_info=True)
            errors = state.get("errors", [])
            errors.append(f"Stage 2 error: {str(e)}")
            return Command(
                goto="process_decision",
                update={
                    "escalation_sent": False,
                    "admin_decision": "error",
                    "errors": errors,
                    "stage_2_duration_ms": (time.perf_counter() - start_time) * 1000,
                }
            )

    def process_decision_node(state: OrchestrationState) -> OrchestrationState:
        """Process the administrator's decision."""
        decision = state.get("admin_decision", "pending")

        logger.info("Stage 2: Processing admin decision: %s", decision)

        if decision == "approved":
            return {"admin_decision": "approved"}
        elif decision == "refused":
            return {
                "admin_decision": "refused",
                "final_response": f"Your reservation was reviewed and unfortunately could not be approved. {state.get('admin_reason', '')}".strip(),
            }
        else:
            # Error or still pending
            return {
                "admin_decision": decision,
                "final_response": "There was an issue processing your reservation. Please try again or contact support.",
            }

    def check_approval_status(state: OrchestrationState) -> Literal["persist", "notify"]:
        """Route based on approval status."""
        if state.get("admin_decision") == "approved":
            return "persist"
        return "notify"

    def persist_to_mcp_node(state: OrchestrationState) -> OrchestrationState:
        """Stage 3: Persist approved reservation to MCP server."""
        start_time = time.perf_counter()

        if not mcp_client:
            logger.warning("Stage 3: MCP client not configured")
            return {
                "mcp_write_attempted": False,
                "mcp_write_success": False,
                "stage_3_duration_ms": (time.perf_counter() - start_time) * 1000,
            }

        try:
            reservation_data = state.get("reservation_data", {})
            reservation_id = state.get("reservation_id")

            logger.info("Stage 3: Writing reservation #%s to MCP", reservation_id)

            # Write to MCP with retry logic (handled by escalation service)
            # For direct graph execution, we do the write here
            result = mcp_client.write_confirmed_reservation(
                reservation_id=reservation_id,
                first_name=reservation_data.get("first_name", ""),
                last_name=reservation_data.get("last_name", ""),
                car_number=reservation_data.get("car_number", ""),
                start_ts=reservation_data.get("start_time", ""),
                end_ts=reservation_data.get("end_time", ""),
                approved_by=state.get("decided_by", "admin"),
                approval_time=datetime.now(timezone.utc).isoformat(),
            )

            duration_ms = (time.perf_counter() - start_time) * 1000

            return {
                "mcp_write_attempted": True,
                "mcp_write_success": result.get("success", False),
                "mcp_file_path": "confirmed_reservations.txt",
                "stage_3_duration_ms": duration_ms,
                "final_response": f"Your reservation #{reservation_id} has been approved and confirmed!",
            }

        except Exception as e:
            logger.error("Stage 3 error: %s", e, exc_info=True)
            errors = state.get("errors", [])
            errors.append(f"Stage 3 error: {str(e)}")
            return {
                "mcp_write_attempted": True,
                "mcp_write_success": False,
                "errors": errors,
                "stage_3_duration_ms": (time.perf_counter() - start_time) * 1000,
                "final_response": f"Your reservation #{state.get('reservation_id')} was approved but we had trouble saving it. Our team has been notified.",
            }

    def notify_user_node(state: OrchestrationState) -> OrchestrationState:
        """Final node: Prepare response to user."""

        # Calculate total duration
        total_duration = (
            state.get("stage_1_duration_ms", 0) +
            state.get("stage_2_duration_ms", 0) +
            state.get("stage_3_duration_ms", 0)
        )

        # Use final_response if set, otherwise use chatbot_response. The latter
        # is a ChatResponse object in-process (and may be a serialised mapping
        # after a checkpoint), so normalise it to text for API consumers.
        final_response = state.get("final_response") or _as_text(
            state.get("chatbot_response")
        )

        return {
            "final_response": final_response,
            "total_duration_ms": total_duration,
        }

    # Build the graph
    graph = StateGraph(OrchestrationState)

    # Add nodes
    graph.add_node("user_interaction", user_interaction_node)
    graph.add_node("escalate_to_admin", escalate_to_admin_node)
    graph.add_node("process_decision", process_decision_node)
    graph.add_node("persist_to_mcp", persist_to_mcp_node)
    graph.add_node("notify_user", notify_user_node)

    # Add edges
    graph.add_edge(START, "user_interaction")

    # Conditional routing after user interaction
    graph.add_conditional_edges(
        "user_interaction",
        check_reservation_complete,
        {
            "escalate": "escalate_to_admin",
            "end": "notify_user",
        }
    )

    # escalate_to_admin returns Command with goto="process_decision"
    # So execution automatically continues to process_decision after resume

    # Conditional routing after decision processing
    graph.add_conditional_edges(
        "process_decision",
        check_approval_status,
        {
            "persist": "persist_to_mcp",
            "notify": "notify_user",
        }
    )

    # After persistence, notify user
    graph.add_edge("persist_to_mcp", "notify_user")

    # End workflow
    graph.add_edge("notify_user", END)

    # Create SqliteSaver checkpointer (persistent)
    checkpointer_path = Path(checkpointer_db)
    checkpointer = SqliteSaver(db_path=str(checkpointer_path))

    logger.info("Production graph compiled with SqliteSaver checkpointer (persistent): %s", checkpointer_path)

    # Compile graph with checkpointer
    return graph.compile(checkpointer=checkpointer)


def process_admin_decision_and_resume(
    graph: StateGraph,
    thread_id: str,
    decision: str,
    reason: Optional[str] = None,
    decided_by: str = "admin",
) -> Dict[str, Any]:
    """Resume graph execution after admin decision.

    This function is called by the admin dashboard or CLI when the
    administrator approves or refuses a reservation.

    Args:
        graph: The compiled graph with checkpointer
        thread_id: The session/thread ID (same as session_id)
        decision: "approved" or "refused"
        reason: Optional reason for the decision
        decided_by: Who made the decision ("admin-dashboard" | "admin-cli")

    Returns:
        Final state after resume
    """
    logger.info(
        "Resuming graph execution for thread %s with decision: %s",
        thread_id,
        decision
    )

    # Resume execution by invoking with the decision
    # The interrupt() call in escalate_to_admin_node will receive this value
    config = {"configurable": {"thread_id": thread_id}}

    # Pass the decision as input to resume from interrupt
    # This will be returned by the interrupt() call
    # When resuming from an interrupt, pass the value directly (not wrapped in Command)
    resume_input = {
        "decision": decision,
        "reason": reason,
        "decided_by": decided_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # Resume execution. LangGraph delivers ``Command(resume=...)`` as the
        # return value of the pending ``interrupt()`` call. Passing a plain
        # mapping here is treated as new graph input, so the interrupt would
        # never be released.
        final_state = graph.invoke(Command(resume=resume_input), config)
        logger.info("Graph execution resumed and completed for thread %s", thread_id)
        return final_state
    except Exception as e:
        logger.error("Error resuming graph for thread %s: %s", thread_id, e, exc_info=True)
        raise
