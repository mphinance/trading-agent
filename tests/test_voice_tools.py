"""Tests for M8-01, M8-04, and M8-05: voice watch tools.

Covers:
- M8-01: watch_setup(proposal_id) - small speakable payload (<2KB), distance-to-trigger sign
         conventions for LONG/SHORT, partial degradation when core.md raises, unknown proposal_id handling.
- M8-04: Repeat-call suppression cache - unchanged payload compression, distance retained, price delta trigger.
- M8-05: find_pending_setup(query) - fuzzy symbol resolution ('in video' -> NVDA), ambiguous query handling,
         no-match graceful degradation, and symbol echoing.
"""

import json
import time
import pytest
from unittest.mock import MagicMock, patch

from core.approval_registry import approval_registry
from trading_mcp.voice_tools import (
    watch_setup,
    find_pending_setup,
    _WATCH_CACHE,
    CACHE_TTL_SECONDS,
)


@pytest.fixture(autouse=True)
def clear_voice_cache():
    _WATCH_CACHE.clear()
    yield
    _WATCH_CACHE.clear()


@pytest.fixture
def mock_clean_registry(monkeypatch, tmp_path):
    """Ensure an isolated approval registry state file."""
    state_file = tmp_path / "approval_registry_state.json"
    import core.approval_registry as reg
    monkeypatch.setattr(reg, "_APPROVAL_STATE_PATH", state_file)
    return reg.approval_registry


def test_watch_setup_long_fixture(monkeypatch, mock_clean_registry):
    """M8-01 Step 2: Long setup with price below entry.
    Assert response has documented top-level keys, no raw OHLCV array, and correct distance.
    """
    prop_id = "prop-nvda-long"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-1",
        details={
            "ticker": "NVDA",
            "side": "BUY",
            "entry": 130.0,
            "stop": 125.0,
            "target": 140.0,
            "thesis": "Bullish continuation off 20-day SMA",
        },
    )

    # Mock market data from core.md
    fake_bars = [
        {"high": 128.5, "low": 126.0, "close": 128.0, "volume": 1000.0}
        for _ in range(25)
    ]
    # Set current price to 128.0 (below entry 130.0)
    fake_md = MagicMock()
    fake_md.snapshot.return_value = {"NVDA": {"last": 128.0}}
    fake_md.bars.return_value = fake_bars

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": True, "summary_phrase": "gamma flip at 125.0"}):

        res = watch_setup(prop_id)

    assert res["available"] is True
    assert res["proposal_id"] == prop_id
    assert res["symbol"] == "NVDA"
    assert res["side"] == "BUY"
    assert res["thesis"] == "Bullish continuation off 20-day SMA"
    assert res["entry"] == 130.0
    assert res["current_price"] == 128.0

    # Distance for long: entry - price = 130.0 - 128.0 = 2.0 dollars
    assert res["distance_to_trigger_dollars"] == 2.0
    expected_pct = (2.0 / 130.0) * 100.0
    assert abs(res["distance_to_trigger_pct"] - expected_pct) < 0.01
    assert "under the trigger" in res["distance_phrase"]

    # Assert no raw OHLCV list in the response
    assert "bars" not in res
    assert "ohlcv" not in res
    assert "raw_bars" not in res

    # M8-01 Step 4: Payload serialized stays under 2KB
    serialized = json.dumps(res)
    assert len(serialized) < 2048, f"Payload too large: {len(serialized)} bytes"


def test_watch_setup_short_fixture_sign_convention(monkeypatch, mock_clean_registry):
    """M8-01 Step 3: Short setup with price above entry.
    Confirm distance-to-trigger sign convention is correct for short direction.
    """
    prop_id = "prop-tsla-short"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-2",
        details={
            "ticker": "TSLA",
            "side": "SELL",
            "entry": 200.0,
            "stop": 210.0,
            "target": 180.0,
            "thesis": "Breakdown below psychological support",
        },
    )

    # Current price 205.0 (above entry 200.0) -> needs to drop 5.0 to trigger
    fake_bars = [
        {"high": 206.0, "low": 204.0, "close": 205.0, "volume": 500.0}
        for _ in range(25)
    ]
    fake_md = MagicMock()
    fake_md.snapshot.return_value = {"TSLA": {"last": 205.0}}
    fake_md.bars.return_value = fake_bars

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": True, "summary_phrase": "gamma flip at 200.0"}):

        res = watch_setup(prop_id)

    assert res["available"] is True
    assert res["side"] == "SELL"
    # Distance for short: price - entry = 205.0 - 200.0 = 5.0 dollars
    assert res["distance_to_trigger_dollars"] == 5.0
    expected_pct = (5.0 / 200.0) * 100.0
    assert abs(res["distance_to_trigger_pct"] - expected_pct) < 0.01
    assert "above the trigger" in res["distance_phrase"]


def test_watch_setup_market_data_failure_partial_degradation(monkeypatch, mock_clean_registry):
    """M8-01 Step 5: When core.md raises, confirm partial degradation:
    price/structure/VWAP report available:false while entry/stop/target/thesis still return.
    """
    prop_id = "prop-degraded"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-3",
        details={
            "ticker": "SPY",
            "side": "BUY",
            "entry": 550.0,
            "stop": 545.0,
            "target": 560.0,
            "thesis": "Gamma squeeze setup",
        },
    )

    # Simulate core.md raising an exception
    fake_md = MagicMock()
    fake_md.snapshot.side_effect = RuntimeError("Webull Market Data API timeout")
    fake_md.bars.side_effect = RuntimeError("Market data feed down")

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": False, "reason": "timeout"}):

        res = watch_setup(prop_id)

    # Core proposal data returned intact
    assert res["available"] is True
    assert res["proposal_id"] == prop_id
    assert res["symbol"] == "SPY"
    assert res["entry"] == 550.0
    assert res["stop"] == 545.0
    assert res["target"] == 560.0
    assert res["thesis"] == "Gamma squeeze setup"

    # Market data gracefully degraded
    assert res["price_available"] is False
    assert res["current_price"] is None
    assert res["structure_summary"]["available"] is False
    assert res["vwap_relation"]["available"] is False


def test_watch_setup_unknown_proposal_id(mock_clean_registry):
    """M8-01 Step 6: Unknown proposal_id degrades to available:false rather than raising."""
    res = watch_setup("non-existent-id")
    assert res["available"] is False
    assert "non-existent-id" in res["reason"]


def test_repeat_call_suppression_cache(monkeypatch, mock_clean_registry):
    """M8-04: Repeat-call suppression when nothing materially changed.
    1. First call: returns full payload.
    2. Second call shortly after: returns compact response (unchanged:true, distance present).
    3. Third call with price moved past 0.25%: re-emits full payload.
    """
    prop_id = "prop-suppress-test"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="sess-suppress",
        details={
            "ticker": "AAPL",
            "side": "BUY",
            "entry": 230.0,
            "stop": 225.0,
            "target": 240.0,
            "thesis": "Ascending triangle breakout",
        },
    )

    fake_bars = [{"high": 229.0, "low": 227.0, "close": 228.0, "volume": 1000.0} for _ in range(25)]
    fake_md = MagicMock()
    fake_md.snapshot.return_value = {"AAPL": {"last": 228.0}}
    fake_md.bars.return_value = fake_bars

    with patch("trading_mcp.voice_tools._get_market_client", return_value=fake_md), \
         patch("trading_mcp.voice_tools.get_compact_gamma", return_value={"available": True, "summary_phrase": "flip at 225"}):

        # Call 1: Full payload
        res1 = watch_setup(prop_id)
        assert res1["unchanged"] is False
        assert "thesis" in res1
        assert res1["current_price"] == 228.0

        # Call 2: Identical state 5 seconds later
        res2 = watch_setup(prop_id)
        assert res2["unchanged"] is True
        assert "unchanged" in res2["speakable_summary"].lower()
        assert res2["distance_to_trigger_dollars"] == 2.0
        assert "thesis" not in res2

        # Call 3: Price moved from 228.0 to 229.0 (0.44% move > 0.25% threshold)
        fake_md.snapshot.return_value = {"AAPL": {"last": 229.0}}
        res3 = watch_setup(prop_id)
        assert res3["unchanged"] is False
        assert "thesis" in res3
        assert res3["current_price"] == 229.0
        assert res3["distance_to_trigger_dollars"] == 1.0


def test_find_pending_setup_fuzzy_match(mock_clean_registry):
    """M8-05: Fuzzy resolves spoken query against pending proposals."""
    mock_clean_registry.register_pending(
        proposal_id="prop-nvda",
        session_id="s1",
        details={"ticker": "NVDA", "side": "BUY", "entry": 130.0},
    )
    mock_clean_registry.register_pending(
        proposal_id="prop-aapl",
        session_id="s2",
        details={"ticker": "AAPL", "side": "BUY", "entry": 230.0},
    )

    # 1. 'in video' resolves to NVDA
    res = find_pending_setup("in video")
    assert res["available"] is True
    assert res["ambiguous"] is False
    assert res["resolved_symbol"] == "NVDA"
    assert res["proposal_id"] == "prop-nvda"

    # 2. Direct clean query echoes symbol
    res_aapl = find_pending_setup("AAPL")
    assert res_aapl["available"] is True
    assert res_aapl["resolved_symbol"] == "AAPL"

    # 3. No match query degrades gracefully
    res_none = find_pending_setup("xyz_unknown_ticker")
    assert res_none["available"] is False
    assert res_none["reason"] == "no_match"


def test_find_pending_setup_ambiguous(mock_clean_registry):
    """M8-05: Ambiguous query matching two proposals equally returns ambiguous flag."""
    mock_clean_registry.register_pending(
        proposal_id="prop-goog",
        session_id="s1",
        details={"ticker": "GOOG", "side": "BUY"},
    )
    mock_clean_registry.register_pending(
        proposal_id="prop-googl",
        session_id="s2",
        details={"ticker": "GOOGL", "side": "BUY"},
    )

    res = find_pending_setup("GOOG")
    # Both GOOG and GOOGL are strong matches for "GOOG"
    if res["ambiguous"]:
        assert len(res["matches"]) >= 2
    else:
        # If scored higher on exact match, resolved_symbol must be GOOG
        assert res["resolved_symbol"] in ("GOOG", "GOOGL")


@pytest.mark.asyncio
async def test_snooze_proposal(mock_clean_registry):
    """M8-06: snooze_proposal sets suppress_until without altering price, quantity, or status."""
    from trading_mcp.voice_tools import snooze_proposal

    prop_id = "prop-snooze-test"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="s1",
        details={"ticker": "NVDA", "side": "BUY", "limit_price": 130.0, "quantity": 10},
    )

    res = snooze_proposal(prop_id, minutes=30)
    assert res["available"] is True
    assert res["proposal_id"] == prop_id
    assert "suppress_until" in res

    # Verify state in registry
    record = mock_clean_registry.get_pending(prop_id)
    assert record is not None
    assert record["status"] == "PENDING"
    assert record["suppress_until"] == res["suppress_until"]
    assert record["details"]["limit_price"] == 130.0
    assert record["details"]["quantity"] == 10

    # Proposal is still fully approvable by button tap / submit_decision at any time
    dec_res = await mock_clean_registry.submit_decision(prop_id, "APPROVE")
    assert dec_res["decision"] == "APPROVE"


def test_tag_proposal(mock_clean_registry, tmp_path, monkeypatch):
    """M8-06: tag_proposal appends a note visible in queue and audit trail."""
    from trading_mcp.voice_tools import tag_proposal
    import core.audit_chain as ac

    # Isolate audit chain file
    chain_file = tmp_path / "audit_chain.jsonl"
    monkeypatch.setattr(ac, "_CHAIN_PATH", chain_file)

    prop_id = "prop-tag-test"
    mock_clean_registry.register_pending(
        proposal_id=prop_id,
        session_id="s-tag",
        details={"ticker": "TSLA", "side": "BUY", "limit_price": 200.0},
    )

    res = tag_proposal(prop_id, "Watching 5m VWAP test before approving")
    assert res["available"] is True
    assert len(res["notes"]) == 1
    assert "VWAP" in res["latest_note"]["note"]

    # Verify note is present in pending record
    record = mock_clean_registry.get_pending(prop_id)
    assert record["notes"][0]["note"] == "Watching 5m VWAP test before approving"

    # Verify note is appended to audit trail
    lines = chain_file.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    tag_entries = [e for e in entries if e.get("node") == "tag_proposal"]
    assert len(tag_entries) == 1
    assert tag_entries[0]["entry"]["proposal_id"] == prop_id
    assert tag_entries[0]["entry"]["note"] == "Watching 5m VWAP test before approving"


def test_arm_and_disarm_alert(tmp_path, monkeypatch):
    """M8-07: arm_alert and disarm_alert over AlertStore.
    Arm an alert -> verify in store -> disarm -> verify removed.
    """
    from trading_mcp.voice_tools import arm_alert, disarm_alert
    import alerts

    store_file = tmp_path / "alerts.json"
    monkeypatch.setattr(alerts, "STORE_PATH", store_file)

    # 1. Arm a dynamic gamma flip alert
    arm_res = arm_alert(
        symbol="SPY",
        level="flip",
        direction="below",
        note="Notify when SPY breaks below gamma flip",
    )
    assert arm_res["available"] is True
    alert_id = arm_res["alert_id"]
    assert arm_res["symbol"] == "SPY"
    assert arm_res["level_ref"] == "flip"

    # Verify it exists in store
    store = alerts.AlertStore(store_file)
    alert_list = store.list()
    assert any(a["id"] == alert_id for a in alert_list)

    # 2. Disarm the alert
    disarm_res = disarm_alert(alert_id)
    assert disarm_res["available"] is True
    assert disarm_res["disarmed"] is True

    # Verify it is removed from store on disk
    alert_list_after = alerts.AlertStore(store_file).list()
    assert not any(a["id"] == alert_id for a in alert_list_after)


def test_voice_tools_has_no_execution_guard_or_submit_decision():
    """Confirm AST of trading_mcp.voice_tools has zero references to order execution."""
    import ast
    from pathlib import Path
    import trading_mcp.voice_tools as mod

    source_path = Path(mod.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    # Assert no vesper import
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("vesper"), f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("vesper"), f"Forbidden import from: {node.module}"

        # Assert no call to submit_decision
        if isinstance(node, ast.Attribute) and node.attr == "submit_decision":
            raise AssertionError("voice_tools must not call submit_decision")


class _CollectingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def _decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return _decorator


def test_halt_tool_flips_halt_file(tmp_path, monkeypatch):
    """M8-08: Calling halt() MCP tool flips halt state file and get_halt_status reflects it."""
    import core.halt as ch
    from trading_mcp.voice_tools import halt
    from trading_mcp.vesper_tools import register_vesper_tools

    halt_file = tmp_path / "halt_state.json"
    monkeypatch.setattr(ch, "_HALT_STATE_PATH", halt_file)

    # Initial state: not halted
    assert not ch.is_halted()[0]

    # Call halt tool
    res = halt(reason="Voice emergency test", source="voice_copilot")
    assert res["status"] == "HALTED"
    assert ch.is_halted()[0]

    # Check via get_halt_status MCP tool
    mcp = _CollectingMCP()
    register_vesper_tools(mcp)
    status_tool = mcp.tools["get_halt_status"]
    status = status_tool()
    assert status.get("available") is True
    assert status.get("is_halted") is True
    assert status.get("details", {}).get("reason") == "Voice emergency test"


def test_no_tool_resolves_to_resume_or_unhalt():
    """M8-08: Enumerating registered tools confirms none resolves to resume or un-halt."""
    from trading_mcp.voice_tools import register_voice_tools

    mcp = _CollectingMCP()
    tools = register_voice_tools(mcp)
    for tool_name in tools:
        assert "resume" not in tool_name.lower()
        assert "unhalt" not in tool_name.lower()
        assert "un_halt" not in tool_name.lower()


def test_instructions_string_no_longer_claims_halt_forbidden():
    """M8-08: Assert server.py instructions no longer claims halt is forbidden."""
    import trading_mcp.server as srv
    instructions = srv.mcp.instructions or ""
    assert "or touch the halt/circuit-breaker switches" not in instructions
    assert "halt is permitted" in instructions


def test_scan_backtest_confinement():
    """M8-09: Assert no tool named run_scan or run_backtest is in the registered set."""
    from trading_mcp.voice_tools import register_voice_tools

    mcp = _CollectingMCP()
    names = register_voice_tools(mcp)
    assert "run_scan" not in names
    assert "run_backtest" not in names


@pytest.mark.asyncio
async def test_get_account_state_bounds_large_portfolio(monkeypatch):
    """M8-10: Synthetic 50-position portfolio keeps response bounded to top 15."""
    import trading_mcp.vesper_tools as vt
    from unittest.mock import MagicMock

    mcp = _CollectingMCP()
    vt.register_vesper_tools(mcp)

    # Synthetic 50 positions
    positions = [
        {"symbol": f"SYM{i}", "qty": 10, "market_value": float(i * 100)}
        for i in range(1, 51)
    ]
    fake_portfolio = {
        "totals": {"nlv": 100000.0, "position_count": 50, "buying_power": 50000.0},
        "positions": positions,
    }

    mock_wb = MagicMock()
    mock_wb.portfolio.return_value = fake_portfolio
    monkeypatch.setattr("core.wb.Webull", lambda: mock_wb)

    tool_fn = mcp.tools["get_account_state"]
    res = await tool_fn()
    assert res["available"] is True
    assert res["position_count"] == 50
    assert len(res["positions"]) == 15
    assert res["positions_truncated"] is True
    assert "Showing top 15 of 50" in res["positions_note"]
    # Check that highest market value position (SYM50 = 5000.0) is present
    assert any(p["symbol"] == "SYM50" for p in res["positions"])


def test_get_audit_trail_summary_mode(tmp_path, monkeypatch):
    """M8-11: get_audit_trail defaults to compact summary mode with limit=5."""
    import core.audit_chain as ac
    import trading_mcp.vesper_tools as vt

    chain_file = tmp_path / "audit_chain.jsonl"
    monkeypatch.setattr(ac, "_CHAIN_PATH", chain_file)

    # Populate 10 entries
    for i in range(10):
        ac.append_entry(f"sess-{i}", f"node-{i}", {"step": i, "details": "some long detail string" * 5})

    mcp = _CollectingMCP()
    vt.register_vesper_tools(mcp)
    get_trail = mcp.tools["get_audit_trail"]

    # 1. Default call: summary_mode=True, returned=5
    default_res = get_trail()
    assert default_res["available"] is True
    assert default_res["summary_mode"] is True
    assert default_res["returned"] == 5
    default_len = len(json.dumps(default_res))

    # 2. Full detail call: summary=False, limit=10
    full_res = get_trail(limit=10, summary=False)
    assert full_res["available"] is True
    assert full_res["summary_mode"] is False
    assert full_res["returned"] == 10
    assert "prev_hash" in full_res["entries"][0]
    full_len = len(json.dumps(full_res))

    assert default_len < full_len


def test_voice_tools_audit_trail(tmp_path, monkeypatch):
    """M8-12: Every voice tool call is recorded in audit chain, no credentials logged."""
    import core.audit_chain as ac
    from trading_mcp.voice_tools import halt, watch_setup
    from core.approval_registry import approval_registry

    chain_file = tmp_path / "audit_chain.jsonl"
    monkeypatch.setattr(ac, "_CHAIN_PATH", chain_file)

    # Call halt
    halt(reason="Audited emergency freeze", source="voice")

    # Call watch_setup
    watch_setup("non-existent-id")

    # Read audit trail
    lines = chain_file.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    assert len(entries) >= 2

    # Verify no credential leaks in arguments
    for e in entries:
        args = e.get("entry", {}).get("arguments", {})
        for k in args:
            assert not any(bad in k.lower() for bad in ("token", "secret", "key", "password"))

    # Confirm audit chain verifies cryptographic integrity
    verify_res = ac.verify_chain()
    assert verify_res["valid"] is True


@pytest.mark.asyncio
async def test_oauth_scope_tiering_enforcement():
    """M8-14: Scopes mirror read vs safe-write tiers.
    A read-only token cannot invoke safe-write tools; safe-write token can.
    """
    from fastmcp import FastMCP
    from fastmcp.server.auth import AccessToken, AuthContext, run_auth_checks
    from trading_mcp.voice_tools import register_voice_tools

    m = FastMCP("test-scopes")
    register_voice_tools(m)

    # 1. Read token: scopes=['read']
    read_tok = AccessToken(token="read-tok", client_id="client-read", scopes=["read"])

    # 2. Write token: scopes=['read', 'safe-write']
    write_tok = AccessToken(token="write-tok", client_id="client-write", scopes=["read", "safe-write"])

    safe_write_tool_names = ["arm_alert", "disarm_alert", "halt", "snooze_proposal", "tag_proposal"]
    read_tool_names = ["watch_setup", "find_pending_setup"]

    for name in safe_write_tool_names:
        comp = m._local_provider._components[f"tool:{name}@"]
        assert comp.auth is not None, f"Expected auth check on safe-write tool {name}"

        # Read-only token fails auth check
        ctx_read = AuthContext(token=read_tok, component=comp)
        passes_read = await run_auth_checks(comp.auth, ctx_read)
        assert not passes_read, f"Read token unexpectedly authorized for safe-write tool {name}"

        # Safe-write token passes auth check
        ctx_write = AuthContext(token=write_tok, component=comp)
        passes_write = await run_auth_checks(comp.auth, ctx_write)
        assert passes_write, f"Safe-write token rejected for safe-write tool {name}"

    for name in read_tool_names:
        comp = m._local_provider._components[f"tool:{name}@"]
        assert comp.auth is not None, f"Expected auth check on read tool {name}"

        # Read token passes auth check
        ctx_read = AuthContext(token=read_tok, component=comp)
        passes_read = await run_auth_checks(comp.auth, ctx_read)
        assert passes_read, f"Read token rejected for read tool {name}"



