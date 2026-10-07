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
import hashlib
import logging
import os
from collections import OrderedDict
from pathlib import Path

import numpy as np
import tiktoken
from openai import AsyncAzureOpenAI

from data_loader import load_all_data

logger = logging.getLogger("rag_backend.retriever")

KNOWLEDGE_BASE_DIR = Path(__file__).parent / "knowledge_base"
# Chunk embeddings kept across restarts. With knowledge_base/website/ the index
# is ~3,000 chunks, which takes minutes to embed from scratch (Azure rate
# limits); with this, a restart only embeds chunks whose text changed.
EMBEDDING_CACHE_FILE = Path(__file__).parent / ".embedding_cache.npz"
CHUNK_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
# The generated mirror of act.edu.in (scrape_website.py). Everything outside it
# — the hand-written top-level .md files and admissions-supplied data tables —
# is the curated pool.
WEBSITE_DIR = KNOWLEDGE_BASE_DIR / "website"
QUERY_CACHE_SIZE = 256

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
        self._is_website: np.ndarray = np.zeros(0, dtype=bool)
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
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
            await self._ingest()
            self._ingested = True

    async def _ingest(self):
        """Build the full chunk set, then swap it in atomically — a failed
        rebuild (e.g. embedding error) leaves the previous index serving."""
        chunks: list[str] = []
        is_website: list[bool] = []
        # rglob: knowledge_base/website/ (see scrape_website.py) is nested by section.
        for file_path in sorted(KNOWLEDGE_BASE_DIR.rglob("*.md")):
            text = file_path.read_text(encoding="utf-8")
            file_chunks = _chunk_text(text)
            # A long page's later chunks would otherwise be anonymous text —
            # "Semester 3: ..." with nothing saying which department's syllabus.
            title = text.split("\n", 1)[0].strip()
            if title.startswith("# "):
                file_chunks[1:] = [f"{title} (continued)\n{c}" for c in file_chunks[1:]]
            chunks.extend(file_chunks)
            is_website.extend([WEBSITE_DIR in file_path.parents] * len(file_chunks))
        chunks.extend(await asyncio.to_thread(load_all_data))
        is_website.extend([False] * (len(chunks) - len(is_website)))
        embeddings = await self._embed_chunks(chunks) if chunks else None
        self._chunks, self._embeddings = chunks, embeddings
        self._is_website = np.array(is_website, dtype=bool)

    async def _embed_chunks(self, chunks: list[str]) -> np.ndarray:
        """Embed chunks, re-using vectors cached on disk for unchanged text."""
        keys = [
            hashlib.sha1(f"{self._embedding_deployment}\n{c}".encode()).hexdigest()
            for c in chunks
        ]
        cached: dict[str, np.ndarray] = {}
        try:
            with np.load(EMBEDDING_CACHE_FILE) as f:
                cached = dict(zip(f["keys"].tolist(), f["vectors"]))
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Unreadable embedding cache — re-embedding everything")
        missing = [i for i, k in enumerate(keys) if k not in cached]
        if missing:
            logger.info("Embedding %d new/changed chunk(s) of %d", len(missing), len(chunks))
            fresh = await self._embed_batched([chunks[i] for i in missing])
            cached.update((keys[i], v) for i, v in zip(missing, fresh))
        vectors = np.vstack([cached[k] for k in keys])
        if missing or len(cached) != len(set(keys)):  # new vectors, or stale ones to drop
            # ".npz" suffix required: np.savez appends it to any other name.
            tmp = EMBEDDING_CACHE_FILE.with_name(EMBEDDING_CACHE_FILE.stem + ".tmp.npz")
            np.savez(tmp, keys=np.array(keys), vectors=vectors)
            os.replace(tmp, EMBEDDING_CACHE_FILE)
        return vectors

    async def _embed_batched(self, texts: list[str], batch: int = 96) -> np.ndarray:
        parts = [await self._embed(texts[i : i + batch]) for i in range(0, len(texts), batch)]
        return np.vstack(parts)

    async def reload(self) -> int:
        """Re-read markdown, data files and the Google Sheet. Returns chunk count."""
        async with self._ingest_lock:
            await self._ingest()
            self._ingested = True
        return len(self._chunks)

    async def _embed_query(self, query: str) -> np.ndarray:
        """Embed one query, remembering recent ones: the Azure round-trip is
        ~0.3-0.45s of every voice turn, and repeated or re-used queries
        (see main._retrieval_query) skip it entirely. Cleared on reload only
        by being keyed to the same embedding model, so it never goes stale."""
        key = query.strip().lower()
        cached = self._query_cache.get(key)
        if cached is not None:
            self._query_cache.move_to_end(key)
            return cached
        vec = (await self._embed([query]))[0]
        self._query_cache[key] = vec
        if len(self._query_cache) > QUERY_CACHE_SIZE:
            self._query_cache.popitem(last=False)
        return vec

    async def warm_up(self):
        """Ingest now instead of on the first visitor's request."""
        await self._ensure_ingested()

    async def search(self, query: str, curated_k: int = 4, website_k: int = 3) -> str:
        """Top chunks from the curated pool AND, separately, from the website
        mirror — not one combined ranking. The mirror is ~300x larger (over
        half of it syllabus text), and in a single ranking it buries the
        curated answers: measured on "what is the counselling code?", the
        curated chunk holding the answer ranked 1,277th of 3,054. Ranking the
        small curated pool on its own keeps the behaviour it had before the
        mirror existed; the mirror then adds detail the curated files lack.
        Curated chunks come first in the returned context."""
        await self._ensure_ingested()

        if not self._chunks or self._embeddings is None:
            return "No matching context found."

        query_vec = await self._embed_query(query)
        scores = self._embeddings @ query_vec
        picked: list[int] = []
        for pool, k in ((~self._is_website, curated_k), (self._is_website, website_k)):
            pool_indices = np.flatnonzero(pool)
            picked.extend(pool_indices[np.argsort(-scores[pool_indices])[:k]])
        return "\n\n---\n\n".join(self._chunks[i] for i in picked)


retriever = Retriever()
