"""Tests for NLP-based PII detection.

These tests verify that the NLP detector correctly identifies and redacts
personally identifiable information using context-aware analysis.
"""

import re

import pytest

from app.nlp_detector import (
    NLPDetector,
    get_nlp_detector,
    is_phone_number_nlp,
    extract_pii_nlp,
)


class TestNLPDetector:
    """Tests for NLPDetector class."""

    def test_detector_initializes(self):
        """NLP detector should initialize without errors."""
        detector = NLPDetector()
        assert detector is not None
        # spaCy may or may not be available, both are valid

    def test_phone_number_detection_in_context(self):
        """Phone numbers should be detected when in phone-related context."""
        detector = NLPDetector()

        # Clear phone context
        text1 = "Call me at 555-123-4567 about the booking"
        assert detector.is_phone_number(text1, "555-123-4567") is True

        text2 = "My phone number is (555) 123-4567"
        assert detector.is_phone_number(text2, "(555) 123-4567") is True

        text3 = "Contact: +1-555-123-4567"
        assert detector.is_phone_number(text3, "+1-555-123-4567") is True

    def test_timestamps_not_detected_as_phone_numbers(self):
        """Timestamps should not be incorrectly flagged as phone numbers."""
        detector = NLPDetector()

        # ISO timestamps
        text1 = "Booking from 2026-12-01T09:00 to 2026-12-01T18:00"
        assert detector.is_phone_number(text1, "2026-12-01T09:00") is False

        # Dates
        text2 = "Start date: 2026-12-01"
        assert detector.is_phone_number(text2, "2026-12-01") is False

    def test_dates_not_detected_as_phone_numbers(self):
        """Date strings should not be flagged as phone numbers."""
        detector = NLPDetector()

        text = "Reservation for 2026-11-20"
        assert detector.is_phone_number(text, "2026-11-20") is False

    def test_reservation_ids_not_detected_as_phone_numbers(self):
        """Short reservation IDs should not be flagged as phone numbers."""
        detector = NLPDetector()

        text = "Your reservation #12345 is confirmed"
        # Short IDs don't have enough digits to be phones
        assert detector.is_phone_number(text, "12345") is False

    def test_extract_pii_finds_emails(self):
        """Email addresses should be extracted as PII."""
        detector = NLPDetector()

        text = "Contact john.doe@example.com for details"
        entities = detector.extract_pii(text)

        # Should find at least the email
        email_entities = [e for e in entities if e.label == "EMAIL"]
        assert len(email_entities) >= 1
        assert "john.doe@example.com" in [e.text for e in email_entities]

    def test_extract_pii_finds_phone_numbers(self):
        """Phone numbers should be extracted as PII."""
        detector = NLPDetector()

        text = "Call 555-123-4567 for more information"
        entities = detector.extract_pii(text)

        # Should find the phone number
        phone_entities = [e for e in entities if e.label == "PHONE"]
        assert len(phone_entities) >= 1

    def test_extract_pii_skips_timestamps(self):
        """Timestamps should not be extracted as phone numbers."""
        detector = NLPDetector()

        text = "Booking from 2026-12-01T09:00:00 to 2026-12-01T18:00:00"
        entities = detector.extract_pii(text)

        # Should not detect timestamps as phone numbers
        phone_entities = [e for e in entities if e.label == "PHONE"]
        timestamp_phones = [e for e in phone_entities if "T" in e.text or e.text.count("-") >= 2]
        assert len(timestamp_phones) == 0

    def test_redact_pii_basic(self):
        """PII should be redacted from text."""
        detector = NLPDetector()

        text = "Email john@example.com or call 555-123-4567"
        redacted = detector.redact_pii(text)

        assert "john@example.com" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_pii_preserves_allowed(self):
        """Allowed values should not be redacted."""
        detector = NLPDetector()

        text = "Your car number ABC123 and phone 555-123-4567"
        allow = {"ABC123"}
        redacted = detector.redact_pii(text, allow=allow)

        # ABC123 should be preserved
        assert "ABC123" in redacted

    def test_redact_pii_preserves_timestamps(self):
        """Timestamps should not be redacted."""
        detector = NLPDetector()

        text = "Booking from 2026-12-01T09:00 to 2026-12-01T18:00"
        redacted = detector.redact_pii(text)

        # Timestamps should be preserved
        assert "2026-12-01T09:00" in redacted
        assert "2026-12-01T18:00" in redacted

    def test_person_name_detection_if_spacy_available(self):
        """Person names should be detected if spaCy is available."""
        detector = NLPDetector()

        if not detector.spacy_available:
            pytest.skip("spaCy not available")

        text = "John Smith made a reservation"
        is_name = detector.is_person_name(text, "John Smith")

        # spaCy should recognize this as a person name
        assert is_name is True

    def test_person_name_detection_fallback(self):
        """Person name detection should have a fallback heuristic."""
        detector = NLPDetector()

        # Even without spaCy, should have basic heuristic
        text = "John Smith made a reservation"
        is_name = detector.is_person_name(text, "John Smith")

        # Should return either True or False (not error)
        assert isinstance(is_name, bool)


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_nlp_detector_singleton(self):
        """get_nlp_detector should return the same instance."""
        detector1 = get_nlp_detector()
        detector2 = get_nlp_detector()

        assert detector1 is detector2

    def test_is_phone_number_nlp_function(self):
        """is_phone_number_nlp convenience function should work."""
        text = "Call 555-123-4567"
        result = is_phone_number_nlp(text, "555-123-4567")

        assert isinstance(result, bool)
        assert result is True

    def test_extract_pii_nlp_function(self):
        """extract_pii_nlp convenience function should work."""
        text = "Contact john@example.com"
        entities = extract_pii_nlp(text)

        assert isinstance(entities, list)
        assert len(entities) > 0


class TestNLPIntegrationWithGuardrails:
    """Tests for NLP integration with existing guardrails."""

    def test_phone_detection_with_context(self):
        """Phone numbers in natural context should be detected."""
        detector = NLPDetector()

        # Natural contexts where phone numbers appear
        contexts = [
            "My phone number is 555-123-4567",
            "Call me at 555-123-4567",
            "Contact: 555-123-4567",
            "You can reach me on 555-123-4567",
            "Mobile: 555-123-4567",
        ]

        for context in contexts:
            assert detector.is_phone_number(context, "555-123-4567") is True, \
                f"Failed to detect phone in: {context}"

    def test_no_false_positives_for_dates(self):
        """Date-like sequences should not be detected as phone numbers."""
        detector = NLPDetector()

        date_contexts = [
            ("Start: 2026-12-01", "2026-12-01"),
            ("Date: 2026-11-20", "2026-11-20"),
            ("From 2026-12-01 to 2026-12-02", "2026-12-01"),
        ]

        for context, candidate in date_contexts:
            assert detector.is_phone_number(context, candidate) is False, \
                f"False positive for date in: {context}"

    def test_no_false_positives_for_timestamps(self):
        """Timestamp sequences should not be detected as phone numbers."""
        detector = NLPDetector()

        timestamp_contexts = [
            ("Start: 2026-12-01T09:00", "2026-12-01T09:00"),
            ("Time: 2026-11-20T14:30:00", "2026-11-20T14:30:00"),
            ("At 2026-12-01T09:00:00Z", "2026-12-01T09:00:00Z"),
        ]

        for context, candidate in timestamp_contexts:
            assert detector.is_phone_number(context, candidate) is False, \
                f"False positive for timestamp in: {context}"

    def test_graceful_fallback_without_spacy(self):
        """System should work even without spaCy installed."""
        # Create a detector and verify it doesn't crash
        detector = NLPDetector()

        # Should be able to detect phone numbers with regex fallback
        text = "Call 555-123-4567"
        result = detector.is_phone_number(text, "555-123-4567")

        assert isinstance(result, bool)
        # Should still work with heuristics even if spaCy unavailable
        assert result is True

    def test_booking_confirmation_not_corrupted(self):
        """Booking confirmation with dates and phones should be processed correctly."""
        detector = NLPDetector()

        confirmation = """
        Your reservation is confirmed!

        Details:
        - Name: John Doe
        - Car: ABC123
        - Start: 2026-12-01T09:00
        - End: 2026-12-01T18:00
        - Phone: 555-123-4567
        - Reservation ID: #12345
        """

        entities = detector.extract_pii(confirmation)

        # Should find phone but not timestamps
        phone_entities = [e for e in entities if e.label == "PHONE"]
        assert len(phone_entities) >= 1

        # Verify timestamps were not detected as phones
        for entity in phone_entities:
            assert "T" not in entity.text
            # Phone numbers can have dashes (555-123-4567 has 2)
            # We just want to ensure full ISO timestamps aren't detected
            if entity.text.count("-") >= 2:
                # If it has 2+ dashes, make sure it's not a date-like pattern
                assert not re.match(r'^\d{4}-\d{2}-\d{2}', entity.text)


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_text(self):
        """Empty text should be handled gracefully."""
        detector = NLPDetector()

        assert detector.extract_pii("") == []
        assert detector.redact_pii("") == ""

    def test_text_with_no_pii(self):
        """Text without PII should pass through unchanged."""
        detector = NLPDetector()

        text = "I want to book a parking spot"
        redacted = detector.redact_pii(text)

        assert redacted == text

    def test_multiple_phone_numbers(self):
        """Multiple phone numbers should all be detected."""
        detector = NLPDetector()

        text = "Call 555-123-4567 or 555-987-6543"
        entities = detector.extract_pii(text)

        phone_entities = [e for e in entities if e.label == "PHONE"]
        assert len(phone_entities) >= 2

    def test_international_phone_formats(self):
        """International phone formats should be detected."""
        detector = NLPDetector()

        international_phones = [
            ("UK: +44-20-7123-4567", "+44-20-7123-4567"),
            ("France: +33-1-23-45-67-89", "+33-1-23-45-67-89"),
            ("Germany: +49-30-12345678", "+49-30-12345678"),
        ]

        for context, phone in international_phones:
            result = detector.is_phone_number(context, phone)
            assert result is True, f"Failed to detect: {phone}"
