"""FastAPI web app: a chat endpoint plus a minimal browser UI.

Run with::

    uvicorn app.web.server:app --reload

``GET /``        serves the chat page.
``POST /chat``   {"message": str, "session_id": str} -> chat response.
``GET /health``  liveness + backend info.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..admin import routes as admin_routes
from ..chatbot import Chatbot
from ..config import Settings, get_settings
from ..orchestration_v2 import build_production_graph, process_admin_decision_and_resume

logger = logging.getLogger(__name__)

_bot: Chatbot | None = None
_bot_lock = threading.Lock()
_graph = None
_graph_lock = threading.Lock()
_INDEX = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def get_bot() -> Chatbot:
    """Return the singleton chatbot, building it at most once (thread-safe)."""
    global _bot
    if _bot is None:
        with _bot_lock:
            if _bot is None:  # double-checked locking
                _bot = Chatbot()
    return _bot


def get_graph():
    """Return the singleton orchestration graph, building it at most once (thread-safe)."""
    global _graph
    if _graph is None:
        with _graph_lock:
            if _graph is None:  # double-checked locking
                settings = get_settings()
                bot = get_bot()  # Get singleton chatbot to prevent duplicate reservations
                _graph = build_production_graph(settings, checkpointer_db=settings.checkpoint_db, chatbot=bot)
                logger.info("Production orchestration graph initialized (checkpoint_db=%s)", settings.checkpoint_db)
    return _graph


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate configuration before starting
    settings = get_settings()
    config_errors = settings.validate_configuration()
    if config_errors:
        logger.error("Configuration validation failed:")
        for error in config_errors:
            logger.error("  - %s", error)
        raise RuntimeError(f"Invalid configuration: {'; '.join(config_errors)}")

    if settings.llm_configured:
        provider = settings.llm_provider.upper()
        model = settings.chat_model
        logger.info("LLM provider: %s (model: %s)", provider, model)
    else:
        logger.info("LLM provider: OFFLINE mode (no API key configured)")

    # Warm the bot once at startup so the first request isn't a cold network build.
    try:
        get_bot()
        logger.info("Chatbot ready")
    except Exception as exc:  # noqa: BLE001 - don't block startup
        logger.warning("Chatbot warm-up failed (%s); will retry on first request", exc)
    yield


app = FastAPI(title="CityPark Assistant", version="0.2.0", lifespan=lifespan)


# --- Dependency providers for admin routes -----------------------------------
def _get_escalation():
    """Expose the bot's escalation service to the admin routes."""
    return get_bot().escalation


def _get_settings() -> Settings:
    return get_settings()


# The admin routes declare placeholder dependencies; register the real providers.
app.include_router(admin_routes.router)
app.dependency_overrides[admin_routes.get_escalation_dep] = _get_escalation
app.dependency_overrides[admin_routes.get_settings_dep] = _get_settings


class ChatRequest(BaseModel):
    message: str
    session_id: str = "web"


class ChatReply(BaseModel):
    reply: str
    intent: str
    refused: bool
    blocked: bool
    in_reservation: bool
    sources: list
    latency_ms: float


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX


@app.get("/health")
def health() -> dict:
    bot = get_bot()
    if not bot.settings.llm_configured:
        llm_provider = "offline"
    else:
        llm_provider = bot.settings.llm_provider  # "openai" or "dial"

    return {
        "status": "ok",
        "vector_backend": bot.backend,
        "llm": llm_provider,
    }


def _reply_text(state: dict) -> str:
    """Extract the user-facing reply from a graph state.

    ``chatbot_response`` is a ``ChatResponse`` dataclass in-process and may be
    a serialised mapping after a checkpoint round-trip. ``__interrupt__`` means
    the graph paused for administrator approval, in which case the reservation
    confirmation from ``chatbot_response`` is the message the user should see.
    """
    for candidate in (state.get("final_response"), state.get("chatbot_response")):
        if isinstance(candidate, str):
            if candidate:
                return candidate
        elif candidate is not None:
            reply = getattr(candidate, "reply", None)
            if isinstance(reply, str) and reply:
                return reply
            if isinstance(candidate, dict) and candidate.get("reply"):
                return str(candidate["reply"])
            text = str(candidate)
            if text:
                return text
    return ""


def _waiting_for_admin(graph, config) -> bool:
    """True when the thread is paused at the human-approval interrupt."""
    try:
        snapshot = graph.get_state(config)
    except Exception:  # noqa: BLE001 - treat unknown state as runnable
        return False
    return bool(getattr(snapshot, "next", ()))


@app.post("/chat", response_model=ChatReply)
def chat(req: ChatRequest) -> ChatReply:
    """Process chat message through LangGraph orchestration.

    This endpoint uses the production orchestration graph with
    interrupt/resume capabilities for human-in-the-loop approval. Each user
    message is processed exactly once: if the graph has already run a turn we
    never replay it through ``bot.chat`` (that would consume a booking answer
    twice and corrupt the reservation state).
    """
    bot = get_bot()

    try:
        graph = get_graph()
    except Exception as exc:  # noqa: BLE001 - graph unavailable, use the bot
        logger.error("Graph unavailable (%s); using direct chatbot", exc, exc_info=True)
        resp = bot.chat(req.message, session_id=req.session_id)
        return ChatReply(
            reply=resp.reply,
            intent=resp.intent,
            refused=resp.refused,
            blocked=resp.blocked,
            in_reservation=resp.in_reservation,
            sources=resp.sources,
            latency_ms=resp.latency_ms,
        )

    # Execute graph with thread_id = session_id for checkpointing
    config = {"configurable": {"thread_id": req.session_id}}

    # A thread paused at the admin-approval interrupt must not treat the next
    # user message as the administrator's decision. Keep the checkpoint intact
    # (so the dashboard can still resume it) and answer this turn directly.
    if _waiting_for_admin(graph, config):
        resp = bot.chat(req.message, session_id=req.session_id)
        return ChatReply(
            reply=resp.reply,
            intent=resp.intent,
            refused=resp.refused,
            blocked=resp.blocked,
            in_reservation=resp.in_reservation,
            sources=resp.sources,
            latency_ms=resp.latency_ms,
        )

    try:
        state = graph.invoke(
            {
                "user_message": req.message,
                "session_id": req.session_id,
            },
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - never replay a partial turn
        logger.error("Graph execution error: %s", exc, exc_info=True)
        return ChatReply(
            reply=(
                "Sorry, I ran into a problem handling that. Please try again "
                "or ask me for the CityPark office contact details."
            ),
            intent="error",
            refused=False,
            blocked=False,
            in_reservation=bot._active_session(req.session_id),
            sources=[],
            latency_ms=0.0,
        )

    # Metadata from the turn (never a DB lookup for a session model that does
    # not exist). ``chatbot_response`` may also be a ChatResponse object when
    # the graph ran without a checkpoint round-trip.
    turn = state.get("chatbot_response")
    intent = state.get("user_intent")
    if not isinstance(intent, str):
        intent = getattr(turn, "intent", None)
    if not isinstance(intent, str):
        intent = turn.get("intent") if isinstance(turn, dict) else None
    refused = bool(state.get("refused", getattr(turn, "refused", False)))
    blocked = bool(state.get("blocked", getattr(turn, "blocked", False)))
    sources = state.get("sources") or getattr(turn, "sources", None) or []
    if isinstance(turn, dict):
        refused = bool(turn.get("refused", refused))
        blocked = bool(turn.get("blocked", blocked))
        sources = turn.get("sources") or sources

    return ChatReply(
        reply=_reply_text(state),
        intent=intent or "info",
        refused=refused,
        blocked=blocked,
        in_reservation=bot._active_session(req.session_id),
        sources=sources,
        latency_ms=float(state.get("total_duration_ms") or 0.0),
    )
