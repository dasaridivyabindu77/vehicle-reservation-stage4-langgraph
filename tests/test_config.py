from app.config import Settings


def test_offline_when_key_absent():
    s = Settings(dial_api_key="")
    assert s.llm_configured is False


def test_online_when_key_present():
    s = Settings(dial_api_key="dial-abc123")
    assert s.llm_configured is True


def test_defaults_present():
    s = Settings(dial_api_key="")
    assert s.top_k > 0
    assert 0 <= s.relevance_min_overlap <= 1
    assert s.dial_endpoint.startswith("http")
