"""Hermetic tests for Conviction Journal and Reflection Node."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import pytest

from core.conviction import (
    log_conviction,
    _load_journal,
    _save_journal,
    deduplicate_journal_entries,
)
from vesper.state import TradingState, OrderProposal, ExecutionResult, MarketRegime, TechnicalAudit
from vesper.nodes.reflection import reflection_node


@pytest.fixture
def temp_journal(tmp_path, monkeypatch):
    """Isolate conviction journal storage in a temporary directory."""
    journal_file = tmp_path / "data" / "conviction_journal.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.conviction._DATA_DIR", data_dir)
    monkeypatch.setattr("core.conviction._JOURNAL_PATH", journal_file)
    return journal_file


@pytest.mark.asyncio
async def test_log_conviction_extended_fields_and_id_uniqueness(temp_journal):
    """Assert log_conviction generates collision-resistant IDs and captures all metadata."""
    res1 = await log_conviction(
        ticker="NVDA",
        direction="bullish",
        confidence=4,
        reasoning="EMA stack bullish bounce",
        signals="RSI_oversold,EMA_BULLISH",
        origin="EXECUTED",
        playbook="momentum_squeeze",
        regime_posture="BULLISH",
        session_id="sess-test-01",
        not_taken_reason=None,
        target_price=230.50,
        stop_loss=210.00,
        entry_price_override=218.40,
    )

    res2 = await log_conviction(
        ticker="NVDA",
        direction="bullish",
        confidence=4,
        reasoning="Second call in same second",
        signals="RSI_oversold,EMA_BULLISH",
        origin="EXECUTED",
        playbook="momentum_squeeze",
        regime_posture="BULLISH",
        session_id="sess-test-01",
        entry_price_override=218.40,
    )

    assert res1["status"] == "logged"
    assert res2["status"] == "logged"
    assert res1["id"] != res2["id"], "Consecutive calls within same second must have unique IDs"

    journal = _load_journal()
    assert len(journal) == 2
    entry = journal[0]
    assert entry["ticker"] == "NVDA"
    assert entry["origin"] == "EXECUTED"
    assert entry["playbook"] == "momentum_squeeze"
    assert entry["regime_posture"] == "BULLISH"
    assert entry["session_id"] == "sess-test-01"
    assert entry["target_price"] == 230.50
    assert entry["stop_loss"] == 210.00
    assert entry["entry_price"] == 218.40


def test_deduplicate_journal_entries():
    """Verify deduplication removes redundant entries while preserving chronology."""
    entries = [
        {
            "id": "AAPL:20260828201138",
            "ticker": "AAPL",
            "entry_date": "2026-08-28T20:11:38.377324+00:00",
            "session_id": "sess-1",
            "reasoning": "Duplicate thesis",
        },
        {
            "id": "AAPL:20260828201138",
            "ticker": "AAPL",
            "entry_date": "2026-08-28T20:11:38.378085+00:00",
            "session_id": "sess-1",
            "reasoning": "Duplicate thesis",
        },
        {
            "id": "SPY:20260828201150",
            "ticker": "SPY",
            "entry_date": "2026-08-28T20:11:50.000000+00:00",
            "session_id": "sess-2",
            "reasoning": "Distinct thesis",
        },
    ]

    deduped = deduplicate_journal_entries(entries)
    assert len(deduped) == 2
    assert deduped[0]["ticker"] == "AAPL"
    assert deduped[1]["ticker"] == "SPY"


@pytest.mark.asyncio
async def test_reflection_node_direction_mapping(temp_journal):
    """Verify reflection_node maps proposal.side to direction instead of string-sniffing res.message."""
    # Proposal for a BUY
    prop_buy = OrderProposal(
        id="prop-buy-1",
        ticker="AAPL",
        asset_type="EQUITY",
        side="BUY",
        limit_price=320.0,
        quantity=5,
        profit_target=335.0,
        stop_loss=312.0,
    )
    # Execution result for a REJECTED BUY
    res_rejected = ExecutionResult(
        order_proposal_id="prop-buy-1",
        ticker="AAPL",
        status="REJECTED_BY_USER",
        message="Order proposal prop-buy-1 was not approved.",  # Does NOT contain "BUY"
        filled_price=0.0,
    )

    # Proposal for a SELL
    prop_sell = OrderProposal(
        id="prop-sell-2",
        ticker="SPY",
        asset_type="OPTION",
        side="SELL",
        limit_price=2.50,
        quantity=1,
    )
    res_sell = ExecutionResult(
        order_proposal_id="prop-sell-2",
        ticker="SPY",
        status="DRY_RUN_SIMULATED",
        message="Simulated SELL 1 SPY @ $2.50",
        filled_price=2.50,
    )

    tech_aapl = TechnicalAudit(
        ticker="AAPL",
        close=320.0,
        rsi_14=55.0,
        rsi_state="neutral",
        ema_stack="BULLISH",
    )

    state: TradingState = {
        "session_id": "sess-refl-test",
        "mode": "dry_run",
        "selected_playbook": "all",
        "target_ticker": None,
        "regime": MarketRegime(posture="DEFENSIVE"),
        "candidates": [],
        "technicals": {"AAPL": tech_aapl},
        "options_audits": {},
        "proposals": [prop_buy, prop_sell],
        "execution_results": [res_rejected, res_sell],
        "needs_human_approval": False,
        "human_decision": "REJECT",
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    out = await reflection_node(state)
    assert len(out["reflection_notes"]) == 2
    assert "AAPL (REJECTED_BY_USER, bullish, origin=REJECTED_BY_USER)" in out["reflection_notes"][0]
    assert "SPY (DRY_RUN_SIMULATED, bearish, origin=EXECUTED)" in out["reflection_notes"][1]

    journal = _load_journal()
    assert len(journal) == 2

    # AAPL must be logged bullish even though execution was rejected
    aapl_entry = next(e for e in journal if e["ticker"] == "AAPL")
    assert aapl_entry["direction"] == "bullish"
    assert aapl_entry["origin"] == "REJECTED_BY_USER"
    assert aapl_entry["session_id"] == "sess-refl-test"
    assert aapl_entry["playbook"] == "all"
    assert aapl_entry["regime_posture"] == "DEFENSIVE"
    assert aapl_entry["target_price"] == 335.0
    assert aapl_entry["stop_loss"] == 312.0

    # SPY must be logged bearish
    spy_entry = next(e for e in journal if e["ticker"] == "SPY")
    assert spy_entry["direction"] == "bearish"
    assert spy_entry["origin"] == "EXECUTED"


@pytest.mark.asyncio
async def test_reflection_node_captures_all_unexecuted_tiers(temp_journal):
    """Verify reflection_node logs risk-rejected, user-rejected, and unproposed candidates."""
    from vesper.state import Candidate

    # Proposal rejected by human gate (bypassed executor)
    prop_user_declined = OrderProposal(
        id="prop-declined-1",
        ticker="AMD",
        asset_type="EQUITY",
        side="BUY",
        limit_price=160.0,
        quantity=10,
        profit_target=175.0,
        stop_loss=152.0,
    )

    # Proposal rejected earlier by deterministic risk gate
    prop_risk_blocked = OrderProposal(
        id="prop-risk-blocked-2",
        ticker="TSLA",
        asset_type="EQUITY",
        side="BUY",
        limit_price=350.0,
        quantity=50,
        rejection_reason="Exceeds maximum notional cap ($2,500)",
    )

    # Candidate screened but not synthesized into an order proposal
    cand_unproposed = Candidate(
        ticker="MSFT",
        source="VCP",
        score=78.5,
        rationale="Contraction 3T pattern on 21 EMA",
        data={"price": 425.50},
    )

    state: TradingState = {
        "session_id": "sess-tiers-test",
        "mode": "autonomous",
        "selected_playbook": "momentum_squeeze",
        "target_ticker": None,
        "regime": MarketRegime(posture="BULLISH"),
        "candidates": [cand_unproposed],
        "technicals": {
            "AMD": TechnicalAudit(ticker="AMD", close=160.0, rsi_14=48.0, rsi_state="neutral", ema_stack="BULLISH"),
            "TSLA": TechnicalAudit(ticker="TSLA", close=350.0, rsi_14=62.0, rsi_state="neutral", ema_stack="BULLISH"),
            "MSFT": TechnicalAudit(ticker="MSFT", close=425.50, rsi_14=51.0, rsi_state="neutral", ema_stack="BULLISH"),
        },
        "options_audits": {},
        "proposals": [prop_user_declined],
        "rejected_proposals": [prop_risk_blocked],
        "execution_results": [],  # Router sent directly to reflection due to human rejection
        "needs_human_approval": True,
        "human_decision": "REJECT",
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    out = await reflection_node(state)
    notes = out["reflection_notes"]

    assert any("AMD" in n and "REJECTED_BY_USER" in n for n in notes)
    assert any("TSLA" in n and "REJECTED_BY_RISK_GATE" in n for n in notes)
    assert any("MSFT" in n and "NOT_PROPOSED" in n for n in notes)

    journal = _load_journal()
    assert len(journal) == 3

    amd_entry = next(e for e in journal if e["ticker"] == "AMD")
    assert amd_entry["origin"] == "REJECTED_BY_USER"
    assert amd_entry["entry_price"] == 160.0

    tsla_entry = next(e for e in journal if e["ticker"] == "TSLA")
    assert tsla_entry["origin"] == "REJECTED_BY_RISK_GATE"
    assert "Exceeds maximum notional cap" in tsla_entry["not_taken_reason"]

    msft_entry = next(e for e in journal if e["ticker"] == "MSFT")
    assert msft_entry["origin"] == "NOT_PROPOSED"
    assert msft_entry["entry_price"] == 425.50
    assert msft_entry["confidence"] >= 3


@pytest.mark.asyncio
async def test_reflection_node_auto_resolves_mature_convictions(temp_journal, monkeypatch):
    """Verify reflection_node triggers auto-resolution for historical convictions."""
    from datetime import datetime, timezone, timedelta
    from core.conviction import _fetch_price

    # Seed an entry from 2 days ago
    past_date = datetime.now(timezone.utc) - timedelta(days=2)
    mature_entry = {
        "id": f"GOOG:{past_date.strftime('%Y%m%d%H%M%S')}_seed01",
        "ticker": "GOOG",
        "direction": "bullish",
        "confidence": 4,
        "reasoning": "Mature call from 48 hours ago",
        "signals": "RSI_oversold",
        "entry_price": 200.00,
        "entry_date": past_date.isoformat(),
        "entry_ts": int(past_date.timestamp()),
        "origin": "EXECUTED",
        "resolved": False,
        "resolutions": {},
    }
    _save_journal([mature_entry])

    # Mock price fetch to return 205.00 (+2.5% move -> WIN)
    async def mock_price(ticker):
        return 205.00

    monkeypatch.setattr("core.conviction._fetch_price", mock_price)

    state: TradingState = {
        "session_id": "sess-auto-res",
        "mode": "dry_run",
        "selected_playbook": "all",
        "target_ticker": None,
        "regime": MarketRegime(posture="BULLISH"),
        "candidates": [],
        "technicals": {},
        "options_audits": {},
        "proposals": [],
        "rejected_proposals": [],
        "execution_results": [],
        "needs_human_approval": False,
        "human_decision": "NO_PROPOSALS",
        "audit_trail": [],
        "reflection_notes": [],
        "errors": [],
    }

    out = await reflection_node(state)
    assert any("Auto-resolved" in n for n in out["reflection_notes"])

    journal = _load_journal()
    goog_entry = next(e for e in journal if e["ticker"] == "GOOG")
    assert "1d" in goog_entry["resolutions"]
    assert goog_entry["resolutions"]["1d"]["result"] == "WIN"
    assert goog_entry["resolutions"]["1d"]["pct_move"] == 2.5


@pytest.mark.asyncio
async def test_trade_memory_ingest_and_recall_similar_setups(temp_journal, tmp_path, monkeypatch):
    """Verify ChromaDB trade_memory ingestion and semantic setup recall."""
    import chromadb
    from core.knowledge import ingest_trade_memory, recall_similar_setups

    # Isolate ChromaDB storage to temp dir
    chroma_dir = tmp_path / "chromadb"
    chroma_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("core.knowledge.CHROMADB_PATH", chroma_dir)
    monkeypatch.setattr("core.knowledge._chroma_client", None)

    entry1 = {
        "id": "NVDA:20260828100000_abc123",
        "ticker": "NVDA",
        "direction": "bullish",
        "origin": "EXECUTED",
        "playbook": "momentum_squeeze",
        "regime_posture": "BULLISH",
        "session_id": "sess-mem-1",
        "confidence": 4,
        "entry_price": 215.0,
        "entry_date": "2026-08-28T10:00:00Z",
        "reasoning": "Pullback into 21 EMA action zone with bullish MACD cross",
        "resolutions": {"1d": {"result": "WIN", "pct_move": 3.4}},
    }

    entry2 = {
        "id": "TSLA:20260828100500_def456",
        "ticker": "TSLA",
        "direction": "bullish",
        "origin": "REJECTED_BY_RISK_GATE",
        "playbook": "momentum_squeeze",
        "regime_posture": "DEFENSIVE",
        "session_id": "sess-mem-2",
        "confidence": 3,
        "entry_price": 340.0,
        "entry_date": "2026-08-28T10:05:00Z",
        "reasoning": "Overbought exhaustion pullback with high RSI",
        "resolutions": {"1d": {"result": "LOSS", "pct_move": -2.8}},
    }

    ingest_trade_memory(entry1)
    ingest_trade_memory(entry2)

    # Recall similar setup
    recalled = await recall_similar_setups("bullish pullback into 21 EMA", top_k=2)
    assert len(recalled) >= 1
    assert recalled[0]["ticker"] in ("NVDA", "TSLA")
    assert "similarity" in recalled[0]
    assert recalled[0]["result"] in ("WIN", "LOSS")

    # Filtered recall
    recalled_nvda = await recall_similar_setups("EMA pullback", ticker="NVDA")
    assert len(recalled_nvda) == 1
    assert recalled_nvda[0]["ticker"] == "NVDA"
    assert recalled_nvda[0]["result"] == "WIN"


def test_playbook_performance_and_calibration_adjustment(temp_journal):
    """Verify playbook win-rate computation and dynamic calibration adjustment."""
    from datetime import datetime, timezone
    from core.conviction import get_playbook_performance

    # Seed 4 entries for 'momentum_squeeze' (3 WIN, 1 LOSS = 75% win rate)
    now_iso = datetime.now(timezone.utc).isoformat()
    journal_data = [
        {
            "id": f"TEST:{i}",
            "ticker": f"TKR{i}",
            "direction": "bullish",
            "playbook": "momentum_squeeze",
            "entry_date": now_iso,
            "resolutions": {"5d": {"result": "WIN" if i < 3 else "LOSS"}},
        }
        for i in range(4)
    ]
    _save_journal(journal_data)

    perf = get_playbook_performance("momentum_squeeze")
    assert perf["resolved"] == 4
    assert perf["wins"] == 3
    assert perf["win_rate_pct"] == 75.0
    assert perf["adjustment"] == 0.10  # Bonus for high win rate



# ── Embedding vector-space integrity (M8-25) ────────────────────────────────

def test_embedding_refuses_without_credential_by_default(monkeypatch):
    """REGRESSION. core/knowledge.py used to fall back to an MD5 word-hash
    'embedding' whenever the API key was missing or any batch call raised,
    writing those vectors into the SAME persistent collection as real ones.
    That is silently corrupt: the two spaces are not comparable, recall
    returns confident nonsense with no error, and adding a working key later
    does not repair rows already written in the wrong space.

    Production must refuse. The deterministic path is reachable only through
    the explicit test-only flag that conftest sets."""
    from core import knowledge as k

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KNOWLEDGE_ALLOW_FAKE_EMBEDDINGS", raising=False)

    with pytest.raises(k.EmbeddingUnavailable) as exc:
        k._embed_texts(["a bullish pullback into the 21 EMA"])
    assert "OPENROUTER_API_KEY" in str(exc.value)

    with pytest.raises(k.EmbeddingUnavailable):
        k._embed_query("same again")


def test_deterministic_embeddings_only_via_explicit_flag(monkeypatch):
    """The escape hatch works, and is genuinely opt-in."""
    from core import knowledge as k

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("KNOWLEDGE_ALLOW_FAKE_EMBEDDINGS", "1")

    vecs = k._embed_texts(["one", "two"])
    assert len(vecs) == 2
    assert all(len(v) == k.EMBEDDING_DIMENSIONS for v in vecs)
    # Deterministic, and lexical rather than semantic -- which is exactly why
    # it must never reach a real collection.
    assert vecs[0] == k._embed_texts(["one"])[0]


def test_embedding_never_writes_a_partial_batch(monkeypatch):
    """A mid-batch API failure must fail the whole call. Returning what
    succeeded would write some rows in the real space while the caller
    believes the batch completed -- the same mixed-space corruption by a
    different route."""
    from core import knowledge as k

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used-offline")
    monkeypatch.delenv("KNOWLEDGE_ALLOW_FAKE_EMBEDDINGS", raising=False)

    class _BoomClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw): raise RuntimeError("connection reset")

    import httpx
    monkeypatch.setattr(httpx, "Client", _BoomClient)

    with pytest.raises(k.EmbeddingUnavailable) as exc:
        k._embed_texts(["a", "b", "c"])
    assert "batch 0" in str(exc.value)


def test_embedding_refuses_a_short_response(monkeypatch):
    """Fewer vectors back than inputs sent is a partial write in disguise."""
    from core import knowledge as k

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used-offline")
    monkeypatch.delenv("KNOWLEDGE_ALLOW_FAKE_EMBEDDINGS", raising=False)

    class _ShortResp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"index": 0, "embedding": [0.0] * 768}]}

    class _ShortClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw): return _ShortResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _ShortClient)

    with pytest.raises(k.EmbeddingUnavailable) as exc:
        k._embed_texts(["a", "b"])
    assert "refusing a partial write" in str(exc.value)


def test_trade_memory_embeds_thesis_not_boilerplate(monkeypatch):
    """REGRESSION. ingest_trade_memory embedded the full display string --
    "Ticker: X | Direction: y | Playbook: z | Regime: N/A | Origin: ... |
    Result: PENDING | Thesis: ..." -- where the header is most of the
    characters and is near-identical across every row. That boilerplate
    dominated the vector and drowned the thesis, so recall ranked on noise:
    a query paraphrasing one setup returned an unrelated trade first.

    It also made the two sides asymmetric, since recall_similar_setups embeds
    a bare thesis. Cosine similarity between differently-shaped texts is not
    meaningful. Every header field is already in `metadata` and filterable via
    chroma's `where`, so embedding it bought nothing at all."""
    from core import knowledge as k

    seen: list[str] = []
    monkeypatch.setattr(
        k, "_embed_texts",
        lambda texts, task_type="RETRIEVAL_DOCUMENT": (
            seen.extend(texts) or [[0.0] * k.EMBEDDING_DIMENSIONS for _ in texts]
        ),
    )

    class _Col:
        def upsert(self, **kw): self.kw = kw
    col = _Col()
    monkeypatch.setattr(k, "_get_trade_memory_collection", lambda: col)
    monkeypatch.setattr(k, "_CHROMADB_AVAILABLE", True)

    thesis = "Bought the dip as price held the rising 21 EMA on shrinking volume."
    k.ingest_trade_memory({
        "id": "t1", "ticker": "SOFI", "direction": "long",
        "playbook": "momentum", "origin": "TEST", "reasoning": thesis,
    })

    assert seen == [thesis], f"embedded the wrong text: {seen!r}"
    for noise in ("Ticker:", "Direction:", "Playbook:", "Origin:", "Result:"):
        assert noise not in seen[0]

    # The readable document keeps the full header -- only the vector changed.
    assert "Ticker: SOFI" in col.kw["documents"][0]
    assert thesis in col.kw["documents"][0]
    # And the header fields remain filterable where they belong.
    assert col.kw["metadatas"][0]["playbook"] == "momentum"
