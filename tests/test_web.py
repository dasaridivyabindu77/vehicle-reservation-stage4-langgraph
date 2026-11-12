"""Web API tests using FastAPI's TestClient against an offline bot."""

from fastapi.testclient import TestClient

from app.chatbot import Chatbot
from app.config import Settings
from app.web import server


def _client(tmp_path):
    bot = Chatbot(
        settings=Settings(dial_api_key="", sqlite_path=str(tmp_path / "web.db")),
        prefer_weaviate=False,
    )
    server._bot = bot
    return TestClient(server.app), bot


def test_health_reports_backend(tmp_path):
    client, bot = _client(tmp_path)
    try:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["llm"] == "offline"
    finally:
        bot.close()
        server._bot = None


def test_chat_endpoint_answers(tmp_path):
    client, bot = _client(tmp_path)
    try:
        r = client.post("/chat", json={"message": "what are the prices?", "session_id": "t"})
        assert r.status_code == 200
        body = r.json()
        assert body["refused"] is False
        assert "price" in body["reply"].lower() or "$" in body["reply"]
    finally:
        bot.close()
        server._bot = None


def test_index_served(tmp_path):
    client, bot = _client(tmp_path)
    try:
        r = client.get("/")
        assert r.status_code == 200
        assert "CityPark" in r.text
    finally:
        bot.close()
        server._bot = None
