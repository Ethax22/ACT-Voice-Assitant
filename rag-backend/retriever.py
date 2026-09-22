"""Lightweight in-process retrieval over ACT's knowledge base.

Chunks markdown files in knowledge_base/ into ~500-token pieces with slight
overlap, embeds them with Azure OpenAI's embedding deployment, and holds them
in memory for cosine-similarity top-k search.

NOTE: The Implementation Plan suggested Chroma for this. At this knowledge
base's scale (a handful of markdown files, a few dozen chunks total), a plain
numpy cosine-similarity search is equivalent in practice — and it avoids
chroma-hnswlib's compiled C++ extension, which fails to build on Windows
without Visual Studio Build Tools installed. Flagging this as a deliberate
deviation from the plan rather than a silent one; revisit if the knowledge
base grows large enough that an index actually matters.

Uses the ASYNC Azure OpenAI client — this is called on every voice/chat turn,
including several speculative calls per turn from Deepgram's Flux agent. A
synchronous client here would block the whole event loop per call, so
concurrent requests would queue up behind each other instead of running in
parallel, causing exactly the kind of multi-second latency blowup that made
Deepgram time out and Sarvam's pipeline stall in early testing.

Ingestion is LAZY (triggered by the first real `search()` call) rather than
run at import time via `asyncio.run()`. `AsyncAzureOpenAI` wraps an
`httpx.AsyncClient` bound to whichever event loop is running when it's first
used; `asyncio.run()` creates and then closes its own temporary loop, so
ingesting there would bind the client to a loop that's already gone by the
time real requests arrive on uvicorn's actual server loop — every later
embedding call would silently fail. Lazy init keeps everything on one loop.
"""
import asyncio
import os
from pathlib import Path

import numpy as np
import tiktoken
from openai import AsyncAzureOpenAI

KNOWLEDGE_BASE_DIR = Path(__file__).parent / "knowledge_base"
CHUNK_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50

_encoding = tiktoken.get_encoding("cl100k_base")


def _chunk_text(text: str) -> list[str]:
    tokens = _encoding.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + CHUNK_TOKENS, len(tokens))
        chunks.append(_encoding.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start = end - CHUNK_OVERLAP_TOKENS
    return chunks


class Retriever:
    def __init__(self):
        self._client = AsyncAzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        self._embedding_deployment = os.environ.get(
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
        )
        self._chunks: list[str] = []
        self._embeddings: np.ndarray | None = None
        self._ingest_lock = asyncio.Lock()
        self._ingested = False

    async def _embed(self, texts: list[str]) -> np.ndarray:
        response = await self._client.embeddings.create(
            model=self._embedding_deployment,
            input=texts,
        )
        vectors = np.array([item.embedding for item in response.data], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        return vectors / norms

    async def _ensure_ingested(self):
        if self._ingested:
            return
        async with self._ingest_lock:
            if self._ingested:  # re-check: another request may have ingested while we waited
                return
            for file_path in sorted(KNOWLEDGE_BASE_DIR.glob("*.md")):
                text = file_path.read_text(encoding="utf-8")
                self._chunks.extend(_chunk_text(text))
            if self._chunks:
                self._embeddings = await self._embed(self._chunks)
            self._ingested = True

    async def search(self, query: str, top_k: int = 4) -> str:
        await self._ensure_ingested()

        if not self._chunks or self._embeddings is None:
            return "No matching context found."

        query_vec = (await self._embed([query]))[0]
        scores = self._embeddings @ query_vec
        top_indices = np.argsort(-scores)[:top_k]
        return "\n\n---\n\n".join(self._chunks[i] for i in top_indices)


retriever = Retriever()
