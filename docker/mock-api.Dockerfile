# The mock internal API. Build from the repo root:
#   docker build -f docker/mock-api.Dockerfile -t ops-agent-mock-api .
FROM python:3.13-slim

# Same versions as uv.lock; this service needs only these two.
RUN pip install --no-cache-dir fastapi==0.162.0 uvicorn==0.54.0

WORKDIR /app
COPY mock_api mock_api

RUN useradd --uid 1000 --create-home api
USER 1000

CMD ["uvicorn", "mock_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
