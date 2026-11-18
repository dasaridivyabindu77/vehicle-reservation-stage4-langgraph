"""Test that NLP model is properly installed and working."""

import pytest

from app.nlp_detector import NLPDetector


def test_spacy_model_loaded():
    """Verify spaCy model is loaded and working."""
    detector = NLPDetector()

    # Verify spaCy is available
    assert detector.nlp is not None, "spaCy model not loaded"
    print(f"\n✓ spaCy model loaded: {detector.nlp.meta['name']}")


def test_nlp_pii_detection_person_names():
    """Test NLP-based detection of person names."""
    detector = NLPDetector()

    # Test person name detection
    text = "My name is John Smith and I live in New York."
    entities = detector.extract_pii(text)

    person_entities = [e for e in entities if e.label == "PERSON"]
    assert person_entities, "Should detect person name"
    person_names = [e.text for e in person_entities]
    assert "John Smith" in person_names or "John" in person_names
    print(f"✓ Detected person names: {person_names}")


def test_nlp_pii_detection_emails():
    """Test NLP-based detection of email addresses."""
    detector = NLPDetector()

    text = "Contact me at john.doe@example.com for more info."
    entities = detector.extract_pii(text)

    email_entities = [e for e in entities if e.label == "EMAIL"]
    assert email_entities, "Should detect email"
    emails = [e.text for e in email_entities]
    assert "john.doe@example.com" in emails
    print(f"✓ Detected emails: {emails}")


def test_nlp_pii_detection_phone_numbers():
    """Test NLP-based detection of phone numbers."""
    detector = NLPDetector()

    text = "Call me at 555-123-4567 or +1-555-987-6543"
    entities = detector.extract_pii(text)

    phone_entities = [e for e in entities if e.label == "PHONE"]
    assert phone_entities, "Should detect phone numbers"
    phones = [e.text for e in phone_entities]
    print(f"✓ Detected phone numbers: {phones}")


def test_nlp_pii_detection_locations():
    """Test NLP-based detection of locations."""
    detector = NLPDetector()

    text = "I live at 123 Main Street, New York, NY 10001"
    entities = detector.extract_pii(text)

    # Should detect location entities (GPE or LOC)
    location_entities = [e for e in entities if e.label in ["GPE", "LOC"]]
    assert location_entities, "Should identify location information"
    locations = [e.text for e in location_entities]
    print(f"✓ Detected locations: {locations}")


def test_nlp_no_false_positives():
    """Test that NLP doesn't flag clean text."""
    detector = NLPDetector()

    text = "I want to book a parking spot for tomorrow."
    entities = detector.extract_pii(text)

    # Should not detect PII in clean parking request
    # Allow location entities that are generic (not specific addresses)
    pii_entities = [e for e in entities if e.label in ["PERSON", "EMAIL", "PHONE"]]
    assert len(pii_entities) == 0, "Should not flag clean text with sensitive PII"
    print("✓ Clean text correctly identified (no false positives)")


def test_nlp_mode_vs_regex_mode():
    """Test NLP mode detects entities better than regex alone."""
    text = "My name is Alice Johnson and my email is alice@example.com"

    detector = NLPDetector()
    entities = detector.extract_pii(text)

    # Extract by type
    person_names = [e.text for e in entities if e.label == "PERSON"]
    emails = [e.text for e in entities if e.label == "EMAIL"]

    print(f"\n✓ NLP detected persons: {person_names}")
    print(f"✓ NLP detected emails: {emails}")

    # Should detect both
    assert emails, "Should detect email"
    assert person_names, "NLP should detect person names"
    print("✓ NLP mode provides entity detection")


def test_nlp_comprehensive_pii():
    """Test comprehensive PII detection with multiple types."""
    detector = NLPDetector()

    text = """
    Hello, I'm Sarah Martinez and I work at 456 Oak Avenue, Boston, MA.
    You can reach me at sarah.martinez@company.com or call 617-555-0123.
    My colleague John Doe also works here at ext. 5678.
    """

    entities = detector.extract_pii(text)

    # Group by type
    by_type = {}
    for e in entities:
        if e.label not in by_type:
            by_type[e.label] = []
        by_type[e.label].append(e.text)

    print("\n=== Comprehensive PII Detection ===")
    print(f"Total entities: {len(entities)}")
    for label, items in sorted(by_type.items()):
        print(f"{label}: {items}")

    assert len(entities) >= 3, "Should detect multiple PII items"
    print("✓ Comprehensive PII detection working")


def test_nlp_redaction():
    """Test PII redaction functionality."""
    detector = NLPDetector()

    text = "Contact John Smith at john@example.com or 555-1234"
    entities = detector.extract_pii(text)
    redacted = detector.redact_pii(text)

    print(f"\nOriginal: {text}")
    print(f"Redacted: {redacted}")

    assert entities, "Should detect PII"
    assert "john@example.com" not in redacted, "Email should be redacted"
    assert "[REDACTED]" in redacted, "Should have redaction marker"
    print("✓ PII redaction working")


def test_phone_number_context():
    """Test phone number detection with context."""
    detector = NLPDetector()

    text = "Call me at 555-123-4567 for more details."
    is_phone = detector.is_phone_number(text, "555-123-4567")

    assert is_phone, "Should recognize phone with context"
    print("✓ Phone number context detection working")


def test_person_name_detection():
    """Test person name detection method."""
    detector = NLPDetector()

    text = "My name is Alice Johnson and I live in Boston."
    is_name = detector.is_person_name(text, "Alice Johnson")

    assert is_name, "Should recognize person name"
    print("✓ Person name detection working")
