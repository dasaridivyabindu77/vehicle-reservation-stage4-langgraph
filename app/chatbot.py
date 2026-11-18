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
from .admin.agent import AdminAgent
from .admin.escalation import EscalationService
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
    r"\b(reserve|reservation|book(?:ing)?|make a booking|park my car|"
    r"park the car|parking (?:space|spot)|need (?:a )?parking)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"^(how|what|whats|what's|where|when|which|why|can|could|do|does|is|are|should|tell|explain)\b",
    re.IGNORECASE,
)
# Greetings may be elongated ("hiiiii", "helloooo", "heyyy") and can be
# capitalised in any way. Optional "there" keeps "hi there" natural.
_GREETING_RE = re.compile(
    r"^(?:hi+|hello+|hey+|hiya+|yo+|howdy+|greetings|"
    r"good\s+(?:morning|afternoon|evening|day))"
    r"(?:\s+there)?[\s!?.,]*$",
    re.IGNORECASE,
)
_CANCEL_WORDS = {"cancel", "stop", "quit", "exit", "nevermind", "never mind"}
_RESET_WORDS = {"reset", "restart", "start over", "start again"}
# CityPark topics that should be answered as information even while a booking
# is in progress (a statement containing these is not a booking-field answer).
_INFO_TOPIC_RE = re.compile(
    r"\b(hour|hours|open|opens|opening|close|closes|closing|price|prices|"
    r"cost|costs|fee|fees|rate|rates|tariff|availability|available|space|"
    r"spaces|location|located|address|where|ev|charging|charger|"
    r"accessibility|accessible|disabled|rules|policy|refund|payment|"
    r"contact|phone|email|zone|zones|booking process|how do i book)\b",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"\b(status|booking status|reservation status|check my booking|"
    r"check my reservation)\b",
    re.IGNORECASE,
)
# "Yes, confirm my booking" refers to an existing pending request rather than
# starting a brand-new booking. Kept separate from _BOOKING_RE so a normal
# "book a space" still starts the slot-filling flow.
_CONFIRM_RE = re.compile(
    r"\b(confirm|confirmed|submit|approve|go ahead|"
    r"yes[,\s]+(?:please\s+)?(?:confirm|book|submit|reserve))\b",
    re.IGNORECASE,
)
_RESERVATION_ID_RE = re.compile(
    r"\b(?:booking|reservation)\s*(?:number|reference)?\s*#?(\d+)\b|#(\d+)",
    re.IGNORECASE,
)
_SMALLTALK = {
    "hi", "hello", "hey", "hiya", "yo",
    "good morning", "good afternoon", "good evening", "good day",
    "thanks", "thank you", "thank",
    "help", "menu", "start",
    "sure", "okay", "ok", "alright", "got it", "got it thanks",
    "great", "cool", "nice", "perfect", "yes", "yeah", "yep", "no", "nope",
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


def _normalize_command(text: str) -> str:
    """Lower-case a message and strip trailing punctuation for commands."""
    return re.sub(r"[\s!?.,;:]+$", "", text.strip().lower())


def _is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text.strip()))


def _is_smalltalk_text(text: str) -> bool:
    """True for greetings/acknowledgements regardless of length or casing."""
    normalized = _normalize_command(text)
    if not normalized:
        return False
    if _is_greeting(normalized):
        return True
    if normalized in _SMALLTALK:
        return True
    # Compact forms such as "thankyou" and "gotit".
    return normalized.replace(" ", "") in {
        "thankyou", "thanks", "gotit", "goodmorning", "goodafternoon",
        "goodevening", "goodday",
    }


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
        escalation: Optional[EscalationService] = None,
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

        # Stage 2: administrator approval escalation.
        # Stage 3: MCP server for writing confirmed reservations to file.
        if escalation is not None:
            self.escalation = escalation
        else:
            admin_agent = AdminAgent(self.settings, self.chat_model)
            # Initialize MCP client if configured (Stage 3)
            mcp_client = None
            if self.settings.mcp_configured:
                try:
                    from mcp_server.client import MCPClient
                    mcp_client = MCPClient(
                        base_url=self.settings.mcp_server_url,
                        token=self.settings.mcp_server_token,
                    )
                    logger.info("Stage 3: MCP client initialized")
                except Exception as e:
                    logger.warning("Stage 3: Failed to initialize MCP client: %s", e)
            self.escalation = EscalationService(self.sql, admin_agent, mcp_client)

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
        normalized = _normalize_command(text)
        if _STATUS_RE.search(text):
            return "status"

        # Handle booking commands while a reservation is active.
        if session is not None and session.active:

            # Reset, restart and cancellation commands belong to the booking flow.
            if normalized in _CANCEL_WORDS or normalized in _RESET_WORDS:
                return "reserve"

            # Greetings, acknowledgements and CityPark questions must not be
            # consumed as the answer to the current booking field.
            if _is_smalltalk_text(text):
                return "info"
            if (
                _QUESTION_RE.match(text)
                or text.endswith("?")
                or _INFO_TOPIC_RE.search(text)
            ):
                return "info"

            # Otherwise, treat the message as the next booking answer.
            return "reserve"

        if normalized in _RESET_WORDS:
            return "reserve"

        # A confirmation refers to an existing request; send it through the
        # reservation handler so it is attached instead of starting over.
        if _CONFIRM_RE.search(text):
            return "reserve"

        # Start a new booking when the user expresses booking intent.
        if _BOOKING_RE.search(text) and not (
            _QUESTION_RE.match(text) or text.endswith("?")
        ):
            return "reserve"

        return "info"

    def run_reservation(self, session_id: str, message: str) -> str:
        session = self._sessions.get(session_id)
        normalized = _normalize_command(message)

        # Confirming an existing pending request must not start a second
        # booking. This also covers a process restart (no in-memory session)
        # and an abandoned draft: the confirmation belongs to the reservation
        # already stored for this chat.
        if _CONFIRM_RE.search(message) and not _STATUS_RE.search(message):
            pending = self.sql.get_latest_reservation_for_session(session_id)
            if pending is not None and pending.status == "pending_admin":
                if session is None or session.reservation_id != pending.id:
                    session = ReservationSession(
                        data={
                            "first_name": pending.first_name,
                            "last_name": pending.last_name,
                            "car_number": pending.car_number,
                            "start": pending.start_ts,
                            "end": pending.end_ts,
                        },
                        active=False,
                        completed=True,
                        reservation_id=pending.id,
                    )
                    self._sessions[session_id] = session
                return reservation.confirmation(session)

        if session is None or not session.active:
            if normalized in _CANCEL_WORDS:
                return "There's no booking in progress to cancel."
            session = reservation.start_session()
            self._sessions[session_id] = session
            if normalized in _RESET_WORDS:
                session.reset()
                return reservation.reset_reply()
            # The message that started the booking is intent, not a field value.
            return reservation.first_prompt()

        # A greeting/acknowledgement is not a booking-field answer. The router
        # normally sends these to the info path; this guard protects direct
        # callers as well.
        if _is_smalltalk_text(message):
            return self._help_text()

        # An explicit new booking request while a booking is in progress should
        # not overwrite the current field (e.g. "book" becoming a first name).
        if _BOOKING_RE.search(message) and normalized not in _RESET_WORDS:
            slot = session.next_slot()
            if slot is not None:
                return (
                    "We're already setting up your booking. "
                    + reservation.PROMPTS[slot]
                )

        result = reservation.ingest(
            session, message, store=self.sql, session_id=session_id
        )

        # Stage 2: Escalation is handled by the orchestration graph (orchestration.py).
        # The chatbot only tracks session state; escalate_to_admin_node() is the
        # single point that calls escalation.submit() to prevent duplicates.

        return result.reply

    def run_status(self, session_id: str, message: str) -> str:
        """Return the status of this chat's latest booking or a referenced one."""
        match = _RESERVATION_ID_RE.search(message)
        if match:
            reservation_id = int(match.group(1) or match.group(2))
            booking = self.sql.get_reservation(reservation_id)
        else:
            session = self._sessions.get(session_id)
            booking = (
                self.sql.get_reservation(session.reservation_id)
                if session and session.reservation_id
                else self.sql.get_latest_reservation_for_session(session_id)
            )

        if booking is None:
            return (
                "I couldn't find a reservation linked to this chat. "
                "If you have the reference number, ask me about booking #123."
            )

        status = {
            "pending_admin": "pending administrator approval",
            "confirmed": "confirmed",
            "refused": "refused",
        }.get(booking.status, booking.status.replace("_", " "))
        reply = f"Reservation #{booking.id} status: {status}."
        if booking.status == "refused" and booking.admin_reason:
            reply += f" Reason: {booking.admin_reason}"
        return reply

    def retrieve(self, message: str) -> Tuple[List[Tuple[Document, float]], str]:
        # Primary retrieval via vector search
        k = self.settings.top_k

        # In offline mode (weak embeddings), retrieve more candidates to ensure
        # we don't miss relevant documents due to poor semantic matching
        if not self.settings.llm_configured:
            k = min(k * 2, 10)

        hits = self.store.search(message, k=k)
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
            normalized = _normalize_command(message)

            if normalized in {"sure", "okay", "ok", "alright", "got it",
                              "yes", "yeah", "yep", "great", "cool", "nice",
                              "perfect", "no", "nope"}:
                return (
                    "Great! What would you like to know about CityPark? "
                    "Anything else I can help with?",
                    False
                )

            if normalized in {"thanks", "thank you", "thank", "thankyou"}:
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
        allow = list(session.data.values()) if session else []
        if session and session.data.get("first_name") and session.data.get("last_name"):
            allow.append(
                f"{session.data['first_name']} {session.data['last_name']}"
            )
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
        return _is_smalltalk_text(message)

    @staticmethod
    def _help_text() -> str:
        return (
            "Hi! I'm the CityPark assistant. I can tell you about opening hours, "
            "prices, space availability, our location, EV charging, accessibility "
            "and the rules — and I can take a parking reservation for you. "
            "Just ask, or say \"I'd like to book a space\"."
        )

    def reservation_session(self, session_id: str) -> Optional[ReservationSession]:
        """Return the booking session for ``session_id`` (or ``None``).

        Public accessor used by the web layer to report accurate turn metadata
        without reaching into private state or re-running a chat turn.
        """
        return self._sessions.get(session_id)

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
