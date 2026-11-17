from app.config import Settings


def test_offline_when_key_absent():
    s = Settings(dial_api_key="", openai_api_key="")
    assert s.llm_configured is False


def test_online_when_dial_key_present():
    s = Settings(dial_api_key="dial-abc123", llm_provider="dial")
    assert s.llm_configured is True


def test_online_when_openai_key_present():
    s = Settings(openai_api_key="sk-abc123", llm_provider="openai")
    assert s.llm_configured is True


def test_defaults_present():
    s = Settings(dial_api_key="")
    assert s.top_k > 0
    assert 0 <= s.relevance_min_overlap <= 1
    assert s.dial_endpoint.startswith("http")


def test_provider_selection_dial():
    s = Settings(
        dial_api_key="dial-key",
        llm_provider="dial",
        dial_chat_model="gpt-4o",
        dial_embedding_model="text-embedding-3-small-1"
    )
    assert s.llm_configured is True
    assert s.chat_model == "gpt-4o"
    assert s.embedding_model == "text-embedding-3-small-1"


def test_provider_selection_openai():
    s = Settings(
        openai_api_key="sk-key",
        llm_provider="openai",
        openai_chat_model="gpt-4o",
        openai_embedding_model="text-embedding-3-small"
    )
    assert s.llm_configured is True
    assert s.chat_model == "gpt-4o"
    assert s.embedding_model == "text-embedding-3-small"


def test_validate_configuration_valid_dial():
    s = Settings(
        dial_api_key="dial-key",
        llm_provider="dial",
        dial_endpoint="https://example.com",
        dial_chat_model="gpt-4o",
        dial_embedding_model="text-embedding"
    )
    errors = s.validate_configuration()
    assert len(errors) == 0


def test_validate_configuration_valid_openai():
    s = Settings(
        openai_api_key="sk-key",
        llm_provider="openai",
        openai_chat_model="gpt-4o",
        openai_embedding_model="text-embedding-3-small"
    )
    errors = s.validate_configuration()
    assert len(errors) == 0


def test_validate_configuration_invalid_provider():
    s = Settings(
        dial_api_key="key",
        llm_provider="invalid"
    )
    errors = s.validate_configuration()
    assert len(errors) > 0
    assert any("Invalid LLM_PROVIDER" in e for e in errors)


def test_validate_configuration_missing_dial_key():
    s = Settings(
        dial_api_key="",
        llm_provider="dial"
    )
    # Offline mode, should not error
    errors = s.validate_configuration()
    assert len(errors) == 0


def test_validate_configuration_missing_openai_key():
    s = Settings(
        openai_api_key="",
        llm_provider="openai"
    )
    # Offline mode, should not error
    errors = s.validate_configuration()
    assert len(errors) == 0
