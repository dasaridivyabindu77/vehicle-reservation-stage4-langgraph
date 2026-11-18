"""Interactive reservation collection (slot filling).

Collects the required booking details one at a time, validating each: first
name, last name, car (licence plate) number, and the reservation period
(start and end). When every slot is filled the flow produces a confirmation
summary and marks the booking ``pending_admin`` — the hand-off point for the
Stage-later human-in-the-loop confirmation.

The session keeps an explicit ``step`` (the field currently being collected).
A field is only advanced after it has been validated successfully, so an
invalid answer never loses previously collected data and can never silently
populate a different field.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,39}$")
PLATE_RE = re.compile(r"^[A-Z0-9]{4,10}$")

# Function words that never appear in a real personal name. They let the
# name slots reject conversational sentences ("I really like pizza") instead
# of silently storing them as a customer name. Common names that double as
# words (Mark, Will, May, Grace, ...) are deliberately NOT listed.
_NAME_STOPWORDS = {
    "i", "im", "me", "my", "mine", "you", "your", "yours", "we", "our",
    "us", "he", "she", "they", "them", "it", "its", "this", "that", "these",
    "those", "the", "a", "an", "is", "are", "am", "was", "were", "be", "been",
    "being", "do", "does", "did", "have", "has", "had", "can", "could",
    "would", "should", "to", "of",
    "and", "or", "but", "for", "with", "about", "from", "at", "in", "on",
    "like", "love", "hate", "really", "very", "just", "only", "also", "not",
    "no", "yes", "please", "thanks", "thank", "hello", "hi", "hey", "what",
    "who", "where", "when", "why", "how", "want", "need", "book", "booking",
    "reserve", "reservation", "park", "parking", "car", "spot", "space",
}

SLOTS: List[str] = ["first_name", "last_name", "car_number", "start", "end"]

PROMPTS: Dict[str, str] = {
    "first_name": "Let's book your spot. What's your first name?",
    "last_name": "Thanks! And your last name?",
    "car_number": "What's your car's licence plate number?",
    "start": "When would you like to start? (e.g. 2026-11-20 09:00)",
    "end": "When would you like to end? (e.g. 2026-11-20 18:00)",
}

CANCEL_WORDS = {"cancel", "stop", "quit", "exit", "nevermind", "never mind"}
RESET_WORDS = {"reset", "restart", "start over", "start again"}

_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?$"
)
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def normalize_command(text: str) -> str:
    """Normalise a possible command, ignoring case and trailing punctuation."""
    return re.sub(r"[\s!?.,;:]+$", "", text.strip().lower())


def _first_missing_slot(data: Dict[str, str]) -> Optional[str]:
    for slot in SLOTS:
        if slot not in data:
            return slot
    return None


@dataclass
class ReservationSession:
    data: Dict[str, str] = field(default_factory=dict)
    active: bool = False
    completed: bool = False
    reservation_id: Optional[int] = None
    escalated: bool = False  # Track if this reservation was escalated to admin
    # The field currently being collected. ``None`` means "not started" or
    # "finished"; next_slot() re-synchronises it from ``data`` when needed.
    step: Optional[str] = None

    def __post_init__(self) -> None:
        if self.step is None and self.active and not self.completed:
            self.step = _first_missing_slot(self.data)

    def next_slot(self) -> Optional[str]:
        """Return the field that must be answered next.

        The earliest unanswered field always wins. This keeps the flow
        self-healing even if an older session object was reconstructed from
        partial data, and makes it impossible to skip a required field.
        """
        if self.completed:
            self.step = None
            return None

        expected = _first_missing_slot(self.data)
        self.step = expected
        return expected

    @property
    def current_step(self) -> Optional[str]:
        """Alias used by callers that want to inspect the active step."""
        return self.next_slot()

    def reset(self) -> None:
        """Discard every collected value and restart at the first field."""
        self.data.clear()
        self.active = True
        self.completed = False
        self.reservation_id = None
        self.escalated = False
        self.step = SLOTS[0]

    def cancel(self) -> None:
        """Discard the booking and leave the session inactive."""
        self.data.clear()
        self.active = False
        self.completed = False
        self.reservation_id = None
        self.escalated = False
        self.step = None

    def is_complete(self) -> bool:
        return self.next_slot() is None


@dataclass
class IngestResult:
    reply: str
    done: bool = False
    reservation_ready: bool = False


def _parse_datetime(text: str) -> Optional[datetime]:
    """Parse the supported ``YYYY-MM-DD HH:MM`` (or ISO-T) format."""
    if not _DATETIME_RE.fullmatch(text):
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _looks_like_name(text: str) -> bool:
    """Heuristic gate for the name slots.

    Accepts one to three name-like tokens (letters, hyphens, apostrophes).
    Rejects conversational sentences via function-word detection and token
    count, so unrelated chatter cannot silently populate the name fields.
    """
    if not NAME_RE.fullmatch(text):
        return False
    tokens = [t for t in re.split(r"[\s'\-]+", text.lower()) if t]
    if not tokens or len(tokens) > 3:
        return False
    return not any(token in _NAME_STOPWORDS for token in tokens)


def _validate(slot: str, raw: str) -> tuple[Optional[str], Optional[str]]:
    """Validate reservation fields and return (value, error)."""

    text = raw.strip()

    if not text:
        return None, "Please enter a value."

    if slot in ("first_name", "last_name"):
        if not _looks_like_name(text):
            return None, "Please enter a valid name using letters only."
        return text.title(), None

    if slot == "car_number":
        normalized = text.upper().replace(" ", "").replace("-", "")
        if not PLATE_RE.fullmatch(normalized):
            return None, (
                "Please enter a valid licence plate with 4-10 "
                "letters or numbers (e.g. ABC1234)."
            )
        return normalized, None

    if slot in ("start", "end"):
        # Require a complete date *and* time; a date on its own is ambiguous.
        if not _DATETIME_RE.fullmatch(text):
            return None, (
                "Please enter the complete date and time in "
                "YYYY-MM-DD HH:MM format. Example: 2026-11-20 09:00."
            )

        dt = _parse_datetime(text)
        if dt is None:
            return None, (
                "Invalid date or time. Please use YYYY-MM-DD HH:MM, "
                "for example 2026-11-20 09:00."
            )

        if dt <= datetime.now():
            return None, (
                "That date/time is in the past. "
                "Please enter a future date and time."
            )

        return dt.isoformat(timespec="minutes"), None

    return text, None


def start_session() -> ReservationSession:
    """Create a clean, active session positioned at the first field."""
    return ReservationSession(active=True, step=SLOTS[0])


def first_prompt() -> str:
    return PROMPTS["first_name"]


def reset_reply() -> str:
    return "Your booking process has been reset. " + first_prompt()


def cancel_reply() -> str:
    return (
        "Your booking process has been cancelled. "
        "You can start a new reservation whenever you're ready."
    )


def ingest(
    session: ReservationSession,
    user_text: str,
    store=None,
    session_id: Optional[str] = None,
) -> IngestResult:
    """Feed one user message into the active reservation flow."""
    command = normalize_command(user_text)

    if command in RESET_WORDS:
        session.reset()
        return IngestResult(reply=reset_reply())

    if command in CANCEL_WORDS:
        session.cancel()
        return IngestResult(reply=cancel_reply(), done=True)

    slot = session.next_slot()
    if slot is None:
        return IngestResult(reply="Your reservation is already complete.", done=True)

    value, error = _validate(slot, user_text)
    if error:
        # Keep every previously collected value and stay on this same field.
        return IngestResult(reply=error)

    session.data[slot] = value  # type: ignore[assignment]

    # Cross-field validation: end must be after start. This runs only once
    # both values exist — never while the user is still answering the start.
    if slot == "end":
        start_value = session.data.get("start")
        if start_value is not None:
            start = datetime.fromisoformat(start_value)
            end = datetime.fromisoformat(session.data["end"])
            if end <= start:
                del session.data["end"]
                session.step = "end"
                return IngestResult(
                    reply="The end time must be after the start time. Please re-enter it."
                )

    # Advance only after the current field validated successfully.
    session.step = session.next_slot()

    if session.step is not None:
        return IngestResult(reply=PROMPTS[session.step])

    # All slots filled — finalise.
    session.completed = True
    session.active = False
    session.step = None
    if store is not None:
        # Only create a reservation once per session (prevents duplicates).
        if session.reservation_id is None:
            session.reservation_id = store.create_reservation(
                first_name=session.data["first_name"],
                last_name=session.data["last_name"],
                car_number=session.data["car_number"],
                start_ts=session.data["start"],
                end_ts=session.data["end"],
                session_id=session_id,
            )
            logger.info(
                "Created NEW reservation #%s for session %s",
                session.reservation_id,
                session_id,
            )
        else:
            logger.info(
                "Session %s already has reservation #%s, skipping duplicate creation",
                session_id,
                session.reservation_id,
            )
    return IngestResult(reply=confirmation(session), done=True, reservation_ready=True)


def confirmation(session: ReservationSession) -> str:
    """Summary shown to the user once every slot is filled.

    Stage 2 is human-in-the-loop: the booking is **not** auto-confirmed. It is
    recorded as ``pending_admin`` and handed to the administrator agent for
    approval, so the message here says the request is awaiting confirmation.
    """
    d = session.data
    ref = f"#{session.reservation_id}" if session.reservation_id else "(pending)"
    return (
        f"Here's your reservation request {ref}:\n"
        f"  Name:  {d['first_name']} {d['last_name']}\n"
        f"  Plate: {d['car_number']}\n"
        f"  From:  {d['start']}\n"
        f"  To:    {d['end']}\n"
        "Status: pending administrator confirmation.\n"
        "I've sent it to our administrator for approval — this usually takes a "
        "moment. Ask me \"what's the status of my booking?\" any time and I'll "
        "check for you."
    )
