# The dashboard: the built React app and its API in one image. Build from the repo root:
#   docker build -f docker/dashboard.Dockerfile -t ops-agent-dashboard .
FROM node:24-slim AS web

WORKDIR /web
COPY dashboard/web/package.json dashboard/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/web/ ./
RUN npm run build

FROM python:3.13-slim

# Same versions as uv.lock. It needs no LLM client, retrieval or Jira code.
RUN pip install --no-cache-dir fastapi==0.162.0 uvicorn==0.54.0 "psycopg[binary]==3.3.7" \
    langgraph-checkpoint-postgres==3.2.0 boto3==1.43.123 httpx==0.28.1 pydantic==2.14.2

WORKDIR /app
COPY agent/__init__.py agent/config.py agent/tools.py agent/
COPY integrations/__init__.py integrations/approval.py integrations/
COPY dashboard/__init__.py dashboard/app.py dashboard/store.py dashboard/
COPY --from=web /web/dist dashboard/web/dist

RUN useradd --uid 1000 --create-home dashboard
USER 1000

CMD ["uvicorn", "dashboard.app:build", "--factory", "--host", "0.0.0.0", "--port", "8090"]
