"""Qdrant vector store: the same chunks and embeddings as pgvector, for the dev-set comparison.

Usage: python -m retrieval.qdrant_store   (after python -m retrieval.ingest)
"""

import asyncio
import threading

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from agent.config import EMBED_DIM, EMBED_MODEL, qdrant_url
from retrieval.ingest import embedder, load_chunks
from retrieval.search import Hit, top_docs

COLLECTION = "policy_chunks"


def ingest_qdrant(url: str) -> int:
    chunks = load_chunks()
    vectors = embedder().get_text_embedding_batch([f"{c['title']}\n{c['text']}" for c in chunks])
    client = QdrantClient(url=url)
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE)
    )
    client.upsert(
        COLLECTION,
        points=[
            PointStruct(id=i, vector=v, payload=c)
            for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
        ],
    )
    return len(chunks)


class QdrantRetriever:
    """Vector-only search in Qdrant; same interface as HybridRetriever."""

    mode = "qdrant"

    def __init__(self, url: str, k: int = 5, candidates: int = 20):
        self.client = QdrantClient(url=url)
        self.k = k
        self.candidates = candidates
        self._embed = None
        self._lock = threading.Lock()

    def embed(self, text: str):
        with self._lock:
            if self._embed is None:
                from fastembed import TextEmbedding

                self._embed = TextEmbedding(model_name=EMBED_MODEL)
            return next(iter(self._embed.query_embed(text)))

    def search_sync(self, query: str) -> list[Hit]:
        points = self.client.query_points(
            COLLECTION, query=self.embed(query).tolist(), limit=self.candidates
        ).points
        hits = [
            Hit(
                p.payload["id"], p.payload["doc_id"], p.payload["title"], p.payload["text"], p.score
            )
            for p in points
        ]
        return top_docs(hits, self.k)

    async def search(self, query: str) -> list[Hit]:
        return await asyncio.to_thread(self.search_sync, query)


if __name__ == "__main__":
    load_dotenv(".env")
    print(f"{ingest_qdrant(qdrant_url())} chunks written to Qdrant")
