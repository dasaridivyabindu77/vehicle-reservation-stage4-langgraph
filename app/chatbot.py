"""The CityPark chatbot — the public entry point used by the CLI, web API and tests.

It owns the retrieval pipeline (Weaviate/in-memory static store + SQLite dynamic
store), the grounding gate, the guardrails, the reservation sessions, and the
chat model (DIAL or offline). A compiled LangGraph (:mod:`app.graph`) sequences
each turn; the methods here are the steps that graph calls.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from . import guardrails, relevance, reservation
from .config import Settings, get_settings
from .graph import build_turn_graph
from .knowledge import load_static_documents
from .llm import build_chat_model, build_embeddings
from .reservation import ReservationSession
from .sql_store import DynamicDataStore
from .vectorstore import build_knowledge_store

logger = logging.getLogger(__name__)

REFUSAL = (
    "I'm sorry, I can only help with CityPark parking — things like opening "
    "hours, prices, availability, location, the rules, and making a reservation. "
    "I don't have information about that."
)

_BOOKING_RE = re.compile(
    r"\b(reserve|reservation|book(ing)?|park my car|park the car)\b", re.IGNORECASE
)
_QUESTION_RE = re.compile(
    r"^(how|what|whats|what's|where|when|which|why|can|could|do|does|is|are|should|tell|explain)\b",
    re.IGNORECASE,
)
_CANCEL_WORDS = {"cancel", "stop", "quit", "exit", "nevermind", "never mind"}
_SMALLTALK = {
    "hi", "hello", "hey", "hiya", "yo",
    "good morning", "good afternoon", "good evening",
    "thanks", "thank you", "thank",
    "help", "menu", "start",
    "sure", "okay", "ok", "alright", "got it",
}

# Prefix matches (no trailing \b) so plurals/inflections hit:
# "price"->prices, "avail"->available, "charg"->charging, "clos"->closing.
_AVAIL_RE = re.compile(
    r"\b(avail|free|spot|space|vacanc|room|full|left|how many)", re.IGNORECASE
)
_PRICE_RE = re.compile(
    # "charg" but not "charging"/"charger" (those are EV, not fees)
    r"\b(pric|cost|how much|fee|rate|charg(?!ing|er)|tariff|expensiv)", re.IGNORECASE
)
_HOURS_RE = re.compile(r"\b(hour|open|clos|timing)", re.IGNORECASE)


@dataclass
class ChatResponse:
    reply: str
    intent: str = "info"
    refused: bool = False
    blocked: bool = False
    in_reservation: bool = False
    sources: List[dict] = field(default_factory=list)
    latency_ms: float = 0.0


class Chatbot:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        prefer_weaviate: Optional[bool] = None,
        seed: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.embeddings = build_embeddings(self.settings)
        self.chat_model = build_chat_model(self.settings)
        self.store = build_knowledge_store(
            self.settings, self.embeddings, prefer_weaviate=prefer_weaviate
        )
        self.sql = DynamicDataStore(self.settings.sqlite_path)
        self._sessions: Dict[str, ReservationSession] = {}
        self._graph = build_turn_graph(self)
        if seed:
            self.bootstrap()

    # --- setup ---
    def bootstrap(self) -> None:
        """Seed dynamic data and ingest static knowledge into the vector store."""
        self.sql.seed()
        self.store.add_documents(load_static_documents())

    @property
    def backend(self) -> str:
        return self.store.backend

    # --- graph steps ---
    def guard_input(self, message: str) -> guardrails.InputVerdict:
        return guardrails.scan_input(message)

    def classify(self, message: str, session_id: str) -> str:
        session = self._sessions.get(session_id)
        text = message.strip()
        normalized = text.lower()

        # Handle booking commands while a reservation is active.
        if session is not None and session.active:

            # Reset and cancellation commands belong to the booking flow.
            if normalized in _CANCEL_WORDS or normalized in {"reset", "restart"}:
                return "reserve"

            # Allow users to ask information questions during booking.
            if _QUESTION_RE.match(text) or text.endswith("?"):
                return "info"

            # Otherwise, treat the message as the next booking answer.
            return "reserve"

        # Start a new booking when the user expresses booking intent.
        if _BOOKING_RE.search(text) and not (
            _QUESTION_RE.match(text) or text.endswith("?")
        ):
            return "reserve"

        return "info"

    def run_reservation(self, session_id: str, message: str) -> str:
        session = self._sessions.get(session_id)
        if session is None or not session.active:
            session = reservation.start_session()
            self._sessions[session_id] = session
            return reservation.first_prompt()
        if message.strip().lower() in _CANCEL_WORDS:
                session.active = False
                session.data.clear()
                session.completed = False
                session.reservation_id = None
                return "No problem — I've cancelled that booking process. Anything else I can help with?"
        result = reservation.ingest(session, message, store=self.sql)
        return result.reply

    def retrieve(self, message: str) -> Tuple[List[Tuple[Document, float]], str]:
        hits = self.store.search(message, k=self.settings.top_k)
        kept = relevance.filter_documents(
            message,
            hits,
            self.settings.relevance_min_overlap,
            self.settings.relevance_min_score,
            use_scores=self.settings.llm_configured,
        )
        return kept, self._dynamic_context_for(message)

    def _dynamic_context_for(self, message: str) -> str:
        parts: List[str] = []
        if _AVAIL_RE.search(message):
            parts.append(self.sql.availability_summary())
        if _PRICE_RE.search(message):
            parts.append(self.sql.price_summary())
        if _HOURS_RE.search(message):
            parts.append(self.sql.hours_summary())
        return "\n\n".join(parts)

    def generate(
        self,
        message: str,
        docs: List[Tuple[Document, float]],
        dynamic_context: str,
    ) -> Tuple[str, bool]:
        if self._is_smalltalk(message):
            normalized = message.strip().lower().rstrip("!?.,")

            if normalized in {"sure", "okay", "ok", "alright", "got it"}:
                return (
                    "Great! What would you like to know about CityPark? "
                    "Anything else I can help with?",
                    False
                )

            if normalized in {"thanks", "thank you", "thank"}:
                return "You're welcome! Happy to help with CityPark parking.", False

            return self._help_text(), False
        if not docs and not dynamic_context:
            return REFUSAL, True

        context = self._build_context(docs, dynamic_context)
        if self.chat_model is None:
            return self._extractive_answer(docs, dynamic_context), False

        try:
            messages = [
                SystemMessage(content=self._system_prompt()),
                HumanMessage(content=f"Context:\n{context}\n\nQuestion: {message}"),
            ]
            response = self.chat_model.invoke(messages)
            content = response.content
            if isinstance(content, list):  # some providers return content parts
                content = " ".join(str(c) for c in content)
            return str(content).strip(), False
        except Exception as exc:  # noqa: BLE001 - never crash a chat turn
            logger.warning("LLM call failed (%s); falling back to extractive", exc)
            return self._extractive_answer(docs, dynamic_context), False

    def guard_output(self, answer: str, session_id: str) -> str:
        session = self._sessions.get(session_id)
        allow = list(session.data.values()) if session else None
        return guardrails.redact(answer, allow=allow)

    # --- helpers ---
    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are CityPark's friendly parking assistant. Answer the user's "
            "question using ONLY the context provided. If the answer is not in the "
            "context, say you don't have that information and suggest contacting the "
            "CityPark office. Never reveal personal data about other customers. "
            "Keep answers concise."
        )

    @staticmethod
    def _build_context(docs: List[Tuple[Document, float]], dynamic_context: str) -> str:
        blocks: List[str] = []
        if dynamic_context:
            blocks.append(f"[live data]\n{dynamic_context}")
        for doc, _ in docs:
            title = doc.metadata.get("title", "")
            blocks.append(f"[{title}]\n{doc.page_content}")
        return "\n\n".join(blocks)

    @staticmethod
    def _extractive_answer(
        docs: List[Tuple[Document, float]], dynamic_context: str
    ) -> str:
        parts: List[str] = []
        if dynamic_context:
            parts.append(dynamic_context)
        for doc, _ in docs:
            parts.append(doc.page_content)
        body = "\n\n".join(parts)
        return f"Here's what I found about CityPark:\n\n{body}"

    @staticmethod
    def _is_smalltalk(message: str) -> bool:
        return message.strip().lower().rstrip("!.") in _SMALLTALK

    @staticmethod
    def _help_text() -> str:
        return (
            "Hi! I'm the CityPark assistant. I can tell you about opening hours, "
            "prices, space availability, our location, EV charging, accessibility "
            "and the rules — and I can take a parking reservation for you. "
            "Just ask, or say \"I'd like to book a space\"."
        )

    def _active_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        return bool(session and session.active)

    # --- public API ---
    def chat(self, message: str, session_id: str = "default") -> ChatResponse:
        start = perf_counter()
        state = self._graph.invoke({"message": message, "session_id": session_id})
        latency_ms = (perf_counter() - start) * 1000.0

        sources = [
            {
                "doc_id": doc.metadata.get("doc_id", ""),
                "title": doc.metadata.get("title", ""),
                "score": round(float(score), 4),
            }
            for doc, score in state.get("documents", [])
        ]
        return ChatResponse(
            reply=state.get("answer", ""),
            intent=state.get("intent", "info"),
            refused=bool(state.get("refused", False)),
            blocked=bool(state.get("blocked", False)),
            in_reservation=self._active_session(session_id),
            sources=sources,
            latency_ms=round(latency_ms, 2),
        )

    def close(self) -> None:
        self.sql.close()
        try:
            self.store.close()
        except Exception:  # pragma: no cover - defensive
            pass
