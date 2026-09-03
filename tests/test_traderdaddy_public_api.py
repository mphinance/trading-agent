"""Pins that core/traderdaddy.py speaks to the CUSTOMER API, not the internal one.

Before 2026-09-03 this module called `/api/agent/*` — the internal superuser
namespace behind the shared `AGENT_API_KEY` — and authenticated with an
email/password JWT. That was three problems at once: it could not be configured
on an OAuth-only account (so every TDPro tool on the deployed server was dead),
it depended on a master credential, and a module that speaks to a superuser
namespace can never ship in a public repo.

These tests pin the fix at the level that matters: the URL that goes on the wire
and the credential that goes in the header. A regression here is not a style
regression — it re-points the public tool surface at an internal namespace.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.traderdaddy as td

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (REPO_ROOT / "core" / "traderdaddy.py").read_text()

# Deliberately short and low-entropy: long enough to be a plausible fixture,
# short enough that scripts/scan_secrets.sh does not read it as a real key.
# The scanner blocked this file on first commit with a realistic-looking value,
# which is the correct behaviour — the fix is to make the placeholder read as
# one, never to loosen the pattern.
FAKE_KEY = "td_live_fake_test_key"


class _Resp:
    status_code = 200

    def json(self):
        return {"ok": True}

    def raise_for_status(self):
        return None


class _SpyClient:
    """Records the URL and headers of the last request."""

    def __init__(self):
        self.url = None
        self.headers = None

    async def get(self, url, params=None, headers=None):
        self.url, self.headers = url, headers
        return _Resp()

    async def post(self, url, json=None, headers=None):
        self.url, self.headers = url, headers
        return _Resp()


@pytest.fixture
def spy(monkeypatch):
    client = _SpyClient()
    monkeypatch.setattr(td, "_get_client", lambda: client)
    monkeypatch.setenv("TD_API_KEY", FAKE_KEY)
    monkeypatch.delenv("TRADERDADDY_API_URL", raising=False)
    # A previous test may have tripped the breaker; start CLOSED.
    monkeypatch.setattr(
        td, "_circuit",
        {"state": "CLOSED", "failures": 0, "last_failure": 0.0,
         "open_until": 0.0, "backoff_multiplier": 1},
    )
    return client


async def test_get_targets_the_public_v1_namespace(spy):
    await td._agent_get("market-pulse")
    assert "/api/v1/market-pulse" in spy.url
    assert "/api/agent/" not in spy.url, (
        "core/traderdaddy.py is calling the INTERNAL superuser namespace again"
    )


async def test_post_targets_the_public_v1_namespace(spy):
    await td._agent_post("market-stats/refresh")
    assert "/api/v1/market-stats/refresh" in spy.url
    assert "/api/agent/" not in spy.url


async def test_the_api_key_is_sent_not_a_jwt(spy):
    await td._agent_get("market-pulse")
    assert spy.headers["X-API-Key"] == FAKE_KEY
    assert spy.headers["Authorization"] == f"Bearer {FAKE_KEY}"


async def test_a_missing_key_is_a_clear_error_not_a_crash(spy, monkeypatch):
    monkeypatch.delenv("TD_API_KEY", raising=False)
    monkeypatch.delenv("TDPRO_API_KEY", raising=False)
    result = await td._agent_get("market-pulse")
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    assert payload["status"] == "error"
    assert "TD_API_KEY" in payload["error"]


async def test_tdpro_api_key_is_accepted_as_a_fallback_name(spy, monkeypatch):
    monkeypatch.delenv("TD_API_KEY", raising=False)
    monkeypatch.setenv("TDPRO_API_KEY", FAKE_KEY)
    await td._agent_get("market-pulse")
    assert spy.headers["X-API-Key"] == FAKE_KEY


async def test_it_defaults_to_the_public_host(spy):
    """The old code had no default, so a box that never set TRADERDADDY_API_URL
    failed every single call — which is exactly what production did."""
    await td._agent_get("market-pulse")
    assert spy.url.startswith("https://api.traderdaddy.pro/api/v1/")


@pytest.mark.parametrize("path", ["alerts", "alerts/summary",
                                  "most-institutionally-traded-tickers/aggregated"])
async def test_paths_with_no_public_equivalent_fail_loudly(spy, path):
    """These exist only on /api/agent. None is a registered MCP tool, so the
    right behaviour is an explanation rather than a bare 404."""
    result = await td._agent_get(path)
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    assert payload["status"] == "error"
    assert "no /api/v1 equivalent" in payload["error"]
    assert spy.url is None, "it should not have hit the network at all"


def test_the_jwt_login_path_is_gone():
    """`_login` posted TRADERDADDY_EMAIL/PASSWORD for a superuser JWT. An API key
    cannot expire and cannot be refreshed, so re-introducing a login here would
    mean the internal namespace came back with it."""
    tree = ast.parse(SOURCE)
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_login" not in names
    assert "_get_token" not in names
    for var in ("TRADERDADDY_EMAIL", "TRADERDADDY_PASSWORD"):
        assert f'getenv("{var}"' not in SOURCE, f"{var} is being read again"


def test_no_call_site_builds_an_api_agent_url():
    """Belt and braces on the two behavioural tests above: no f-string anywhere
    in the module assembles the internal namespace."""
    offenders = [
        line for line in SOURCE.splitlines()
        if "/api/agent/" in line and not line.lstrip().startswith(("#", "*", '"', "'"))
        and "no /api/v1 equivalent" not in line
    ]
    assert not offenders, f"internal-namespace URL construction found: {offenders}"
