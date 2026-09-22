# rag-backend

The shared "brain" for ACT AI. Every input path — typed chat, Deepgram English
voice, and the Sarvam Tamil pipeline — calls the same underlying chat logic,
so answers stay consistent across languages and input modes. Two routes
expose it: `/v1/chat/completions` (bearer-auth, OpenAI-compatible, for
Deepgram/Sarvam) and `/widget/chat` (public, simplified shape, for the
browser widget — see `widget/index.html`).

## Setup

```bash
cd rag-backend
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env         # fill in Azure OpenAI + auth key values
```

Required env vars (see `.env.example`):
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`,
  `AZURE_OPENAI_DEPLOYMENT` — this project uses **Azure OpenAI Service**
  (confirmed with Prakash), not a direct OpenAI key.
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` — an embedding-model deployment (e.g.
  `text-embedding-3-small`) used by the retriever to embed knowledge_base
  chunks and queries.
- `RAG_BACKEND_AUTH_KEY` — bearer token checked on every request; used by
  Deepgram's custom endpoint call, the Tamil pipeline's LLM call, and the
  widget's chat proxy.

## Run

```bash
uvicorn main:app --reload --port 8000
```

Test:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $RAG_BACKEND_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What courses does ACT offer?"}]}'
```

## Knowledge base

Markdown files in `knowledge_base/` are chunked (~500 tokens, 50-token
overlap), embedded via Azure OpenAI on process startup, and held in memory as
a numpy array for cosine-similarity top-k search (see `retriever.py`). The
Implementation Plan suggested Chroma; at this knowledge base's small scale a
plain numpy search is equivalent, and it avoids `chroma-hnswlib`'s compiled
C++ extension, which fails to build on Windows without Visual Studio Build
Tools. Revisit if the knowledge base grows large enough that an actual index
matters.

Content in `about.md`, `courses.md`, `admissions.md`, `placements.md`, and
`faq.md` was pulled from ACT's official site and public college-info
aggregators (CollegeDekho, Collegedunia, Shiksha) as of September 2026 — figures
like fees, cutoffs, and placement packages should be re-verified with
Prakash/admissions before the pilot, since these change yearly and third-party
sources disagreed on exact numbers (e.g., highest package reported).

To add more content: drop additional `.md` files into `knowledge_base/` — they
are re-ingested and re-embedded every time the process starts.
