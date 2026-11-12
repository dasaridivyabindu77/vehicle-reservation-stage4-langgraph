"""Loader for the static knowledge base."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from langchain_core.documents import Document

_DATA_PATH = Path(__file__).parent / "data" / "static_knowledge.json"


def load_static_documents(path: Path | None = None) -> List[Document]:
    """Load static knowledge entries as LangChain documents."""
    source = path or _DATA_PATH
    records = json.loads(source.read_text(encoding="utf-8"))
    docs: List[Document] = []
    for rec in records:
        docs.append(
            Document(
                page_content=rec["text"],
                metadata={
                    "doc_id": rec["doc_id"],
                    "title": rec.get("title", ""),
                    "category": rec.get("category", ""),
                },
            )
        )
    return docs
