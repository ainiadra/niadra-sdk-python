"""LangMem over HTTP, for the benchmark only. LangMem is a library with no server, so this service is the
smallest one that lets the harness call it the way its guides do, and nothing else:

- `POST /v1/conversations` {user_id, thread_id, messages}: queues one conversation for the memory manager
  (`create_memory_store_manager` with its defaults and the namespace `("memories", "{user_id}")`) and
  answers 202. One worker runs the queue in order, as LangMem's `ReflectionExecutor` does with its one
  thread: `await manager.ainvoke({"messages": ...}, config={"configurable": {"user_id": ...}})`, the call
  of its Background Quickstart.
- `GET /v1/queue`: what is queued, running, done and failed.
- `POST /v1/search` {user_id, query}: the store's search in the customer's namespace with `limit` 10, the
  default of LangMem's `search_memory` tool.
- `GET /v1/memories/{user_id}/{key}`: the store's `get`.
- `GET /health`.

The store is LangGraph's `AsyncPostgresStore` (LangMem's guides name it for production) with the
benchmark's embedder; the manager's model is the benchmark's extraction model; both go through the
system's gateway (`LLM_BASE_URL`). Nothing here changes what LangMem extracts or how it searches.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.store.postgres import AsyncPostgresStore
from langmem import create_memory_store_manager
from pydantic import BaseModel

log = logging.getLogger("langmem-service")

NAMESPACE = ("memories", "{user_id}")
SEARCH_LIMIT = 10  # create_search_memory_tool's default


class Message(BaseModel):
    role: str
    content: str


class ConversationIn(BaseModel):
    user_id: str
    thread_id: str
    messages: list[Message]


class SearchIn(BaseModel):
    user_id: str
    query: str


class Service:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ConversationIn] = asyncio.Queue()
        self.running = 0
        self.done = 0
        self.failed = 0
        self.store: AsyncPostgresStore | None = None
        self.manager: Any = None

    async def work(self) -> None:
        while True:
            item = await self.queue.get()
            self.running = 1
            try:
                await self.manager.ainvoke(
                    {"messages": [m.model_dump() for m in item.messages]},
                    config={"configurable": {"user_id": item.user_id, "thread_id": item.thread_id}},
                )
                self.done += 1
            except Exception:
                self.failed += 1
                log.exception("memory manager failed on %s", item.thread_id)
            finally:
                self.running = 0
                self.queue.task_done()


service = Service()


def content_of(value: dict[str, Any]) -> str:
    """A stored memory's text: the `content` of LangMem's default `Memory` schema, or the whole value."""
    content = value.get("content")
    if isinstance(content, dict) and isinstance(content.get("content"), str):
        return str(content["content"])
    return json.dumps(content if content is not None else value, ensure_ascii=False)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    base_url = os.environ["LLM_BASE_URL"]
    embeddings = OpenAIEmbeddings(
        model=os.environ["EMBEDDING_MODEL"],
        base_url=base_url,
        api_key="gateway",  # the gateway sends the provider key
        # Send the texts themselves: the embedder is not OpenAI's and takes no token ids.
        check_embedding_ctx_length=False,
    )
    index = {"dims": int(os.environ["EMBEDDING_DIMS"]), "embed": embeddings}
    async with AsyncPostgresStore.from_conn_string(os.environ["DATABASE_URL"], index=index) as store:
        await store.setup()
        model = ChatOpenAI(model=os.environ["EXTRACTION_MODEL"], base_url=base_url, api_key="gateway")
        service.store = store
        service.manager = create_memory_store_manager(model, namespace=NAMESPACE, store=store)
        worker = asyncio.create_task(service.work())
        try:
            yield
        finally:
            worker.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/conversations", status_code=202)
async def add_conversation(body: ConversationIn) -> dict[str, int]:
    await service.queue.put(body)
    return {"queued": service.queue.qsize()}


@app.get("/v1/queue")
async def queue() -> dict[str, int]:
    return {
        "queued": service.queue.qsize(),
        "running": service.running,
        "done": service.done,
        "failed": service.failed,
    }


@app.post("/v1/search")
async def search(body: SearchIn) -> dict[str, Any]:
    assert service.store is not None
    items = await service.store.asearch(("memories", body.user_id), query=body.query, limit=SEARCH_LIMIT)
    return {
        "memories": [
            {"key": i.key, "content": content_of(i.value), "score": i.score, "updated_at": str(i.updated_at)}
            for i in items
        ]
    }


@app.get("/v1/memories/{user_id}/{key}")
async def get_memory(user_id: str, key: str) -> dict[str, Any]:
    assert service.store is not None
    item = await service.store.aget(("memories", user_id), key)
    if item is None:
        raise HTTPException(status_code=404, detail="no such memory")
    return {"key": item.key, "content": content_of(item.value), "updated_at": str(item.updated_at)}
