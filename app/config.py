"""Application configuration.

Settings load from environment variables / a local ``.env`` file. The single
most important switch is ``DIAL_API_KEY``: when it is empty the whole system
runs in a deterministic *offline* mode (hashing embeddings + extractive
answers) so development, tests and CI need no network access.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- EPAM DIAL (Azure-OpenAI compatible) ---
    dial_api_key: str = ""
    dial_endpoint: str = "https://ai-proxy.lab.epam.com"
    dial_api_version: str = "2024-02-01"
    chat_model: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small-1"

    # --- Weaviate ---
    weaviate_url: str = "http://localhost:8080"
    weaviate_api_key: str = ""

    # --- Dynamic data ---
    sqlite_path: str = "citypark.db"

    # --- Retrieval & grounding ---
    top_k: int = 4
    relevance_min_overlap: float = 0.12
    relevance_min_score: float = 0.30
    embedding_dim: int = 256

    @property
    def llm_configured(self) -> bool:
        """True when a real DIAL key is present (i.e. not offline mode)."""
        return bool(self.dial_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings instance."""
    return Settings()
