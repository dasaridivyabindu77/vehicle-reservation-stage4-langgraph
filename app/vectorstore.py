"""Static-knowledge vector store.

The primary backend is **Weaviate** (self-hosted via docker-compose). To keep
the system runnable without the server — in unit tests, in CI, or on a laptop
with no Docker — a drop-in :class:`InMemoryKnowledgeStore` is used as an
automatic fallback. Both expose the same tiny interface:

    store.add_documents(docs)
    store.search(query, k) -> list[(Document, similarity)]

``similarity`` is cosine similarity in ``[0, 1]``. We supply our own vectors
(from :mod:`app.llm`), so Weaviate is configured with *no* vectorizer module.
"""

from __future__ import annotations

import logging
import socket
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from .config import Settings

logger = logging.getLogger(__name__)

WEAVIATE_CLASS = "ParkingKnowledge"


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


class BaseKnowledgeStore:
    """Common interface for knowledge stores."""

    backend: str = "base"

    def add_documents(self, docs: Sequence[Document]) -> None:  # pragma: no cover
        raise NotImplementedError

    def search(self, query: str, k: int = 4) -> List[Tuple[Document, float]]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - optional
        pass


class InMemoryKnowledgeStore(BaseKnowledgeStore):
    """Cosine-similarity vector store held entirely in process memory."""

    backend = "memory"

    def __init__(self, embeddings: Embeddings) -> None:
        self._embeddings = embeddings
        self._docs: List[Document] = []
        self._vectors: List[np.ndarray] = []

    def add_documents(self, docs: Sequence[Document]) -> None:
        texts = [d.page_content for d in docs]
        if not texts:
            return
        vectors = self._embeddings.embed_documents(texts)
        for doc, vec in zip(docs, vectors):
            self._docs.append(doc)
            self._vectors.append(np.asarray(vec, dtype=float))

    def search(self, query: str, k: int = 4) -> List[Tuple[Document, float]]:
        if not self._docs:
            return []
        q = np.asarray(self._embeddings.embed_query(query), dtype=float)
        scored = [(doc, _cosine(q, vec)) for doc, vec in zip(self._docs, self._vectors)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: max(k, 0)]


class WeaviateKnowledgeStore(BaseKnowledgeStore):
    """Weaviate v4 backend. Vectors are provided by us (no vectorizer module)."""

    backend = "weaviate"

    def __init__(self, client, embeddings: Embeddings) -> None:
        self._client = client
        self._embeddings = embeddings
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        import weaviate.classes.config as wvcc

        if self._client.collections.exists(WEAVIATE_CLASS):
            return
        self._client.collections.create(
            name=WEAVIATE_CLASS,
            vectorizer_config=wvcc.Configure.Vectorizer.none(),
            properties=[
                wvcc.Property(name="doc_id", data_type=wvcc.DataType.TEXT),
                wvcc.Property(name="title", data_type=wvcc.DataType.TEXT),
                wvcc.Property(name="category", data_type=wvcc.DataType.TEXT),
                wvcc.Property(name="text", data_type=wvcc.DataType.TEXT),
            ],
        )

    def add_documents(self, docs: Sequence[Document]) -> None:
        if not docs:
            return
        collection = self._client.collections.get(WEAVIATE_CLASS)
        vectors = self._embeddings.embed_documents([d.page_content for d in docs])
        with collection.batch.dynamic() as batch:
            for doc, vec in zip(docs, vectors):
                batch.add_object(
                    properties={
                        "doc_id": doc.metadata.get("doc_id", ""),
                        "title": doc.metadata.get("title", ""),
                        "category": doc.metadata.get("category", ""),
                        "text": doc.page_content,
                    },
                    vector=list(vec),
                )

    def search(self, query: str, k: int = 4) -> List[Tuple[Document, float]]:
        from weaviate.classes.query import MetadataQuery

        collection = self._client.collections.get(WEAVIATE_CLASS)
        qvec = self._embeddings.embed_query(query)
        res = collection.query.near_vector(
            near_vector=list(qvec),
            limit=max(k, 1),
            return_metadata=MetadataQuery(distance=True),
        )
        out: List[Tuple[Document, float]] = []
        for obj in res.objects:
            props = obj.properties
            doc = Document(
                page_content=props.get("text", ""),
                metadata={
                    "doc_id": props.get("doc_id", ""),
                    "title": props.get("title", ""),
                    "category": props.get("category", ""),
                },
            )
            distance = getattr(obj.metadata, "distance", None)
            similarity = 1.0 - distance if distance is not None else 0.0
            out.append((doc, float(similarity)))
        return out

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            pass


def _weaviate_reachable(url: str, timeout: float = 0.75) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 8080)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_knowledge_store(
    settings: Settings,
    embeddings: Embeddings,
    prefer_weaviate: Optional[bool] = None,
) -> BaseKnowledgeStore:
    """Build the best available knowledge store.

    Tries Weaviate when its port is reachable; otherwise (and on any connection
    error) falls back to the in-memory store. Pass ``prefer_weaviate=False`` to
    force the in-memory backend (used by unit tests).
    """
    if prefer_weaviate is False:
        return InMemoryKnowledgeStore(embeddings)

    if prefer_weaviate or _weaviate_reachable(settings.weaviate_url):
        try:
            import weaviate
            from weaviate.classes.init import Auth

            parsed = urlparse(settings.weaviate_url)
            auth = (
                Auth.api_key(settings.weaviate_api_key)
                if settings.weaviate_api_key
                else None
            )
            client = weaviate.connect_to_custom(
                http_host=parsed.hostname or "localhost",
                http_port=parsed.port or 8080,
                http_secure=parsed.scheme == "https",
                grpc_host=parsed.hostname or "localhost",
                grpc_port=50051,
                grpc_secure=parsed.scheme == "https",
                auth_credentials=auth,
                skip_init_checks=True,
            )
            logger.info("Connected to Weaviate at %s", settings.weaviate_url)
            return WeaviateKnowledgeStore(client, embeddings)
        except Exception as exc:  # noqa: BLE001 - fall back on anything
            logger.warning("Weaviate unavailable (%s); using in-memory store", exc)

    return InMemoryKnowledgeStore(embeddings)
