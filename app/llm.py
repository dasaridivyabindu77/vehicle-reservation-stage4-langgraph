"""Factories for the chat model and embeddings.

Supports both EPAM DIAL (Azure-OpenAI compatible) and standard OpenAI backends
when configured, and falls back to offline implementations otherwise.
``langchain_openai`` is imported lazily *inside* the keyed branch so the offline
path needs neither the package's network stack nor a key.
"""

from __future__ import annotations

from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from .config import Settings
from .embeddings import HashingEmbeddings


def build_embeddings(settings: Settings) -> Embeddings:
    """Return provider embeddings when keyed, else deterministic hashing embeddings."""
    if not settings.llm_configured:
        return HashingEmbeddings(dim=settings.embedding_dim)

    provider = settings.llm_provider.lower().strip()

    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            timeout=30.0,
            max_retries=2,
        )

    # Default to DIAL (Azure-OpenAI compatible)
    from langchain_openai import AzureOpenAIEmbeddings

    return AzureOpenAIEmbeddings(
        azure_endpoint=settings.dial_endpoint,
        api_key=settings.dial_api_key,
        api_version=settings.dial_api_version,
        azure_deployment=settings.dial_embedding_model,
        timeout=30.0,
        max_retries=2,
    )


def build_chat_model(settings: Settings) -> Optional[BaseChatModel]:
    """Return a chat model for the configured provider, or ``None`` in offline mode.

    A ``None`` model signals the chatbot to build an extractive answer directly
    from the retrieved context instead of calling a generative model.
    """
    if not settings.llm_configured:
        return None

    provider = settings.llm_provider.lower().strip()

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            api_key=settings.openai_api_key,
            model=settings.openai_chat_model,
            temperature=0.0,
            timeout=60.0,
            max_retries=2,
        )

    # Default to DIAL (Azure-OpenAI compatible)
    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(
        azure_endpoint=settings.dial_endpoint,
        api_key=settings.dial_api_key,
        api_version=settings.dial_api_version,
        azure_deployment=settings.dial_chat_model,
        temperature=0.0,
        timeout=60.0,
        max_retries=2,
    )
