"""
Knowledge Module — ChromaDB-powered RAG for Sam.

Gives Sam a brain loaded with 139+ trading book summaries,
the Options Field Manual, and the Agentic Trader's Playbook.

Embeddings go through OpenRouter (`/api/v1/embeddings`, OpenAI-schema), reusing
`OPENROUTER_API_KEY` -- the credential this project already holds for
`vesper/llm.py`. That is the whole reason it is not Gemini: every additional
secret on the deploy box is another thing to rotate and another placeholder
that can quietly go live (rule 2).

ChromaDB stores to data/chromadb/ (project-local, persists across restarts).

ON MIXING VECTOR SPACES -- the thing that used to be wrong here. Embeddings are
only comparable to other embeddings from the SAME model. This module previously
fell back to `_deterministic_embedding()` -- an MD5 word-hash, lexical and not
semantic -- whenever the API key was missing OR any single batch call raised.
Both paths wrote those hash vectors into the SAME persistent collection as real
ones, so a collection could silently end up holding two incompatible spaces,
and `recall_similar_setups` would return confident, plausible, meaningless
neighbours with no error anywhere. Worse, it is not self-healing: adding a
working key later does not fix rows already written in the wrong space.

So the fallback is now opt-in and test-only (`KNOWLEDGE_ALLOW_FAKE_EMBEDDINGS=1`),
and every other failure raises. Refusing to embed loses one call; poisoning a
persistent store loses the store.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import chromadb
    _CHROMADB_AVAILABLE = True
except ImportError:          # optional dependency
    chromadb = None
    _CHROMADB_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHROMADB_PATH = PROJECT_ROOT / "data" / "chromadb"
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBEDDING_DIMENSIONS = 768
EMBEDDING_BATCH_SIZE = 20
EMBEDDING_TIMEOUT_SEC = 60.0


class EmbeddingUnavailable(RuntimeError):
    """Raised when text cannot be embedded in the real vector space.

    Deliberately NOT caught-and-substituted anywhere in this module: the
    caller either gets real vectors or an error, never a quietly different
    vector space written into a persistent collection.
    """


def _embedding_api_key() -> str:
    """The OpenRouter key, or "" if it is absent or still a placeholder.

    The `your_` guard matches `vesper/llm.py`'s `is_llm_enabled()` and exists
    for the same reason: `.env.vesper.example` ships
    `OPENROUTER_API_KEY=your_openrouter_api_key`, and on 2026-09-04 that
    placeholder was found sitting in the live env file on the deploy box.
    Treating it as a real key turns a clear "not configured" into an opaque
    401 at request time (rule 2 -- a placeholder must never be mistaken for a
    credential)."""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key or key.startswith("your_"):
        return ""
    return key


def _fake_embeddings_allowed() -> bool:
    """Test-only escape hatch. The suite is hermetic (no network, no
    credentials), so it needs deterministic vectors -- but nothing should be
    able to reach them by accident, which is exactly how the old silent
    fallback poisoned collections."""
    return os.getenv("KNOWLEDGE_ALLOW_FAKE_EMBEDDINGS", "").strip() == "1"

# ---------------------------------------------------------------------------
# Lazy-loaded clients
# ---------------------------------------------------------------------------

_chroma_client: Optional[Any] = None


def _get_chroma() -> Any:
    global _chroma_client
    if not _CHROMADB_AVAILABLE:
        raise RuntimeError("chromadb optional dependency is not installed")
    if _chroma_client is None:
        CHROMADB_PATH.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(CHROMADB_PATH))
        logger.info("ChromaDB initialized at %s", CHROMADB_PATH)
    return _chroma_client


import math

def _deterministic_embedding(text: str, dim: int = 768) -> list[float]:
    """Generates a deterministic normalized unit vector for offline/testing use."""
    vec = [0.0] * dim
    words = text.lower().split()
    if not words:
        words = ["empty"]
    for i, word in enumerate(words):
        h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 1.0 / (1.0 + (i * 0.1))
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def _embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embed texts via OpenRouter's OpenAI-schema `/embeddings` endpoint.

    `task_type` is accepted for call-site compatibility with the previous
    Gemini implementation and ignored -- the OpenAI embedding schema has no
    equivalent, and silently pretending otherwise would be worse than saying
    so here.

    Raises `EmbeddingUnavailable` rather than substituting anything. See the
    module docstring on mixing vector spaces.
    """
    if not texts:
        return []

    api_key = _embedding_api_key()
    if not api_key:
        if _fake_embeddings_allowed():
            return [_deterministic_embedding(t, EMBEDDING_DIMENSIONS) for t in texts]
        raise EmbeddingUnavailable(
            "OPENROUTER_API_KEY is not set, so text cannot be embedded. Refusing "
            "rather than writing hash vectors into a persistent collection -- see "
            "core/knowledge.py's module docstring. Note this key must be present "
            "in the env contract the calling SERVICE reads: trading-agent.service "
            "reads .env.trading-agent, not .env.vesper."
        )

    import httpx

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/mphinance/webull-sidecar",
        "X-Title": "Vesper Quant Trading System",
    }

    all_embeddings: list[list[float]] = []
    with httpx.Client(timeout=EMBEDDING_TIMEOUT_SEC) as client:
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i:i + EMBEDDING_BATCH_SIZE]
            payload = {
                "model": EMBEDDING_MODEL,
                "input": batch,
                "dimensions": EMBEDDING_DIMENSIONS,
            }
            try:
                resp = client.post(OPENROUTER_EMBEDDINGS_URL, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()["data"]
            except Exception as e:
                # A partial result is the dangerous shape: it would write some
                # rows in the real space and leave the caller believing the
                # whole batch succeeded. Fail the entire call instead.
                raise EmbeddingUnavailable(
                    f"embedding request failed at batch {i} ({type(e).__name__}: {e})"
                ) from e

            if len(data) != len(batch):
                raise EmbeddingUnavailable(
                    f"embedding response returned {len(data)} vectors for "
                    f"{len(batch)} inputs; refusing a partial write"
                )
            # The API does not promise input order, but does return `index`.
            for item in sorted(data, key=lambda d: d.get("index", 0)):
                all_embeddings.append(item["embedding"])

    return all_embeddings


def _embed_query(text: str) -> list[float]:
    """Embed a single query text. Propagates `EmbeddingUnavailable`."""
    return _embed_texts([text], task_type="RETRIEVAL_QUERY")[0]


# ---------------------------------------------------------------------------
# Knowledge Base Collection
# ---------------------------------------------------------------------------

def _get_knowledge_collection() -> chromadb.Collection:
    return _get_chroma().get_or_create_collection(
        name="knowledge_base",
        metadata={"hnsw:space": "cosine"},
    )


def ingest_knowledge(
    text: str,
    source: str,
    doc_type: str = "book_summary",
    section: str = "",
) -> Any:
    """Ingest a chunk of knowledge into the knowledge base."""
    if not _CHROMADB_AVAILABLE:
        return {"available": False, "reason": "chromadb optional dependency is not installed"}
    try:
        collection = _get_knowledge_collection()
        chunk_id = hashlib.md5(f"{source}:{section}:{text[:100]}".encode()).hexdigest()
        embedding = _embed_texts([text])[0]

        collection.upsert(
            ids=[chunk_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "source": source,
                "section": section,
                "doc_type": doc_type,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }],
        )
    except Exception as e:
        logger.warning("Knowledge ingest error for %s: %s", source, e)


# ---------------------------------------------------------------------------
# Trade Memory Collection (Module 5 Semantic Recall)
# ---------------------------------------------------------------------------

def _get_trade_memory_collection() -> Any:
    return _get_chroma().get_or_create_collection(
        name="trade_memory",
        metadata={"hnsw:space": "cosine"},
    )


def ingest_trade_memory(entry: dict) -> Any:
    """Ingest a conviction journal entry or trade outcome into the ChromaDB trade_memory collection."""
    if not _CHROMADB_AVAILABLE:
        return {"available": False, "reason": "chromadb optional dependency is not installed"}
    try:
        collection = _get_trade_memory_collection()
        chunk_id = str(entry.get("id"))
        ticker = str(entry.get("ticker", "")).upper()
        direction = str(entry.get("direction", "")).lower()
        origin = str(entry.get("origin", "EXECUTED"))
        playbook = str(entry.get("playbook") or "N/A")
        regime_posture = str(entry.get("regime_posture") or "N/A")
        session_id = str(entry.get("session_id") or "N/A")
        reasoning = str(entry.get("reasoning") or f"{direction} thesis on {ticker}")

        # Derive best resolution status
        resolutions = entry.get("resolutions", {})
        result = "PENDING"
        pct_move = 0.0
        for h in ["5d", "1d", "10d"]:
            if h in resolutions:
                result = str(resolutions[h].get("result", "PENDING"))
                pct_move = float(resolutions[h].get("pct_move", 0.0))
                break

        # `doc_text` is what a human reads back; `embed_text` is what the
        # vector is built from, and they are deliberately NOT the same string.
        #
        # This used to embed doc_text wholesale. That metadata header is most
        # of the characters and is near-identical across records -- every row
        # carries "Ticker: | Direction: | Playbook: | Regime: | Origin: |
        # Result:" and most carry "N/A"/"PENDING" -- so it dominated the
        # vector and drowned out the thesis. Recall then ranked essentially on
        # boilerplate: a query paraphrasing one setup's thesis returned an
        # unrelated trade first (SOFI momentum vs XOM mean-reversion, verified
        # on the box 2026-09-04).
        #
        # It also made the two sides asymmetric -- documents got the header,
        # queries came in as bare prose from recall_similar_setups -- and
        # cosine similarity between differently-shaped texts is not meaningful.
        # Every one of those fields is ALREADY in `metadata` below and
        # filterable via chroma's `where`, so embedding them bought nothing.
        doc_text = (
            f"Ticker: {ticker} | Direction: {direction} | Playbook: {playbook} | "
            f"Regime: {regime_posture} | Origin: {origin} | Result: {result} | Thesis: {reasoning}"
        )
        embed_text = reasoning
        embedding = _embed_texts([embed_text])[0]

        metadata = {
            "ticker": ticker,
            "direction": direction,
            "origin": origin,
            "playbook": playbook,
            "regime_posture": regime_posture,
            "session_id": session_id,
            "confidence": int(entry.get("confidence", 3)),
            "entry_price": float(entry.get("entry_price", 0.0)),
            "entry_date": str(entry.get("entry_date", "")),
            "resolved": bool(entry.get("resolved", False)),
            "result": result,
            "pct_move": pct_move,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

        collection.upsert(
            ids=[chunk_id],
            embeddings=[embedding],
            documents=[doc_text],
            metadatas=[metadata],
        )
    except Exception as e:
        logger.warning("Trade memory ingest error for %s: %s", entry.get("ticker"), e)


async def recall_similar_setups(
    query_thesis: str,
    top_k: int = 5,
    ticker: Optional[str] = None,
    playbook: Optional[str] = None,
    origin: Optional[str] = None,
) -> Any:
    """Recall similar historical setups and their outcomes from trade memory.

    Args:
        query_thesis: Description or signals of the setup being considered.
        top_k: Number of historical setups to recall.
        ticker: Optional filter for a specific ticker.
        playbook: Optional filter for a specific playbook.
        origin: Optional filter for lifecycle origin (e.g. 'EXECUTED', 'REJECTED_BY_RISK_GATE').

    Returns:
        List of similar past setups with similarity score, outcome results, and thesis.
    """
    if not _CHROMADB_AVAILABLE:
        return {"available": False, "reason": "chromadb optional dependency is not installed"}
    try:
        collection = _get_trade_memory_collection()
        if collection.count() == 0:
            return []

        conditions = []
        if ticker:
            conditions.append({"ticker": ticker.upper()})
        if playbook:
            conditions.append({"playbook": playbook})
        if origin:
            conditions.append({"origin": origin})

        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}
        else:
            where_filter = None

        query_embedding = _embed_query(query_thesis)

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, collection.count()),
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        output = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                score = 1.0 - results["distances"][0][i]
                meta = results["metadatas"][0][i]
                output.append({
                    "id": doc_id,
                    "ticker": meta.get("ticker"),
                    "direction": meta.get("direction"),
                    "playbook": meta.get("playbook"),
                    "regime_posture": meta.get("regime_posture"),
                    "origin": meta.get("origin"),
                    "result": meta.get("result"),
                    "pct_move": meta.get("pct_move"),
                    "resolved": meta.get("resolved"),
                    "entry_date": meta.get("entry_date"),
                    "similarity": round(max(0.0, score), 3),
                    "document": results["documents"][0][i],
                })
        return output
    except Exception as e:
        logger.warning("Trade memory recall error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Search — LLM Tool
# ---------------------------------------------------------------------------

async def search_knowledge(
    query: str,
    top_k: int = 5,
    n_results: int | None = None,
    **kwargs: Any,
) -> Any:
    """
    Search Sam's knowledge base — 139 trading books, options manual, and methodology.

    Args:
        query: What to search for (e.g., "cash secured puts", "EMA stack", "VoPR").
        top_k: Number of results. Default: 5.
        n_results: Alias for top_k.

    Returns a list of relevant knowledge chunks with source attribution.
    """
    if not _CHROMADB_AVAILABLE:
        return {"available": False, "reason": "chromadb optional dependency is not installed"}
    k = n_results if n_results is not None else top_k
    try:
        collection = _get_knowledge_collection()
        if collection.count() == 0:
            return [{"info": "Knowledge base is empty — run seed_knowledge.py first"}]

        query_embedding = _embed_query(query)

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(k, collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        output = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                score = 1 - results["distances"][0][i]  # cosine distance → similarity
                if score < 0.25:  # skip low-relevance noise
                    continue
                meta = results["metadatas"][0][i]
                output.append({
                    "text": results["documents"][0][i],
                    "source": meta.get("source", "?"),
                    "section": meta.get("section", ""),
                    "doc_type": meta.get("doc_type", ""),
                    "relevance": round(score, 3),
                })

        return output if output else [{"info": "No relevant knowledge found for that query."}]
    except Exception as e:
        logger.warning("Knowledge search error: %s", e)
        return [{"error": str(e)}]


# ---------------------------------------------------------------------------
# RAG Context Builder — auto-injection into prompts
# ---------------------------------------------------------------------------

def get_rag_context(query: str, max_chars: int = 4000) -> tuple[str, list[dict]]:
    """
    Build RAG context string for prompt injection.

    Returns (context_string, sources_list) where sources_list contains
    the source/section pairs for Glass Box transparency display.
    """
    if not _CHROMADB_AVAILABLE:
        return "", []
    try:
        collection = _get_knowledge_collection()
        if collection.count() == 0:
            return "", []

        query_embedding = _embed_query(query)

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(5, collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        sections = []
        sources = []
        total_chars = 0

        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                score = 1 - results["distances"][0][i]
                if score < 0.35:  # higher threshold for auto-injection
                    continue

                meta = results["metadatas"][0][i]
                text = results["documents"][0][i]

                # Trim if needed but keep it thorough
                if total_chars + len(text) > max_chars:
                    remaining = max_chars - total_chars
                    if remaining < 200:
                        break
                    text = text[:remaining] + "..."

                source = meta.get("source", "Unknown")
                section = meta.get("section", "")
                header = f"[{source}" + (f" — {section}]" if section else "]")
                sections.append(f"{header}\n{text}")
                sources.append({
                    "source": source,
                    "section": section,
                    "relevance": round(score, 3),
                })
                total_chars += len(text) + len(header)

        if not sections:
            return "", []

        context = "## Relevant Knowledge from Sam's Library:\n\n" + "\n\n---\n\n".join(sections)
        return context, sources
    except Exception as e:
        logger.warning("RAG context error: %s", e)
        return "", []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_knowledge_stats() -> dict:
    """Get knowledge base statistics."""
    if not _CHROMADB_AVAILABLE:
        return {"available": False, "reason": "chromadb optional dependency is not installed"}
    try:
        kb = _get_knowledge_collection()
        return {
            "total_chunks": kb.count(),
            "chromadb_path": str(CHROMADB_PATH),
        }
    except Exception as e:
        return {"error": str(e)}
