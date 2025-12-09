from __future__ import annotations

from typing import Iterable, List, Optional

import chromadb
from chromadb.utils import embedding_functions


class VectorStoreConnector:
    """
    Lightweight Chroma wrapper for storing/retrieving documents.
    Uses Chroma's default embedding unless a custom function is provided.
    """

    def __init__(self, persist_dir: Optional[str] = None, collection: str = "markets", embedding_fn=None):
        client = chromadb.Client()
        self.collection = client.get_or_create_collection(
            name=collection,
            embedding_function=embedding_fn or embedding_functions.DefaultEmbeddingFunction(),
        )

    def add(self, ids: List[str], documents: List[str], metadatas: Optional[List[dict]] = None) -> None:
        self.collection.add(ids=ids, documents=documents, metadatas=metadatas)

    def similarity_search(self, query: str, k: int = 5) -> List[dict]:
        res = self.collection.query(query_texts=[query], n_results=k)
        # Normalize output
        results: List[dict] = []
        for i, doc in enumerate(res.get("documents", [[]])[0]):
            meta = res.get("metadatas", [[]])[0][i] if res.get("metadatas") else {}
            score = res.get("distances", [[]])[0][i] if res.get("distances") else None
            results.append({"document": doc, "metadata": meta, "score": score})
        return results
