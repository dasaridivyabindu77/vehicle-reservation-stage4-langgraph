"""Factories for the chat model and embeddings.

Both pick the EPAM DIAL (Azure-OpenAI compatible) backend when a key is
configured, and fall back to offline implementations otherwise. ``langchain_openai``
is imported lazily *inside* the keyed branch so the offline path needs neither
the package's network stack nor a key.
"""

from __future__ import annotations

from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from .config import Settings
from .embeddings import HashingEmbeddings


def build_embeddings(settings: Settings) -> Embeddings:
    """Return DIAL embeddings when keyed, else deterministic hashing embeddings."""
    if not settings.llm_configured:
        return HashingEmbeddings(dim=settings.embedding_dim)

    from langchain_openai import AzureOpenAIEmbeddings

    return AzureOpenAIEmbeddings(
        azure_endpoint=settings.dial_endpoint,
        api_key=settings.dial_api_key,
        api_version=settings.dial_api_version,
        azure_deployment=settings.embedding_model,
        timeout=30.0,
        max_retries=2,
    )


def build_chat_model(settings: Settings) -> Optional[BaseChatModel]:
    """Return a DIAL chat model, or ``None`` in offline mode.

    A ``None`` model signals the chatbot to build an extractive answer directly
    from the retrieved context instead of calling a generative model.
    """
    if not settings.llm_configured:
        return None

    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(
        azure_endpoint=settings.dial_endpoint,
        api_key=settings.dial_api_key,
        api_version=settings.dial_api_version,
        azure_deployment=settings.chat_model,
        temperature=0.0,
        timeout=60.0,
        max_retries=2,
    )
