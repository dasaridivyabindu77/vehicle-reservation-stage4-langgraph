"""NLP-based PII detection using spaCy for context-aware guardrails.

This module provides machine learning-based detection of personally identifiable
information (PII) using natural language processing. It complements the regex-based
detection in guardrails.py with entity recognition and contextual understanding.

Requirements:
    pip install spacy
    python -m spacy download en_core_web_sm

Usage:
    from app.nlp_detector import NLPDetector

    detector = NLPDetector()
    is_phone = detector.is_phone_number("Call me at 555-123-4567")
    entities = detector.extract_pii("John Smith lives at 123 Main St")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# Regex fallbacks (from guardrails.py) - enhanced for international formats
PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4,}"  # (555) 123-4567
    r"|"
    r"(?:\+\d{1,3}[\s.-]?)?\d{3}[\s.-]\d{3}[\s.-]\d{4,}"  # 555-123-4567
    r"|"
    r"(?:\+\d{1,3}[\s.-]?)?\d{10,}"  # Continuous 10+ digits
    r"|"
    r"\+\d{1,3}[\s.-]\d{1,4}[\s.-]\d{4,}"  # +44-20-7123-4567
    r"|"
    r"\+\d{1,3}[\s.-]\d{1,4}[\s.-]\d{2}[\s.-]\d{2}[\s.-]\d{2}[\s.-]\d{2}"  # +33-1-23-45-67-89
    r")"
    r"(?!\d)"
)

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
ISO_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


@dataclass
class PIIEntity:
    """A detected PII entity with context."""
    text: str
    label: str  # PERSON, ORG, PHONE, EMAIL, etc.
    start: int
    end: int
    confidence: float


class NLPDetector:
    """NLP-based PII detector using spaCy.

    Falls back to regex if spaCy is not available.
    """

    def __init__(self, model_name: str = "en_core_web_sm"):
        """Initialize the NLP detector.

        Args:
            model_name: spaCy model to load (default: en_core_web_sm)
        """
        self.nlp = None
        self.spacy_available = False

        try:
            import spacy
            self.nlp = spacy.load(model_name)
            self.spacy_available = True
            logger.info(f"NLP detector initialized with spaCy model: {model_name}")
        except (ImportError, OSError) as e:
            logger.warning(
                f"spaCy not available: {e}. "
                "Falling back to regex-based detection. "
                "Install with: pip install spacy && python -m spacy download en_core_web_sm"
            )

    def extract_pii(self, text: str) -> List[PIIEntity]:
        """Extract all PII entities from text.

        Args:
            text: The text to analyze

        Returns:
            List of detected PII entities
        """
        entities = []

        if self.spacy_available and self.nlp:
            # Use spaCy NER
            doc = self.nlp(text)

            for ent in doc.ents:
                # Filter for PII-relevant entity types
                if ent.label_ in ["PERSON", "ORG", "GPE", "LOC", "NORP"]:
                    entities.append(PIIEntity(
                        text=ent.text,
                        label=ent.label_,
                        start=ent.start_char,
                        end=ent.end_char,
                        confidence=1.0,  # spaCy doesn't provide confidence scores by default
                    ))

        # Add phone numbers (regex + NLP context)
        for match in PHONE_RE.finditer(text):
            phone_text = match.group()
            # Skip if it's part of a timestamp or date
            if not self._is_timestamp_or_date(text, match.start(), match.end()):
                entities.append(PIIEntity(
                    text=phone_text,
                    label="PHONE",
                    start=match.start(),
                    end=match.end(),
                    confidence=0.9,
                ))

        # Add emails
        for match in EMAIL_RE.finditer(text):
            entities.append(PIIEntity(
                text=match.group(),
                label="EMAIL",
                start=match.start(),
                end=match.end(),
                confidence=1.0,
            ))

        return entities

    def is_phone_number(self, text: str, candidate: str) -> bool:
        """Check if a candidate string is a phone number in context.

        Args:
            text: The full text containing the candidate
            candidate: The potential phone number to check

        Returns:
            True if candidate is likely a phone number
        """
        # Quick rejection: timestamps (contain 'T')
        if "T" in candidate:
            return False

        # Quick rejection: dates in ISO format (YYYY-MM-DD with nothing else)
        if re.match(r'^\d{4}-\d{2}-\d{2}$', candidate.strip()):
            return False

        # Check if it matches phone pattern
        if not PHONE_RE.search(candidate):
            return False

        # Count digits
        digits = re.sub(r"\D", "", candidate)
        digit_count = len(digits)

        # Must have at least 10 digits to be a phone number
        if digit_count < 10:
            return False

        # Phone-related context words
        phone_contexts = {"call", "phone", "contact", "reach", "number", "mobile", "cell"}

        # Use NLP context if available
        if self.spacy_available and self.nlp:
            text_lower = text.lower()

            # Check if any phone context words appear near the candidate
            candidate_pos = text.find(candidate)
            if candidate_pos >= 0:
                # Check 50 characters before and after
                context_start = max(0, candidate_pos - 50)
                context_end = min(len(text), candidate_pos + len(candidate) + 50)
                context = text[context_start:context_end].lower()

                if any(word in context for word in phone_contexts):
                    return True

        # Fallback heuristics when NLP unavailable

        # Starts with + (international)
        if candidate.strip().startswith('+'):
            return True

        # Has parentheses (area code style)
        if '(' in candidate and ')' in candidate:
            return True

        # Has 10 digits with formatting (US/Canada style)
        if digit_count == 10 and any(c in candidate for c in ['-', ' ', '.', '(', ')']):
            return True

        # Has 11+ digits (international without +)
        if digit_count >= 11:
            return True

        # Has exactly 10 digits and appears near phone context words (manual check)
        if digit_count == 10:
            text_lower = text.lower()
            candidate_pos = text.lower().find(candidate.lower())
            if candidate_pos >= 0:
                context_start = max(0, candidate_pos - 50)
                context_end = min(len(text), candidate_pos + len(candidate) + 50)
                context = text_lower[context_start:context_end]
                if any(word in context for word in phone_contexts):
                    return True

        return False

    def is_person_name(self, text: str, candidate: str) -> bool:
        """Check if a candidate string is a person's name.

        Args:
            text: The full text containing the candidate
            candidate: The potential name to check

        Returns:
            True if candidate is likely a person's name
        """
        if not self.spacy_available or not self.nlp:
            # Fallback: Simple heuristic
            # Names are typically 2-4 words, each capitalized
            words = candidate.split()
            if len(words) < 1 or len(words) > 4:
                return False
            return all(word[0].isupper() for word in words if word)

        # Use spaCy NER
        doc = self.nlp(text)
        for ent in doc.ents:
            if ent.label_ == "PERSON" and candidate in ent.text:
                return True

        return False

    def _is_timestamp_or_date(self, text: str, start: int, end: int) -> bool:
        """Check if a position in text is part of a timestamp or date.

        Args:
            text: The full text
            start: Start position of the potential match
            end: End position of the potential match

        Returns:
            True if the match is part of a timestamp or date
        """
        # Extract a window around the match
        window_start = max(0, start - 10)
        window_end = min(len(text), end + 10)
        window = text[window_start:window_end]

        # Check for ISO timestamp pattern
        if ISO_TIMESTAMP_RE.search(window):
            return True

        # Check for date pattern
        if DATE_RE.search(window):
            # Make sure it's not just a coincidence
            # Dates have specific structure: YYYY-MM-DD
            match_text = text[start:end]
            if "-" in match_text and match_text.count("-") == 2:
                parts = match_text.split("-")
                if len(parts) == 3 and all(p.isdigit() for p in parts):
                    return True

        return False

    def redact_pii(self, text: str, allow: Optional[Set[str]] = None) -> str:
        """Redact PII from text, except for allowed values.

        Args:
            text: The text to redact
            allow: Set of strings that should not be redacted

        Returns:
            Text with PII redacted as [REDACTED]
        """
        if allow is None:
            allow = set()

        entities = self.extract_pii(text)

        # Sort by position (reverse) to maintain offsets during replacement
        entities.sort(key=lambda e: e.start, reverse=True)

        result = text
        for entity in entities:
            # Skip if this value is explicitly allowed
            if entity.text in allow:
                continue

            # Skip low-confidence detections for certain types
            if entity.confidence < 0.7 and entity.label in ["PHONE"]:
                continue

            # Replace with [REDACTED]
            result = result[:entity.start] + "[REDACTED]" + result[entity.end:]

        return result


# Singleton instance for efficient reuse
_detector: Optional[NLPDetector] = None


def get_nlp_detector() -> NLPDetector:
    """Get the singleton NLP detector instance."""
    global _detector
    if _detector is None:
        _detector = NLPDetector()
    return _detector


def is_phone_number_nlp(text: str, candidate: str) -> bool:
    """Check if a candidate is a phone number using NLP context.

    This is a convenience function that uses the singleton detector.

    Args:
        text: The full text containing the candidate
        candidate: The potential phone number

    Returns:
        True if candidate is likely a phone number
    """
    detector = get_nlp_detector()
    return detector.is_phone_number(text, candidate)


def extract_pii_nlp(text: str) -> List[PIIEntity]:
    """Extract PII entities using NLP.

    This is a convenience function that uses the singleton detector.

    Args:
        text: The text to analyze

    Returns:
        List of detected PII entities
    """
    detector = get_nlp_detector()
    return detector.extract_pii(text)
