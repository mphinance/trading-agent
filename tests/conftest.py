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
                    "webull.data", "webull.data.data_client"):
            sys.modules[mod] = types.ModuleType(mod)
        sys.modules["webull.core.client"].ApiClient = object
        sys.modules["webull.trade.trade_client"].TradeClient = object
        sys.modules["webull.data.data_client"].DataClient = object


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
