"""Hybrid search over policy chunks: pgvector + Postgres full-text, fused with RRF, then reranked.

Modes (for the ablations in Phase 3): vector, fulltext, hybrid, hybrid_rerank (default).
"""

import asyncio
import re
from dataclasses import dataclass

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from agent.config import EMBED_MODEL, RERANK_MODEL

MODES = ("vector", "fulltext", "hybrid", "hybrid_rerank")


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float


def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: score(id) = sum over lists of 1 / (k + rank)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def or_query(text: str) -> str:
    """Turn free text into an OR tsquery ('lost | package'), so any matching word counts."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " | ".join(dict.fromkeys(words))


def top_docs(hits: list[Hit], k: int) -> list[Hit]:
    """Best chunk per document, first k documents."""
    seen, out = set(), []
    for hit in hits:
        if hit.doc_id not in seen:
            seen.add(hit.doc_id)
            out.append(hit)
    return out[:k]


class HybridRetriever:
    def __init__(
        self, conninfo: str, mode: str = "hybrid_rerank", k: int = 5, candidates: int = 20
    ):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        self.conninfo = conninfo
        self.mode = mode
        self.k = k
        self.candidates = candidates
        self._embed = None
        self._rerank = None

    def embed(self, text: str) -> np.ndarray:
        if self._embed is None:
            from fastembed import TextEmbedding

            self._embed = TextEmbedding(model_name=EMBED_MODEL)
        return next(iter(self._embed.query_embed(text)))

    def rerank_scores(self, query: str, texts: list[str]) -> list[float]:
        if self._rerank is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._rerank = TextCrossEncoder(model_name=RERANK_MODEL)
        return list(self._rerank.rerank(query, texts))

    def _vector(self, conn, query: str) -> list[Hit]:
        vector = self.embed(query)
        rows = conn.execute(
            "SELECT id, doc_id, title, text, 1 - (embedding <=> %s) FROM policy_chunks "
            "ORDER BY embedding <=> %s LIMIT %s",
            (vector, vector, self.candidates),
        ).fetchall()
        return [Hit(*row) for row in rows]

    def _fulltext(self, conn, query: str) -> list[Hit]:
        tsquery = or_query(query)
        if not tsquery:
            return []
        rows = conn.execute(
            "SELECT id, doc_id, title, text, ts_rank_cd(tsv, q) AS rank "
            "FROM policy_chunks, to_tsquery('english', %s) q WHERE tsv @@ q "
            "ORDER BY rank DESC, id LIMIT %s",
            (tsquery, self.candidates),
        ).fetchall()
        return [Hit(*row) for row in rows]

    def search_sync(self, query: str) -> list[Hit]:
        with psycopg.connect(self.conninfo) as conn:
            register_vector(conn)
            if self.mode == "vector":
                return top_docs(self._vector(conn, query), self.k)
            if self.mode == "fulltext":
                return top_docs(self._fulltext(conn, query), self.k)
            vector_hits, text_hits = self._vector(conn, query), self._fulltext(conn, query)

        by_id = {h.chunk_id: h for h in vector_hits + text_hits}
        fused = rrf([[h.chunk_id for h in vector_hits], [h.chunk_id for h in text_hits]])
        hits = [Hit(**{**by_id[cid].__dict__, "score": score}) for cid, score in fused]
        if self.mode == "hybrid":
            return top_docs(hits, self.k)

        hits = hits[: self.candidates]
        scores = self.rerank_scores(query, [f"{h.title}\n{h.text}" for h in hits])
        for hit, score in zip(hits, scores, strict=True):
            hit.score = float(score)
        return top_docs(sorted(hits, key=lambda h: -h.score), self.k)

    async def search(self, query: str) -> list[Hit]:
        # Embedding and reranking are CPU-bound; keep them off the event loop.
        return await asyncio.to_thread(self.search_sync, query)
