"""Offline, deterministic embeddings.

Used whenever no DIAL key is configured. The vectors are produced by hashing
content words into a fixed-dimension bag-of-words space and L2-normalising, so
they are reproducible across runs and machines with zero dependencies and no
network. They are *not* semantically rich — they exist so the full retrieval
pipeline (vector store, grounding gate, graph) is exercisable offline and in
CI. Lexical overlap, not these vectors, is the primary grounding signal offline.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import List

from langchain_core.embeddings import Embeddings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


class HashingEmbeddings(Embeddings):
    """Deterministic hashing bag-of-words embeddings."""

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = _tokens(text)
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)
