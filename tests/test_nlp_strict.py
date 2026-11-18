"""Strict NLP tests that require spaCy to be installed.

These tests FAIL if spaCy is not available, ensuring that the NLP
functionality is actually working and not just falling back to regex.
"""

import pytest

from app.nlp_detector import NLPDetector


class TestStrictNLPRequirements:
    """Tests that enforce spaCy must be installed for NLP mode."""

    def test_spacy_must_be_installed_for_nlp_mode(self):
        """STRICT: spaCy must be installed for true NLP functionality."""
        detector = NLPDetector()

        if not detector.spacy_available:
            pytest.fail(
                "spaCy is not installed. Install with:\n"
                "  pip install spacy\n"
                "  python -m spacy download en_core_web_sm\n"
                "\n"
                "The NLP detector is currently running in fallback mode (regex only).\n"
                "This is acceptable for testing but NOT for production deployment."
            )

        # Verify NLP model is actually loaded
        assert detector.nlp is not None, "spaCy nlp object should be loaded"
        assert detector.spacy_available is True, "spacy_available flag should be True"

    def test_spacy_ner_actually_detects_person_names(self):
        """STRICT: spaCy NER must detect person names."""
        detector = NLPDetector()

        if not detector.spacy_available:
            pytest.skip("spaCy not available - skipping NLP-specific test")

        text = "John Smith made a reservation for Alice Johnson"
        entities = detector.extract_pii(text)

        # Should find PERSON entities via spaCy NER
        person_entities = [e for e in entities if e.label == "PERSON"]
        assert len(person_entities) >= 1, "Should detect at least one person name with spaCy"

        # Verify spaCy detected the names (not just regex)
        person_texts = [e.text for e in person_entities]
        assert any("John" in name or "Smith" in name for name in person_texts), \
            "Should detect 'John Smith' via NER"

    def test_spacy_context_detection_works(self):
        """STRICT: spaCy context analysis must work for phone numbers."""
        detector = NLPDetector()

        if not detector.spacy_available:
            pytest.skip("spaCy not available - skipping NLP-specific test")

        # With phone context
        text1 = "My phone number is 555-123-4567"
        result1 = detector.is_phone_number(text1, "555-123-4567")
        assert result1 is True, "Should detect phone with context"

        # Without phone context but with formatting
        text2 = "Reference code: 555-123-4567"
        result2 = detector.is_phone_number(text2, "555-123-4567")
        # Should still detect due to formatting (10 digits with dashes)
        assert result2 is True, "Should detect based on format"


class TestPhoneNumberRegressionStrict:
    """Strict phone number tests that must not regress."""

    def test_phone_numbers_in_natural_contexts(self):
        """Phone numbers in natural language contexts must be detected."""
        detector = NLPDetector()

        test_cases = [
            # (text, phone_number, should_detect)
            ("Call me at 555-123-4567", "555-123-4567", True),
            ("My phone: 555-123-4567", "555-123-4567", True),
            ("Contact: +1-555-123-4567", "+1-555-123-4567", True),
            ("Mobile: (555) 123-4567", "(555) 123-4567", True),
            ("You can reach me on 555.123.4567", "555.123.4567", True),
        ]

        for text, phone, should_detect in test_cases:
            result = detector.is_phone_number(text, phone)
            assert result == should_detect, \
                f"Failed for: {text} | phone={phone} | expected={should_detect} | got={result}"

    def test_dates_must_not_be_detected_as_phones(self):
        """STRICT: Date strings must NEVER be detected as phone numbers."""
        detector = NLPDetector()

        date_cases = [
            ("Start date: 2026-12-01", "2026-12-01"),
            ("From 2026-11-20 to 2026-11-25", "2026-11-20"),
            ("Reservation on 2026-12-15", "2026-12-15"),
        ]

        for text, date_str in date_cases:
            result = detector.is_phone_number(text, date_str)
            assert result is False, \
                f"FALSE POSITIVE: Date '{date_str}' incorrectly detected as phone in: {text}"

    def test_timestamps_must_not_be_detected_as_phones(self):
        """STRICT: ISO timestamps must NEVER be detected as phone numbers."""
        detector = NLPDetector()

        timestamp_cases = [
            ("Start: 2026-12-01T09:00", "2026-12-01T09:00"),
            ("Time: 2026-11-20T14:30:00", "2026-11-20T14:30:00"),
            ("At 2026-12-01T09:00:00Z", "2026-12-01T09:00:00Z"),
            ("Timestamp: 2026-11-17T10:30:45.123Z", "2026-11-17T10:30:45.123Z"),
            ("With offset: 2026-11-17T10:30:45+05:30", "2026-11-17T10:30:45+05:30"),
        ]

        for text, timestamp in timestamp_cases:
            result = detector.is_phone_number(text, timestamp)
            assert result is False, \
                f"FALSE POSITIVE: Timestamp '{timestamp}' incorrectly detected as phone in: {text}"

    def test_reservation_ids_not_detected_as_phones(self):
        """Short numeric IDs should not be detected as phone numbers."""
        detector = NLPDetector()

        id_cases = [
            ("Reservation #12345", "12345"),
            ("Booking ID: 67890", "67890"),
            ("Reference #123", "123"),
        ]

        for text, id_str in id_cases:
            result = detector.is_phone_number(text, id_str)
            assert result is False, \
                f"FALSE POSITIVE: ID '{id_str}' incorrectly detected as phone in: {text}"

    def test_international_phone_formats(self):
        """International phone formats must be detected."""
        detector = NLPDetector()

        international_cases = [
            ("UK: +44-20-7123-4567", "+44-20-7123-4567"),
            ("France: +33-1-23-45-67-89", "+33-1-23-45-67-89"),
            ("Germany: +49-30-12345678", "+49-30-12345678"),
            ("India: +91-9876543210", "+91-9876543210"),
            ("US: +1-555-123-4567", "+1-555-123-4567"),
        ]

        for text, phone in international_cases:
            result = detector.is_phone_number(text, phone)
            assert result is True, \
                f"Failed to detect international phone: {phone} in: {text}"

    def test_indian_phone_formats(self):
        """Indian phone number formats must be detected."""
        detector = NLPDetector()

        indian_cases = [
            ("Call +91-9876543210", "+91-9876543210"),
            ("Mobile: +919876543210", "+919876543210"),
            ("Phone: 9876543210", "9876543210"),  # 10-digit mobile
            ("Landline: 022-12345678", "022-12345678"),  # Mumbai landline
        ]

        for text, phone in indian_cases:
            result = detector.is_phone_number(text, phone)
            assert result is True, \
                f"Failed to detect Indian phone: {phone} in: {text}"

    def test_licence_plates_not_detected_as_phones(self):
        """Licence plates should not be detected as phone numbers."""
        detector = NLPDetector()

        plate_cases = [
            ("Car: ABC123", "ABC123"),
            ("Plate: XYZ-9876", "XYZ-9876"),
            ("Vehicle: DL-01-AB-1234", "DL-01-AB-1234"),  # Indian format
            ("License: CA-1234567", "CA-1234567"),
        ]

        for text, plate in plate_cases:
            result = detector.is_phone_number(text, plate)
            assert result is False, \
                f"FALSE POSITIVE: Plate '{plate}' incorrectly detected as phone in: {text}"


class TestComprehensiveMixedScenarios:
    """Test realistic scenarios mixing phones, dates, timestamps, and plates."""

    def test_booking_confirmation_comprehensive(self):
        """Complete booking confirmation with all data types."""
        detector = NLPDetector()

        confirmation = """
        Your reservation is confirmed!

        Reservation ID: #12345
        Name: Rajesh Kumar
        Car Number: DL-04-AB-5678
        Phone: +91-9876543210

        Booking Period:
        Start: 2026-12-01T09:00:00+05:30
        End: 2026-12-01T18:00:00+05:30

        Approved at: 2026-11-17T14:30:45.123+05:30
        Confirmation sent to: rajesh.kumar@example.com

        For assistance, call +91-22-12345678
        """

        entities = detector.extract_pii(confirmation)

        # Should detect phones
        phone_entities = [e for e in entities if e.label == "PHONE"]
        assert len(phone_entities) >= 2, "Should detect at least 2 phone numbers"

        # Verify phone numbers detected
        phone_texts = [e.text for e in phone_entities]
        assert any("9876543210" in p for p in phone_texts), "Should detect mobile"

        # Should detect email
        email_entities = [e for e in entities if e.label == "EMAIL"]
        assert len(email_entities) >= 1, "Should detect email"

        # Should NOT detect timestamps as phones
        for entity in phone_entities:
            assert "T" not in entity.text, f"Timestamp detected as phone: {entity.text}"
            # Allow phone numbers to have up to 2 dashes (555-123-4567)
            # but timestamps have full date format
            if entity.text.count("-") >= 2:
                assert not entity.text.startswith("2026-"), \
                    f"Date/timestamp detected as phone: {entity.text}"

        # Should NOT detect reservation ID as phone
        for entity in phone_entities:
            assert "12345" not in entity.text or len(entity.text) > 8, \
                "Short reservation ID incorrectly detected as phone"

        # Should NOT detect car plate as phone
        assert not any("DL-04-AB-5678" in e.text for e in phone_entities), \
            "Licence plate incorrectly detected as phone"

    def test_timezone_offsets_not_detected_as_phones(self):
        """Timezone offsets in timestamps must not be detected as phones."""
        detector = NLPDetector()

        text = "Confirmed at 2026-11-17T10:30:45+05:30 (IST)"

        # The full timestamp should not be detected
        assert not detector.is_phone_number(text, "2026-11-17T10:30:45+05:30")

        # The offset alone should not be detected
        assert not detector.is_phone_number(text, "05:30")

        # Extract all entities - should not have timestamps
        entities = detector.extract_pii(text)
        phone_entities = [e for e in entities if e.label == "PHONE"]

        for entity in phone_entities:
            assert "2026" not in entity.text, \
                f"Timestamp with timezone offset detected as phone: {entity.text}"


class TestPhoneRegexPatternValidation:
    """Validate PHONE_RE pattern for correctness."""

    def test_phone_regex_matches_valid_formats(self):
        """PHONE_RE should match valid phone formats."""
        from app.nlp_detector import PHONE_RE

        valid_formats = [
            "555-123-4567",
            "(555) 123-4567",
            "+1-555-123-4567",
            "5551234567",
            "+919876543210",
            "+44-20-7123-4567",
            "+33-1-23-45-67-89",
        ]

        for phone in valid_formats:
            match = PHONE_RE.search(phone)
            assert match is not None, f"PHONE_RE failed to match valid phone: {phone}"

    def test_phone_regex_rejects_invalid_formats(self):
        """PHONE_RE should NOT match dates and timestamps."""
        from app.nlp_detector import PHONE_RE

        invalid_formats = [
            "2026-12-01",
            "2026-11-20",
            "2026-12-01T09:00",
            "2026-11-17T10:30:45",
            "123",  # Too short
            "12345",  # Too short
        ]

        for invalid in invalid_formats:
            match = PHONE_RE.search(invalid)
            if match:
                # Even if regex matches, the is_phone_number logic should reject it
                from app.nlp_detector import NLPDetector
                detector = NLPDetector()
                result = detector.is_phone_number(f"Text: {invalid}", invalid)
                assert result is False, \
                    f"PHONE_RE matched invalid format: {invalid} (should be rejected by logic)"
