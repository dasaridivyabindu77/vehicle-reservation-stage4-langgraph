"""End-to-end regression tests for the conversational booking flow.

These tests drive the *real* chatbot and orchestration graph (no mocked
reservation logic) and assert the session state and persisted reservation
after every step. They cover the reported failures:

* elongated / mixed-case greetings (``hiiiii``) must not become refusals
* the booking lifecycle must advance one field at a time, in order
* invalid input must not advance the flow or lose collected values
* questions / greetings / unrelated chatter must not fill booking fields
* the web endpoint must process each message exactly once (no graph+fallback
  replay that made ``book`` the first name and reused start as end)
* the graph must interrupt for admin approval and resume exactly once
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.admin.agent import AdminAgent
from app.admin.escalation import EscalationService
from app.chatbot import Chatbot
from app.config import Settings
from app.orchestration_v2 import (
    build_production_graph,
    process_admin_decision_and_resume,
)
from app.reservation import PROMPTS, SLOTS
from app.web import server


def _future_date(days: int = 7) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        llm_provider="dial",
        dial_api_key="",
        sqlite_path=str(tmp_path / "app.db"),
        checkpoint_db=str(tmp_path / "checkpoints.db"),
        mcp_enabled=False,
        mcp_server_url="",
        admin_token="test-admin-token",
    )


@pytest.fixture
def bot(settings) -> Chatbot:
    instance = Chatbot(settings=settings, prefer_weaviate=False)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize(
    "greeting",
    ["hi", "hiiiii", "HELLOOOO", "hey there!", "Good Morning!", "howdy", "hi!!!"],
)
def test_greetings_are_not_refusals_and_do_not_start_booking(bot, greeting):
    session_id = f"greeting-{greeting}"
    response = bot.chat(greeting, session_id=session_id)

    assert response.refused is False
    assert "only help with CityPark" not in response.reply
    assert bot.reservation_session(session_id) is None


def test_full_booking_flow_collects_fields_in_order_and_persists(bot):
    sid = "e2e-chat-booking"
    day = _future_date()

    response = bot.chat("I'd like to book a parking space", session_id=sid)
    session = bot.reservation_session(sid)
    assert session is not None and session.active
    assert session.step == "first_name" and session.data == {}
    assert PROMPTS["first_name"] in response.reply

    response = bot.chat("Dasari", session_id=sid)
    assert session.data == {"first_name": "Dasari"}
    assert session.step == "last_name"
    assert PROMPTS["last_name"] in response.reply

    response = bot.chat("Divya", session_id=sid)
    assert session.data == {"first_name": "Dasari", "last_name": "Divya"}
    assert session.step == "car_number"
    assert PROMPTS["car_number"] in response.reply

    response = bot.chat("abc 1234", session_id=sid)
    assert session.data["car_number"] == "ABC1234"
    assert session.step == "start"
    assert PROMPTS["start"] in response.reply

    # A bare date is incomplete: reject it, keep the step and all data.
    response = bot.chat(day, session_id=sid)
    assert session.step == "start"
    assert "start" not in session.data
    assert "complete date and time" in response.reply.lower()
    assert session.data["first_name"] == "Dasari"

    response = bot.chat(f"{day} 09:00", session_id=sid)
    assert session.data["start"] == f"{day}T09:00"
    assert "end" not in session.data
    assert session.step == "end"
    assert PROMPTS["end"] in response.reply

    # End-before-start is only checked once both values exist; the valid start
    # must be preserved and the end field re-asked.
    response = bot.chat(f"{day} 08:00", session_id=sid)
    assert session.step == "end"
    assert "end" not in session.data
    assert session.data["start"] == f"{day}T09:00"
    assert "end time must be after the start time" in response.reply.lower()

    response = bot.chat(f"{day} 12:00", session_id=sid)
    assert session.data["end"] == f"{day}T12:00"
    assert session.completed and not session.active
    assert session.reservation_id is not None
    assert "reservation request" in response.reply.lower()

    stored = bot.sql.get_reservation(session.reservation_id)
    assert stored is not None
    assert stored.first_name == "Dasari"
    assert stored.last_name == "Divya"
    assert stored.car_number == "ABC1234"
    assert stored.start_ts == f"{day}T09:00"
    assert stored.end_ts == f"{day}T12:00"
    assert stored.status == "pending_admin"

    status = bot.chat("what is the status of my booking?", session_id=sid)
    assert f"#{stored.id}" in status.reply
    assert "pending administrator approval" in status.reply


def test_questions_and_greetings_during_booking_do_not_fill_fields(bot):
    sid = "e2e-booking-interruptions"
    bot.chat("book", session_id=sid)
    session = bot.reservation_session(sid)
    assert session.step == "first_name" and session.data == {}

    greeting = bot.chat("hiiiii", session_id=sid)
    assert bot.reservation_session(sid).data == {}
    assert bot.reservation_session(sid).step == "first_name"
    assert "only help with CityPark" not in greeting.reply

    question = bot.chat("what are your opening hours?", session_id=sid)
    assert bot.reservation_session(sid).data == {}
    assert bot.reservation_session(sid).step == "first_name"
    assert "only help with CityPark" not in question.reply

    unrelated = bot.chat("I really like pizza", session_id=sid)
    # It is treated as an (invalid) name answer, never stored silently.
    assert bot.reservation_session(sid).data == {}
    assert bot.reservation_session(sid).step == "first_name"
    assert "valid name" in unrelated.reply.lower()

    bot.chat("Dasari", session_id=sid)
    assert bot.reservation_session(sid).data == {"first_name": "Dasari"}


def test_reset_and_cancel_clear_state_and_start_clean(bot):
    sid = "e2e-reset-cancel"
    bot.chat("book", session_id=sid)
    bot.chat("Dasari", session_id=sid)
    assert bot.reservation_session(sid).data == {"first_name": "Dasari"}

    reset = bot.chat("reset", session_id=sid)
    session = bot.reservation_session(sid)
    assert session.active and not session.completed
    assert session.data == {} and session.step == "first_name"
    assert "reset" in reset.reply.lower()

    bot.chat("Dasari", session_id=sid)
    cancel = bot.chat("cancel", session_id=sid)
    session = bot.reservation_session(sid)
    assert not session.active and not session.completed
    assert session.data == {} and session.step is None
    assert "cancel" in cancel.reply.lower()

    bot.chat("book", session_id=sid)
    fresh = bot.reservation_session(sid)
    assert fresh.active and fresh.data == {} and fresh.step == SLOTS[0]


def test_web_chat_endpoint_processes_each_message_exactly_once(bot, settings, monkeypatch):
    monkeypatch.setattr(server, "_bot", bot)
    monkeypatch.setattr(server, "_graph", None)
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    client = TestClient(server.app)
    sid = "e2e-web-booking"
    day = _future_date(8)

    def post(message: str) -> dict:
        response = client.post("/chat", json={"message": message, "session_id": sid})
        assert response.status_code == 200, response.text
        return response.json()

    greeting = post("hiiiii")
    assert greeting["intent"] == "info"
    assert "only help with CityPark" not in greeting["reply"]
    assert bot.reservation_session(sid) is None

    booking = post("book")
    assert booking["in_reservation"] is True
    assert PROMPTS["first_name"] in booking["reply"]
    # Regression: duplicate processing turned "book" into the first name.
    assert bot.reservation_session(sid).data == {}

    assert PROMPTS["last_name"] in post("dasari")["reply"]
    assert bot.reservation_session(sid).data == {"first_name": "Dasari"}

    assert PROMPTS["car_number"] in post("divya")["reply"]
    assert PROMPTS["start"] in post("abc1234")["reply"]
    assert bot.reservation_session(sid).data["car_number"] == "ABC1234"

    assert "complete date and time" in post(day)["reply"].lower()
    assert bot.reservation_session(sid).step == "start"

    start_reply = post(f"{day} 09:00")
    assert PROMPTS["end"] in start_reply["reply"]
    assert bot.reservation_session(sid).data["start"] == f"{day}T09:00"
    assert "end" not in bot.reservation_session(sid).data

    early_end = post(f"{day} 08:00")
    assert "end time must be after the start time" in early_end["reply"].lower()
    assert bot.reservation_session(sid).data["start"] == f"{day}T09:00"
    assert "end" not in bot.reservation_session(sid).data

    confirmation = post(f"{day} 12:00")
    assert "reservation request" in confirmation["reply"].lower()
    session = bot.reservation_session(sid)
    assert session.completed and session.reservation_id is not None

    stored = bot.sql.get_reservation(session.reservation_id)
    assert (
        stored.first_name,
        stored.last_name,
        stored.car_number,
        stored.start_ts,
        stored.end_ts,
        stored.status,
    ) == (
        "Dasari",
        "Divya",
        "ABC1234",
        f"{day}T09:00",
        f"{day}T12:00",
        "pending_admin",
    )

    # The graph is now paused for administrator approval; a normal status
    # message must still be answered (not treated as an admin decision).
    status = post("what is the status of my booking?")
    assert f"#{stored.id}" in status["reply"]
    assert "pending administrator approval" in status["reply"]


class _RecordingMCP:
    def __init__(self) -> None:
        self.writes = []

    def is_available(self) -> bool:
        return True

    def write_confirmed_reservation(self, **kwargs) -> dict:
        self.writes.append(kwargs["car_number"])
        return {"success": True}


def test_graph_interrupts_for_admin_and_resumes_once(settings, bot):
    mcp = _RecordingMCP()
    escalation = EscalationService(bot.sql, AdminAgent(settings, None), mcp_client=mcp)
    graph = build_production_graph(
        settings,
        checkpointer_db=settings.checkpoint_db,
        chatbot=bot,
        escalation_service=escalation,
        mcp_client=mcp,
    )

    sid = "e2e-graph-booking"
    config = {"configurable": {"thread_id": sid}}
    day = _future_date(9)

    steps = [
        ("book", PROMPTS["first_name"]),
        ("Dasari", PROMPTS["last_name"]),
        ("Divya", PROMPTS["car_number"]),
        ("ABC1234", PROMPTS["start"]),
        (f"{day} 09:00", PROMPTS["end"]),
    ]
    for message, expected_prompt in steps:
        state = graph.invoke({"user_message": message, "session_id": sid}, config)
        assert expected_prompt in state["final_response"]

    final_state = graph.invoke(
        {"user_message": f"{day} 12:00", "session_id": sid}, config
    )
    assert final_state.get("__interrupt__")
    assert graph.get_state(config).next == ("escalate_to_admin",)
    # The user still receives the confirmation while the graph waits.
    assert "reservation request" in (final_state.get("chatbot_response") or "").lower()

    pending = escalation.pending()
    assert len(pending) == 1
    reservation_id = pending[0].id

    updated, _ = escalation.decide(
        reservation_id,
        "approve",
        reason="Looks good",
        decided_by="admin-dashboard",
        skip_mcp_write=True,
    )
    assert updated.status == "confirmed"

    resumed = process_admin_decision_and_resume(
        graph=graph,
        thread_id=sid,
        decision="approved",
        reason="Looks good",
        decided_by="admin-dashboard",
    )
    assert resumed["admin_decision"] == "approved"
    assert "approved" in (resumed.get("final_response") or "").lower()
    assert mcp.writes == ["ABC1234"]
    assert graph.get_state(config).next == ()

    status = graph.invoke(
        {"user_message": "what is the status of my booking?", "session_id": sid},
        config,
    )
    assert "confirmed" in status["final_response"].lower()
    # A follow-up status turn must not create a second escalation.
    assert len(escalation.pending()) == 0
