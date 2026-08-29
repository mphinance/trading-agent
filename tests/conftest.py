"""Test fixtures, and the stubs that keep CI from needing a broker.

Two of sidecar's dependencies are deliberately NOT installed in CI:

- **webull-openapi-python-sdk** pulls in paho-mqtt, which needs a compiler on
  some Pythons, and pins python <3.14. Installing it would make the test matrix
  about the SDK's build rather than about this code.
- **claude-agent-sdk** additionally shells out to a `claude` binary that only
  ships via npm, so importing it proves nothing a test can act on.

Neither is exercised by anything worth testing here — `wb.py` is a thin wrapper
whose interesting behaviour (rate-limit backoff, stale fallback) is reachable
without the SDK, and `chat.py`'s logic is the prompt formatting, not the SDK
call. So both are stubbed at import time and the suite stays fast and hermetic.

Nothing here touches the network. The one test that genuinely hit ntfy.sh
during development is not in the suite: a green build must not depend on a
third-party service being up.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _install_stubs() -> None:
    if "claude_agent_sdk" not in sys.modules:
        sdk = types.ModuleType("claude_agent_sdk")
        for name in ("AssistantMessage", "ClaudeAgentOptions", "ResultMessage",
                     "SystemMessage", "TextBlock", "query"):
            setattr(sdk, name, type(name, (), {}))
        sys.modules["claude_agent_sdk"] = sdk

    if "webull" not in sys.modules:
        for mod in ("webull", "webull.core", "webull.core.client",
                    "webull.trade", "webull.trade.trade_client",
                    "webull.data", "webull.data.data_client",
                    "webull.data.common", "webull.data.common.category"):
            sys.modules[mod] = types.ModuleType(mod)
        sys.modules["webull.core.client"].ApiClient = object
        sys.modules["webull.trade.trade_client"].TradeClient = object
        sys.modules["webull.data.data_client"].DataClient = object

        # md.py imports Category at module level to map a position's
        # instrument_type onto a market-data category. Only the enum member
        # NAMES are used, so a namespace of stand-ins is enough — and keeping
        # it here means md.py needs no test-only branch.
        class _Cat:
            def __init__(self, name): self.name = name

        category = sys.modules["webull.data.common.category"]
        category.Category = type("Category", (), {
            name: _Cat(name) for name in
            ("US_STOCK", "US_ETF", "US_OPTION", "US_CRYPTO", "US_FUTURES",
             "US_EVENT", "HK_STOCK", "HK_ETF", "CN_STOCK", "HK_FUTURES")
        })


_install_stubs()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point the alert store at a temp dir for every test.

    Without this a test run would read and WRITE the developer's real
    ~/.local/state/webull-sidecar/alerts.json. Losing your armed alerts to a
    test run is exactly the kind of thing that only gets noticed later.
    """
    monkeypatch.setenv("SIDECAR_STATE_DIR", str(tmp_path / "state"))
    # notify reads ../.env.notify relative to the repo; make sure a developer's
    # real topic never leaks into a test's view of "configured".
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolated_vesper_state(tmp_path, monkeypatch):
    """Point every Vesper state file (halt, circuit breaker, paper ledger) at
    a temp dir for every test, globally.

    These three modules each use their own hardcoded
    `_DATA_DIR = .../vesper/data/...` (not an env var like alerts.py's
    SIDECAR_STATE_DIR above), and before this fixture existed each test file
    that cared had to remember to monkeypatch all of them itself. That's
    exactly as fragile as it sounds: risk_gate_node started calling
    circuit_breaker.check_portfolio_drawdown() (which calls halt.is_halted())
    unconditionally, and every pre-existing test that invoked risk_gate_node
    without knowing to patch these paths silently wrote real halt/breaker
    state into the repo's actual vesper/data/ directory — which then
    contaminated *other, unrelated* tests reading that same real file later
    in the same run (test order-dependent failures, e.g. a kill-switch test
    asserting "trading disabled" instead saw a leftover real "HALTED" state
    from an earlier test). A single global autouse fixture closes this for
    every current and future test, not just the ones that remember to opt in.
    A test-specific fixture that does the same redirect (there are several)
    is harmless on top of this — monkeypatch just reapplies the same value.
    """
    vesper_data_dir = tmp_path / "vesper_data"
    vesper_data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("vesper.halt._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("vesper.halt._HALT_STATE_PATH", vesper_data_dir / "halt_state.json")
    monkeypatch.setattr("vesper.circuit_breaker._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("vesper.circuit_breaker._STATE_PATH", vesper_data_dir / "circuit_breaker_state.json")
    monkeypatch.setattr("vesper.paper_ledger._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("vesper.paper_ledger._LEDGER_PATH", vesper_data_dir / "paper_ledger.json")
    yield


@pytest.fixture
def levels():
    """A realistic td.levels() payload, shaped like a real SPY response."""
    return {
        "symbol": "SPY", "spot": 746.48, "flip": 745.6, "pin": 743.0,
        "regime": "Positive Gamma",
        "walls": [
            {"strike": 740.0, "net_gex": -5.0e8, "side": "below", "call_oi": 3, "put_oi": 9},
            {"strike": 743.0, "net_gex": -1.38e9, "side": "below", "call_oi": 14801, "put_oi": 44972},
            {"strike": 750.0, "net_gex": 7.45e8, "side": "above", "call_oi": 42347, "put_oi": 4924},
            {"strike": 755.0, "net_gex": 3.8e7, "side": "above", "call_oi": 61728, "put_oi": 167},
        ],
        "key_levels": [{"strike": 743.0, "type": "support", "net_gex": -1.38e9}],
        "expirations": ["2026-07-31"], "as_of": "2026-07-31T20:00:01Z",
    }
