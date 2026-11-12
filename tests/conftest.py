"""Shared test fixtures.

All tests run in deterministic **offline** mode: no DIAL key, in-memory vector
store, and a throwaway SQLite file per test. This is enforced regardless of any
local ``.env`` by clearing ``DIAL_API_KEY`` in the environment before settings
are constructed.
"""

from __future__ import annotations

import os

# Force offline mode before any app module reads settings.
os.environ["DIAL_API_KEY"] = ""

import pytest  # noqa: E402

from app.config import Settings  # noqa: E402
from app.chatbot import Chatbot  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(dial_api_key="", sqlite_path=str(tmp_path / "test.db"))


@pytest.fixture
def bot(settings) -> Chatbot:
    chatbot = Chatbot(settings=settings, prefer_weaviate=False)
    yield chatbot
    chatbot.close()
