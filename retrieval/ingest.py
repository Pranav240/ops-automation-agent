"""Load the policy docs with LlamaIndex, embed them, and write the chunks to Postgres.

Each chunk gets a pgvector embedding and a full-text tsvector. Re-running rebuilds the table.

Usage: python -m retrieval.ingest
"""

from pathlib import Path

import psycopg
from dotenv import load_dotenv
from llama_index.core import Document
from llama_index.core.node_parser import SentenceSplitter
from pgvector.psycopg import register_vector

from agent.config import EMBED_DIM, EMBED_MODEL, db_url

KB_DIR = Path(__file__).parent / "kb"

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS policy_chunks (
    id text PRIMARY KEY,
    doc_id text NOT NULL,
    title text NOT NULL,
    text text NOT NULL,
    embedding vector({EMBED_DIM}) NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || text)) STORED
);
CREATE INDEX IF NOT EXISTS policy_chunks_tsv ON policy_chunks USING gin (tsv);
"""


def parse_doc(path: Path) -> Document:
    """Split '---' frontmatter (id, title, source) from the markdown body."""
    _, front, body = path.read_text(encoding="utf-8").split("---", 2)
    meta = dict(line.split(": ", 1) for line in front.strip().splitlines())
    return Document(
        id_=meta["id"],
        text=body.strip(),
        metadata={"doc_id": meta["id"], "title": meta["title"]},
        excluded_embed_metadata_keys=["doc_id"],
    )


def load_chunks(kb_dir: Path = KB_DIR) -> list[dict]:
    docs = [parse_doc(p) for p in sorted(kb_dir.glob("POL-*.md"))]
    nodes = SentenceSplitter(chunk_size=256, chunk_overlap=32).get_nodes_from_documents(docs)
    chunks = []
    counts: dict[str, int] = {}
    for node in nodes:
        doc_id = node.metadata["doc_id"]
        n = counts.get(doc_id, 0)
        counts[doc_id] = n + 1
        chunks.append(
            {
                "id": f"{doc_id}#{n}",
                "doc_id": doc_id,
                "title": node.metadata["title"],
                "text": node.get_content(),
            }
        )
    return chunks


def embedder():
    from llama_index.embeddings.fastembed import FastEmbedEmbedding

    return FastEmbedEmbedding(model_name=EMBED_MODEL)


def ingest(conninfo: str) -> int:
    chunks = load_chunks()
    vectors = embedder().get_text_embedding_batch([f"{c['title']}\n{c['text']}" for c in chunks])
    with psycopg.connect(conninfo) as conn:
        conn.execute(SCHEMA)
        register_vector(conn)
        conn.execute("TRUNCATE policy_chunks")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO policy_chunks (id, doc_id, title, text, embedding) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (c["id"], c["doc_id"], c["title"], c["text"], v)
                    for c, v in zip(chunks, vectors, strict=True)
                ],
            )
    return len(chunks)


if __name__ == "__main__":
    load_dotenv(".env")
    print(f"{ingest(db_url())} chunks written")
