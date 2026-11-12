import math

from app.embeddings import HashingEmbeddings


def test_dimension_and_determinism():
    emb = HashingEmbeddings(dim=128)
    v1 = emb.embed_query("covered garage in zone a")
    v2 = emb.embed_query("covered garage in zone a")
    assert len(v1) == 128
    assert v1 == v2  # deterministic


def test_unit_normalised():
    emb = HashingEmbeddings(dim=64)
    v = emb.embed_query("electric vehicle charging")
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-9


def test_embed_documents_matches_query():
    emb = HashingEmbeddings(dim=64)
    [doc_vec] = emb.embed_documents(["opening hours"])
    assert doc_vec == emb.embed_query("opening hours")
