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

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..chatbot import Chatbot

logger = logging.getLogger(__name__)

_bot: Chatbot | None = None
_bot_lock = threading.Lock()
_INDEX = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def get_bot() -> Chatbot:
    """Return the singleton chatbot, building it at most once (thread-safe)."""
    global _bot
    if _bot is None:
        with _bot_lock:
            if _bot is None:  # double-checked locking
                _bot = Chatbot()
    return _bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the bot once at startup so the first request isn't a cold network build.
    try:
        get_bot()
        logger.info("Chatbot ready")
    except Exception as exc:  # noqa: BLE001 - don't block startup
        logger.warning("Chatbot warm-up failed (%s); will retry on first request", exc)
    yield


app = FastAPI(title="CityPark Assistant", version="0.1.0", lifespan=lifespan)


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
    return {
        "status": "ok",
        "vector_backend": bot.backend,
        "llm": "dial" if bot.settings.llm_configured else "offline",
    }


@app.post("/chat", response_model=ChatReply)
def chat(req: ChatRequest) -> ChatReply:
    bot = get_bot()
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
