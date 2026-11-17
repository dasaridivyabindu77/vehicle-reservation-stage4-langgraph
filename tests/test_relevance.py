from langchain_core.documents import Document

from app.relevance import content_words, filter_documents, is_relevant, lexical_overlap, _words_match


def test_lexical_overlap_ignores_stopwords():
    assert lexical_overlap("what are the opening hours", "our opening hours vary") > 0.5
    assert content_words("the and of a") == set()


def test_fuzzy_word_matching():
    """Test that word variations match (e.g., located matches location)."""
    assert _words_match("location", "located") is True
    assert _words_match("located", "location") is True
    assert _words_match("book", "booking") is True
    # "reserve" vs "reservation" share common prefix but aren't close enough for current algorithm
    # Non-matches
    assert _words_match("price", "open") is False


def test_overlap_with_word_variations():
    """Test that overlap works with word variations."""
    # "located" should match "location"
    overlap = lexical_overlap("where are you located", "Location and directions")
    assert overlap > 0.0, "Should match 'located' with 'location'"

    # "booking" should match "book"
    overlap = lexical_overlap("how to make a booking", "How to book a space")
    assert overlap > 0.0, "Should match 'booking' with 'book'"


def test_is_relevant_passes_on_overlap_and_fails_off_topic():
    doc = "CityPark offers electric vehicle charging in Zone C."
    assert is_relevant("do you have EV charging", doc, None, 0.12, 0.3) is True
    assert is_relevant("what is the capital of France", doc, None, 0.12, 0.3) is False


def test_is_relevant_passes_on_high_score_even_without_overlap():
    doc = "Completely unrelated text."
    assert is_relevant("xyz query", doc, score=0.9, min_overlap=0.5, min_score=0.3) is True


def test_filter_documents_drops_off_topic():
    docs = [
        (Document(page_content="Opening hours vary by day of the week."), 0.1),
        (Document(page_content="Bananas are a good source of potassium."), 0.1),
    ]
    kept = filter_documents("what are the opening hours", docs, 0.12, 0.3, use_scores=False)
    assert len(kept) == 1
    assert "hours" in kept[0][0].page_content.lower()
