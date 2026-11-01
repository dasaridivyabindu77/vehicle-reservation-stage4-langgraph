import hashlib
import json
from pathlib import Path

import chromadb
import numpy as np


class HashEmbeddingFunction:
    def is_legacy(self):
        return False

    def name(self):
        return "deterministic-token-hash-v1"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return HashEmbeddingFunction()

    def __call__(self, input):
        return [self.embed(text) for text in input]

    def embed(self, text):
        vector = np.zeros(384, dtype=np.float32)
        tokens = "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for offset in range(0, len(digest), 4):
                value = int.from_bytes(digest[offset:offset + 4], "little")
                vector[value % 384] += 1.0
        norm = np.linalg.norm(vector)
        if norm:
            vector /= norm
        return vector.tolist()


class StaticVectorStore:
    def __init__(self, persist_directory, knowledge_file):
        self.persist_directory = Path(persist_directory)
        self.persist_directory.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.persist_directory))
        self.embedding_function = HashEmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name="vehicle_static_knowledge",
            embedding_function=self.embedding_function,
            metadata={"hnsw:space": "cosine"},
        )
        self._load_documents(knowledge_file)

    def _load_documents(self, knowledge_file):
        with Path(knowledge_file).open("r", encoding="utf-8") as handle:
            documents = json.load(handle)
        ids = [item["id"] for item in documents]
        texts = [f'{item["title"]}. {item["text"]}' for item in documents]
        metadatas = [
            {"title": item["title"], "category": item["category"], "text": item["text"]}
            for item in documents
        ]
        existing_ids = set(self.collection.get(ids=ids)["ids"])
        missing = [index for index, doc_id in enumerate(ids) if doc_id not in existing_ids]
        if missing:
            self.collection.add(
                ids=[ids[index] for index in missing],
                documents=[texts[index] for index in missing],
                metadatas=[metadatas[index] for index in missing],
            )

    def search(self, query, top_k=3):
        result = self.collection.query(
            query_texts=[query],
            n_results=min(top_k, max(self.collection.count(), 1)),
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for index, doc_id in enumerate(result["ids"][0]):
            metadata = result["metadatas"][0][index]
            hits.append({
                "id": doc_id,
                "title": metadata["title"],
                "category": metadata["category"],
                "text": metadata["text"],
                "score": 1 - result["distances"][0][index],
                "document": result["documents"][0][index],
            })
        return hits
