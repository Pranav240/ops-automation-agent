"""Retrieval helpers. The database test runs only when Postgres is reachable and ingested."""

import os

import psycopg
import pytest
from dotenv import load_dotenv

from retrieval.ingest import load_chunks
from retrieval.search import Hit, HybridRetriever, or_query, rrf, top_docs


def test_rrf_rewards_items_ranked_high_in_both_lists():
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]], k=60)
    assert [item for item, _ in fused][:2] in (["a", "b"], ["b", "a"])
    assert dict(fused)["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert [item for item, _ in fused][-2:] == ["c", "d"]


def test_or_query_keeps_words_only():
    assert or_query("Where's my ORD-1001?! where") == "where | s | my | ord | 1001"
    assert or_query("!!!") == ""


def test_top_docs_keeps_best_chunk_per_doc():
    hits = [Hit(f"{d}#{i}", d, "t", "x", 1.0) for d, i in [("A", 0), ("A", 1), ("B", 0), ("C", 0)]]
    assert [h.chunk_id for h in top_docs(hits, 2)] == ["A#0", "B#0"]


def test_load_chunks_covers_every_policy():
    chunks = load_chunks()
    assert {c["doc_id"] for c in chunks} == {f"POL-{n:03d}" for n in range(1, 28)}
    assert all(c["text"] and c["title"] for c in chunks)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        HybridRetriever("postgresql://x", mode="magic")


def database():
    load_dotenv(".env")
    if not os.getenv("POSTGRES_USER"):
        return None
    from agent.config import db_url

    try:
        with psycopg.connect(db_url(), connect_timeout=2) as conn:
            conn.execute("SELECT 1 FROM policy_chunks LIMIT 1")
    except psycopg.Error:
        return None
    return db_url()


@pytest.mark.parametrize("mode", ["vector", "fulltext", "hybrid", "hybrid_rerank"])
def test_search_finds_lost_package_policy(mode):
    url = database()
    if url is None:
        pytest.skip("Postgres with ingested policies not available")
    hits = HybridRetriever(url, mode=mode).search_sync("my package never arrived")
    assert "POL-007" in [h.doc_id for h in hits]
    assert len(hits) == 5
