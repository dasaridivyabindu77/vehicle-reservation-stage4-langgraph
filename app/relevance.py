"""Grounding / refusal gate.

A retrieved document is kept only when it is plausibly *about* the user's
question. We combine two signals:

* **Lexical content-word overlap** — deterministic and model-agnostic, so it
  works even with the offline hashing embeddings.
* **Embedding similarity score** — only trusted when a real embedding model is
  configured (offline hashing scores are noisy).

A document passes if *either* signal clears its threshold. If nothing passes,
the chatbot refuses instead of hallucinating an answer.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from langchain_core.documents import Document

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Small built-in stopword list — enough to stop function words dominating overlap.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could", "do",
    "does", "for", "from", "had", "has", "have", "how", "i", "if", "in", "is", "it",
    "its", "me", "my", "no", "not", "of", "on", "or", "please", "so", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "to", "too",
    "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your", "yours", "about", "would", "want", "need", "tell",
    "give", "get", "know", "there", "any", "some", "much", "many",
    # Domain-ubiquitous terms: the brand/topic appears in nearly every document,
    # so they carry no discriminating signal and would inflate overlap.
    "citypark", "parking", "park",
}

# Synonym groups for semantic matching in offline mode
# Maps words to their semantic equivalents for better lexical matching
_SYNONYMS = {
    "address": ["location", "located", "place", "where", "site"],
    "location": ["address", "located", "place", "where", "site"],
    "located": ["location", "address", "place", "where", "site"],
    "price": ["cost", "fee", "rate", "charge", "tariff"],
    "cost": ["price", "fee", "rate", "charge", "tariff"],
    "open": ["available", "operating", "hour", "timing"],
    "hour": ["time", "timing", "schedule", "open", "close"],
    "available": ["free", "vacancy", "spot", "space"],
}


def content_words(text: str) -> set:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1}


def _normalize_word(word: str) -> str:
    """Simple normalization: remove common suffixes for better matching."""
    # Common suffixes
    for suffix in ['ed', 'ing', 's', 'es', 'ion', 'ions']:
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[:-len(suffix)]
    return word


def _words_match(word1: str, word2: str) -> bool:
    """Check if two words match, accounting for common variations and synonyms."""
    # Exact match
    if word1 == word2:
        return True

    # Normalized match (location <-> located)
    norm1 = _normalize_word(word1)
    norm2 = _normalize_word(word2)

    if norm1 == norm2:
        return True

    # Prefix match for longer words (locat matches location, located, etc)
    if len(norm1) >= 4 and len(norm2) >= 4:
        if norm1.startswith(norm2) or norm2.startswith(norm1):
            return True

    # Synonym match for common semantic equivalents
    if word1 in _SYNONYMS:
        if word2 in _SYNONYMS[word1]:
            return True
        # Also check normalized versions of synonyms
        if any(_normalize_word(syn).startswith(norm2) or norm2.startswith(_normalize_word(syn))
               for syn in _SYNONYMS[word1]):
            return True

    if word2 in _SYNONYMS:
        if word1 in _SYNONYMS[word2]:
            return True
        if any(_normalize_word(syn).startswith(norm1) or norm1.startswith(_normalize_word(syn))
               for syn in _SYNONYMS[word2]):
            return True

    return False


def lexical_overlap(query: str, document: str) -> float:
    """Fraction of the query's content words that appear in the document.

    Uses fuzzy matching to handle word variations (e.g., "located" matches "location").
    """
    q = content_words(query)
    if not q:
        return 0.0
    d = content_words(document)

    # Count matches using fuzzy word matching
    matches = 0
    for q_word in q:
        for d_word in d:
            if _words_match(q_word, d_word):
                matches += 1
                break  # Count each query word at most once

    return matches / len(q)


def is_relevant(
    query: str,
    document: str,
    score: Optional[float],
    min_overlap: float,
    min_score: float,
) -> bool:
    if lexical_overlap(query, document) >= min_overlap:
        return True
    if score is not None and score >= min_score:
        return True
    return False


def filter_documents(
    query: str,
    scored_docs: Sequence[Tuple[Document, float]],
    min_overlap: float,
    min_score: float,
    use_scores: bool,
) -> List[Tuple[Document, float]]:
    """Keep only documents that pass the grounding gate.

    ``use_scores`` should mirror ``settings.llm_configured``: embedding scores
    are only considered when a real embedding model produced them.
    """
    kept: List[Tuple[Document, float]] = []
    for doc, score in scored_docs:
        effective_score = score if use_scores else None
        searchable_text = " ".join(
            [
                doc.page_content,
                doc.metadata.get("title", ""),
                doc.metadata.get("category", ""),
            ]
        )

        if is_relevant(
            query,
            searchable_text,
            effective_score,
            min_overlap,
            min_score,
        ):
            kept.append((doc, score))
    return kept
