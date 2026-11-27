"""Start the HTTP API with the real agent: python -m api [--port 8100]

Needs what the Jira worker needs (Postgres with run history and ingested policies, the mock API,
the LLM settings) plus ALLOWED_TENANT_ID, HTTP_API_HMAC_SECRET and WEBHOOK_URL in .env.
"""

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.config import db_url, git_state, mock_api_url
from agent.graph import build_graph
from agent.llm import LLMClient
from agent.nodes import Deps
from agent.run_history import RunHistory
from agent.tools import MockApi
from api.app import create_app
from api.runner import Runner
from api.store import ApiStore
from api.webhooks import Webhooks
from integrations.worker import Worker
from retrieval.search import HybridRetriever


def build():
    store = ApiStore(db_url())
    secret = os.environ.get("HTTP_API_HMAC_SECRET", "")

    @asynccontextmanager
    async def lifespan(app):
        llm = LLMClient.from_env()
        api = MockApi(mock_api_url())
        deps = Deps(llm=llm, retriever=HybridRetriever(db_url()), api=api)
        webhooks = Webhooks(os.getenv("WEBHOOK_URL", ""), secret)
        meta = {"model": llm.model, **git_state()}
        await asyncio.to_thread(store.setup)
        async with AsyncPostgresSaver.from_conn_string(db_url()) as saver:
            await saver.setup()
            graph = build_graph(deps, saver)
            worker = Worker(graph, deps, None, RunHistory(db_url()), meta)
            app.state.runner = Runner(worker, store, webhooks)
            await app.state.runner.fail_unfinished()
            yield
            await app.state.runner.drain()
        await webhooks.aclose()
        await api.aclose()
        llm.tracer.flush()

    return create_app(store, os.environ.get("ALLOWED_TENANT_ID", ""), secret, lifespan=lifespan)


if __name__ == "__main__":
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = uvicorn.Server(uvicorn.Config(build(), host=args.host, port=args.port))
    # psycopg's async mode does not work with Windows' default event loop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(server.serve(), loop_factory=loop_factory)
