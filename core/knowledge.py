"""
Knowledge Module — ChromaDB-powered RAG for Sam.

Gives Sam a brain loaded with 139+ trading book summaries,
the Options Field Manual, and the Agentic Trader's Playbook.

Uses Gemini embeddings API (free tier: 1,500 req/min).
ChromaDB stores to data/chromadb/ (project-local, persists across restarts).
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_BATCH_SIZE = 20

# ---------------------------------------------------------------------------
# Lazy-loaded clients
# ---------------------------------------------------------------------------

_chroma_client: Optional[Any] = None
_genai_client = None


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


def _get_genai():
    global _genai_client
    if _genai_client is None:
        from google import genai
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client


def _embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embed texts using Gemini embedding API. Handles batching with robust offline fallback."""
    if not GEMINI_API_KEY:
        return [_deterministic_embedding(t) for t in texts]

    try:
        from google.genai import types

        client = _get_genai()
        all_embeddings = []

        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i:i + EMBEDDING_BATCH_SIZE]
            try:
                result = client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=768,
                    ),
                )
                all_embeddings.extend([e.values for e in result.embeddings])
            except Exception as e:
                logger.warning("Embedding error (batch %d): %s, using fallback", i, e)
                all_embeddings.extend([_deterministic_embedding(t) for t in batch])

        return all_embeddings
    except Exception as e:
        logger.warning("Embedding client failure (%s), using fallback", e)
        return [_deterministic_embedding(t) for t in texts]


def _embed_query(text: str) -> list[float]:
    """Embed a single query text."""
    results = _embed_texts([text], task_type="RETRIEVAL_QUERY")
    return results[0] if results else _deterministic_embedding(text)


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

        doc_text = (
            f"Ticker: {ticker} | Direction: {direction} | Playbook: {playbook} | "
            f"Regime: {regime_posture} | Origin: {origin} | Result: {result} | Thesis: {reasoning}"
        )
        embedding = _embed_texts([doc_text])[0]

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
