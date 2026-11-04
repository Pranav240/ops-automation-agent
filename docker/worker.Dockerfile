# The agent worker. It also runs the database setup job. Build from the repo root:
#   docker build -f docker/worker.Dockerfile -t ops-agent-worker --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .
FROM python:3.13-slim

RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PATH=/app/.venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/app/models PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
RUN rm -rf /root/.cache

COPY agent agent
COPY integrations integrations
COPY retrieval retrieval

# Models go in the image so that a new pod starts without downloading anything.
RUN python -c "from agent.config import EMBED_MODEL, RERANK_MODEL; \
from fastembed import TextEmbedding; \
from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextEmbedding(model_name=EMBED_MODEL); TextCrossEncoder(model_name=RERANK_MODEL)"

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=$GIT_COMMIT

RUN useradd --uid 1000 --create-home worker && chown -R worker /app/models
USER 1000

CMD ["python", "-m", "integrations.queue_worker"]
