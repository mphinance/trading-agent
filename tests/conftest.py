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

    # Per-user approval allowlists. Two things have to be neutralised, not one:
    # the env vars AND the module-level sets, because vesper/bot/inbound.py and
    # vesper/bot/discord_gateway.py compute theirs ONCE at import time. Clearing
    # only the env var is useless if the module was already imported with it set.
    #
    # This became load-bearing the moment real credentials landed in ./.env: a
    # real TELEGRAM_AUTHORIZED_USER_IDS made every approval test see an
    # allowlist that excluded its fixture user, and only in the full suite --
    # tests passed alone and failed together, the order-dependent shape this
    # file exists to prevent. Tests that DO want an allowlist monkeypatch these
    # same names explicitly, which still works on top of this reset.
    monkeypatch.delenv("TELEGRAM_AUTHORIZED_USER_IDS", raising=False)
    monkeypatch.delenv("DISCORD_AUTHORIZED_USER_IDS", raising=False)
    monkeypatch.setattr("vesper.bot.inbound._AUTHORIZED_TELEGRAM_USER_IDS", set())
    monkeypatch.setattr("vesper.bot.inbound._AUTHORIZED_DISCORD_USER_IDS", set())
    try:
        monkeypatch.setattr("vesper.bot.discord_gateway._AUTHORIZED_USER_IDS", set())
    except (ImportError, AttributeError):
        pass  # discord.py may not be importable in every environment

    # notify.py reads ./.env directly from disk (not just os.environ), so the
    # delenv calls above do not hide a real token from it. Point it at nothing.
    monkeypatch.setattr("notify.ENV_PATHS", ())
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

    # M0-03 moved halt.py/circuit_breaker.py/paper_ledger.py/audit_chain.py to
    # core/ -- patch core.X, not vesper.X. vesper/halt.py is now a thin
    # compat shim (kept only so execution_guard.py's untouched `from
    # vesper.halt import is_halted` still resolves) that re-exports the same
    # function objects, whose __globals__ point at core.halt's namespace; it
    # does not carry its own _DATA_DIR, so patching vesper.halt._DATA_DIR
    # would silently do nothing -- core.halt is the module whose globals the
    # functions actually read from, so that's what must be patched here.
    monkeypatch.setattr("core.halt._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("core.halt._HALT_STATE_PATH", vesper_data_dir / "halt_state.json")
    monkeypatch.setattr("core.circuit_breaker._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("core.circuit_breaker._STATE_PATH", vesper_data_dir / "circuit_breaker_state.json")
    monkeypatch.setattr("core.paper_ledger._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("core.paper_ledger._LEDGER_PATH", vesper_data_dir / "paper_ledger.json")

    # core/audit_chain.py's hash-chained ledger -- same reasoning as the
    # three above: a hardcoded module-level _DATA_DIR that would otherwise
    # read/write the developer's real core/data/audit_chain.jsonl and
    # contaminate cross-test state.
    monkeypatch.setattr("core.audit_chain._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("core.audit_chain._CHAIN_PATH", vesper_data_dir / "audit_chain.jsonl")

    # ApprovalRegistry (vesper/bot/inbound.py) became disk-backed the same
    # way -- same reasoning, same fix.
    monkeypatch.setattr("vesper.bot.inbound._DATA_DIR", vesper_data_dir)
    monkeypatch.setattr("vesper.bot.inbound._APPROVAL_STATE_PATH", vesper_data_dir / "approval_registry_state.json")

    # vesper/metrics.py's cross-process snapshot file (Health/observability
    # metrics module) -- same reasoning as the state files above: a
    # hardcoded module-level _DATA_DIR that would otherwise read/write the
    # developer's real vesper/data/metrics_snapshot.json.
    import vesper.metrics as _metrics_module
    monkeypatch.setattr(_metrics_module, "_DATA_DIR", vesper_data_dir)
    monkeypatch.setattr(_metrics_module, "_SNAPSHOT_PATH", vesper_data_dir / "metrics_snapshot.json")

    # vesper/graph.py's persistent checkpointer -- redirect the sqlite path
    # AND reset the process-lifetime connection/saver singletons, since a
    # real connection opened by an earlier test would otherwise point at a
    # tmp_path from a PREVIOUS test that no longer exists.
    import vesper.graph as _graph_module
    monkeypatch.setattr(_graph_module, "_DATA_DIR", vesper_data_dir)
    monkeypatch.setattr(_graph_module, "_CHECKPOINT_DB_PATH", vesper_data_dir / "checkpoints.sqlite")
    monkeypatch.setattr(_graph_module, "_sqlite_conn", None)
    monkeypatch.setattr(_graph_module, "_sqlite_saver", None)
    yield


@pytest.fixture(autouse=True)
def _isolated_metrics_state():
    """Reset vesper/metrics.py's process-wide `metrics` singleton before
    every test.

    Unlike the disk-backed state above, this is in-memory and NOT redirected
    by tmp_path -- every module that instruments itself does `from
    vesper.metrics import metrics`, binding its own reference to the one
    object, so a test-order leak here (one test's record_broker_call calls
    showing up in another test's snapshot() assertions) would be exactly the
    kind of order-dependent failure _isolated_vesper_state's own docstring
    describes for disk state. metrics.reset() clears state on the existing
    object rather than replacing it, which is what keeps every already-bound
    reference in sync -- see reset()'s own docstring.
    """
    from vesper.metrics import metrics as _metrics_singleton
    _metrics_singleton.reset()
    yield
    _metrics_singleton.reset()


@pytest.fixture(autouse=True)
def _isolated_sector_cache(monkeypatch):
    """vesper/sector.py's _SECTOR_CACHE is a module-level, in-process dict
    (deliberately NOT a disk-backed state file -- sector classification is
    static metadata, not market data, so it's cached for the life of the
    process; see vesper/sector.py's own docstring). Reset it fresh for every
    test so a mocked or unresolvable sector value cached by one test can
    never leak into another within the same pytest process -- the same
    order-dependent-leak concern _isolated_vesper_state above exists to
    prevent, just for an in-memory dict instead of an on-disk file. A
    test-specific reset (there are several in tests/test_sector_concentration.py)
    is harmless on top of this, same as the note on _isolated_vesper_state.
    """
    monkeypatch.setattr("vesper.sector._SECTOR_CACHE", {})


@pytest.fixture(autouse=True)
def _stub_sector_network_lookup(monkeypatch):
    """vesper.sector.get_sector calls out to yfinance over the network on a
    cache miss, which the suite's hermetic contract (no network, no broker,
    no credentials -- see CLAUDE.md's Tests section) forbids. risk_gate_node
    calls get_sector for every proposal that adds exposure (the sector-
    concentration bucket in vesper/risk.py), so without this, any test that
    drafts an ordinary BUY proposal through risk_gate_node would silently
    depend on network access and, offline, get None back -- which the bucket
    correctly fails closed on, incidentally rejecting proposals that have
    nothing to do with sector logic.

    Stubbed at the yfinance.Ticker level, not by replacing get_sector itself,
    so get_sector's real caching logic still runs under test everywhere;
    tests that exercise sector logic directly (tests/test_sector_concentration.py)
    layer their own patch("yfinance.Ticker", ...) / patch("vesper.sector.get_sector", ...)
    on top of this inside their own `with` block. The stand-in sector name is
    deliberately not a real GICS sector (so it can't be mistaken for one) and
    is a stable per-ticker value, so two different tickers never collide into
    the same sector bucket by accident.
    """
    class _StubYfinanceTicker:
        def __init__(self, ticker):
            self._ticker = ticker

        @property
        def info(self):
            return {"sector": f"TEST_SECTOR_{self._ticker.upper()}"}

    monkeypatch.setattr("yfinance.Ticker", _StubYfinanceTicker)


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
