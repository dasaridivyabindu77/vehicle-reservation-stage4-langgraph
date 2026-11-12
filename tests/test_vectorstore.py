from langchain_core.documents import Document

from app.embeddings import HashingEmbeddings
from app.vectorstore import InMemoryKnowledgeStore, _weaviate_reachable


def _store():
    store = InMemoryKnowledgeStore(HashingEmbeddings(dim=256))
    store.add_documents(
        [
            Document(page_content="Zone C offers electric vehicle charging points.",
                     metadata={"doc_id": "ev"}),
            Document(page_content="The maximum vehicle height in the covered garage is 2.1 metres.",
                     metadata={"doc_id": "rules"}),
            Document(page_content="CityPark is located at 15 Market Street near Central Station.",
                     metadata={"doc_id": "location"}),
        ]
    )
    return store


def test_search_ranks_relevant_doc_first():
    store = _store()
    results = store.search("where can I charge my electric car", k=3)
    assert results
    assert results[0][0].metadata["doc_id"] == "ev"
    # scores are cosine similarities
    assert all(-1.0 <= score <= 1.0 for _, score in results)


def test_search_empty_store_returns_empty():
    store = InMemoryKnowledgeStore(HashingEmbeddings(dim=64))
    assert store.search("anything", k=4) == []


def test_weaviate_reachability_on_closed_port_is_false():
    # Nothing should be listening here; must fail fast, not hang.
    assert _weaviate_reachable("http://127.0.0.1:1", timeout=0.5) is False
