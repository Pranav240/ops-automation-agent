"""Settings shared by the agent, retrieval and scripts. Values come from .env."""

import os

# The fixed "today" of the synthetic world (see evals/preregistration.md).
TODAY = "2026-10-01"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


def db_url() -> str:
    return (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5433')}"
        f"/{os.environ['POSTGRES_DB']}"
    )


def mock_api_url() -> str:
    return os.getenv("MOCK_API_URL", "http://127.0.0.1:8000")
