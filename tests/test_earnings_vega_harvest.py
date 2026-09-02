"""Unit tests for the Earnings-Week CSP Vega Harvest playbook.

Sells an ATM CSP the day before an AMC (after-market-close) report or the
same day for a BMO (before-market-open) report, tagged with earnings_exit_date
so monitor.py force-closes it once the IV crush has happened (see
tests/test_monitor.py's EARNINGS_EXIT tests for that half).
"""

from __future__ import annotations

from unittest.mock import patch
import pytest

from vesper.nodes.playbooks import _fetch_upcoming_earnings, playbooks_node
from vesper.state import OrderProposal, TradingState


def _make_state(selected_playbook: str = "earnings_vega") -> TradingState:
    return {
        "session_id": "test-earnvega-sess",
        "selected_playbook": selected_playbook,
        "candidates": [],
        "technicals": {},
        "options_audits": {},
        "proposals": [],
        "risk_assessments": {},
        "needs_human_approval": False,
        "audit_trail": [],
    }


def _amc_entry(symbol="MDB", earnings_date="2026-09-01", expected_move=15.9):
    return {
        "event": {
            "symbol": symbol,
            "earningsDate": earnings_date,
            "earningsTime": "AMC",
            "expectedMovePct": expected_move,
        }
    }


def _bmo_entry(symbol="NIO", earnings_date="2026-09-01", expected_move=4.23):
    return {
        "event": {
            "symbol": symbol,
            "earningsDate": earnings_date,
            "earningsTime": "BMO",
            "expectedMovePct": expected_move,
        }
    }


# ── _fetch_upcoming_earnings ─────────────────────────────────────────────────

def test_fetch_upcoming_earnings_returns_list_when_configured():
    from unittest.mock import MagicMock
    with patch("core.td.TDPro") as mock_td_cls:
        mock_td = MagicMock()
        mock_td.configured = True
        mock_td.call.return_value = {"earnings": [_amc_entry()]}
        mock_td_cls.return_value = mock_td

        res = _fetch_upcoming_earnings()
        assert res == [_amc_entry()]
        mock_td.call.assert_called_once_with("get_earnings_flow", {})


def test_fetch_upcoming_earnings_returns_none_when_unconfigured():
    from unittest.mock import MagicMock
    with patch("core.td.TDPro") as mock_td_cls:
        mock_td_cls.return_value = MagicMock(configured=False)
        assert _fetch_upcoming_earnings() is None


def test_fetch_upcoming_earnings_returns_none_on_malformed_response():
    from unittest.mock import MagicMock
    with patch("core.td.TDPro") as mock_td_cls:
        mock_td = MagicMock()
        mock_td.configured = True
        mock_td.call.return_value = "not a dict"
        mock_td_cls.return_value = mock_td
        assert _fetch_upcoming_earnings() is None


# ── playbooks_node drafting ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drafts_amc_csp_one_day_before_with_exit_date_the_next_day():
    """AMC report on 2026-09-02 -> draft "today" 2026-09-01, exit "2026-09-03"."""
    state = _make_state()
    with patch("vesper.nodes.playbooks.datetime") as mock_dt:
        import datetime as real_datetime
        mock_dt.now.return_value = real_datetime.datetime(2026, 9, 1, 12, 0, tzinfo=real_datetime.timezone.utc)
        mock_dt.strptime = real_datetime.datetime.strptime
        mock_dt.timezone = real_datetime.timezone

        with patch("vesper.nodes.playbooks._fetch_upcoming_earnings",
                   return_value=[_amc_entry(symbol="MDB", earnings_date="2026-09-02")]):
            with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=280.0):
                with patch("vesper.nodes.playbooks._fetch_live_option_quote", return_value=3.45):
                    res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    p: OrderProposal = props[0]
    assert p.ticker == "MDB"
    assert p.side == "SELL"
    assert p.asset_type == "OPTION"
    assert p.option_type == "put"
    assert p.strike == 280.0
    assert p.limit_price == 3.45
    assert p.earnings_exit_date == "2026-09-03"  # AMC: exit the day AFTER the report
    assert p.estimated_cost == 28000.0
    assert p.max_risk == 28000.0

    notes = res["audit_trail"][0]["notes"]
    assert any("Drafted Earnings Vega Harvest CSP for MDB" in n and "force-exit 2026-09-03" in n for n in notes)


@pytest.mark.asyncio
async def test_drafts_bmo_csp_same_day_with_exit_date_same_day():
    """BMO report on 2026-09-02 -> draft "today" 2026-09-02, exit "2026-09-02" (same day)."""
    state = _make_state()
    with patch("vesper.nodes.playbooks.datetime") as mock_dt:
        import datetime as real_datetime
        mock_dt.now.return_value = real_datetime.datetime(2026, 9, 2, 8, 0, tzinfo=real_datetime.timezone.utc)
        mock_dt.strptime = real_datetime.datetime.strptime
        mock_dt.timezone = real_datetime.timezone

        with patch("vesper.nodes.playbooks._fetch_upcoming_earnings",
                   return_value=[_bmo_entry(symbol="NIO", earnings_date="2026-09-02")]):
            with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=5.00):
                with patch("vesper.nodes.playbooks._fetch_live_option_quote", return_value=0.30):
                    res = await playbooks_node(state)

    props = res["proposals"]
    assert len(props) == 1
    assert props[0].ticker == "NIO"
    assert props[0].earnings_exit_date == "2026-09-02"  # BMO: exit the SAME day


@pytest.mark.asyncio
async def test_skips_entry_too_far_from_earnings():
    """AMC report 5 days out must not draft yet -- only the day before."""
    state = _make_state()
    with patch("vesper.nodes.playbooks.datetime") as mock_dt:
        import datetime as real_datetime
        mock_dt.now.return_value = real_datetime.datetime(2026, 9, 1, 12, 0, tzinfo=real_datetime.timezone.utc)
        mock_dt.strptime = real_datetime.datetime.strptime
        mock_dt.timezone = real_datetime.timezone

        with patch("vesper.nodes.playbooks._fetch_upcoming_earnings",
                   return_value=[_amc_entry(symbol="MDB", earnings_date="2026-09-06")]):
            res = await playbooks_node(state)

    assert len(res["proposals"]) == 0


@pytest.mark.asyncio
async def test_skips_when_earnings_calendar_unavailable():
    state = _make_state()
    with patch("vesper.nodes.playbooks._fetch_upcoming_earnings", return_value=None):
        res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("earnings calendar unavailable" in n for n in notes)


@pytest.mark.asyncio
async def test_skips_when_no_live_equity_quote():
    state = _make_state()
    with patch("vesper.nodes.playbooks.datetime") as mock_dt:
        import datetime as real_datetime
        mock_dt.now.return_value = real_datetime.datetime(2026, 9, 1, 12, 0, tzinfo=real_datetime.timezone.utc)
        mock_dt.strptime = real_datetime.datetime.strptime
        mock_dt.timezone = real_datetime.timezone

        with patch("vesper.nodes.playbooks._fetch_upcoming_earnings",
                   return_value=[_amc_entry(symbol="MDB", earnings_date="2026-09-02")]):
            with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=None):
                res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped Earnings Vega Harvest for MDB" in n and "no live equity quote" in n for n in notes)


@pytest.mark.asyncio
async def test_skips_when_no_live_option_quote():
    state = _make_state()
    with patch("vesper.nodes.playbooks.datetime") as mock_dt:
        import datetime as real_datetime
        mock_dt.now.return_value = real_datetime.datetime(2026, 9, 1, 12, 0, tzinfo=real_datetime.timezone.utc)
        mock_dt.strptime = real_datetime.datetime.strptime
        mock_dt.timezone = real_datetime.timezone

        with patch("vesper.nodes.playbooks._fetch_upcoming_earnings",
                   return_value=[_amc_entry(symbol="MDB", earnings_date="2026-09-02")]):
            with patch("vesper.nodes.playbooks._fetch_live_quote", return_value=280.0):
                with patch("vesper.nodes.playbooks._fetch_live_option_quote", return_value=None):
                    res = await playbooks_node(state)

    assert len(res["proposals"]) == 0
    notes = res["audit_trail"][0]["notes"]
    assert any("Skipped Earnings Vega Harvest for MDB" in n and "no live option quote" in n for n in notes)


@pytest.mark.asyncio
async def test_malformed_entries_are_skipped_not_crashed():
    state = _make_state()
    bad_entries = [
        {"event": {"symbol": "", "earningsDate": "2026-09-02", "earningsTime": "AMC"}},  # no symbol
        {"event": {"symbol": "MDB", "earningsDate": None, "earningsTime": "AMC"}},  # no date
        {"event": {"symbol": "MDB", "earningsDate": "not-a-date", "earningsTime": "AMC"}},  # bad date
        {"not_event": {}},  # wrong shape
        "not even a dict",
    ]
    with patch("vesper.nodes.playbooks._fetch_upcoming_earnings", return_value=bad_entries):
        res = await playbooks_node(state)  # must not raise
    assert len(res["proposals"]) == 0
