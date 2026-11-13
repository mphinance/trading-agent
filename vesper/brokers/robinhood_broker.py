"""Robinhood broker adapter for Vesper (equities only, agentic account only).

Talks to Robinhood's hosted MCP server (see robinhood_auth.py for login). Same
surface as PublicBrokerClient so executor.py can treat brokers alike, and, like
every broker here, it never decides anything: vesper.execution_guard previews and
gates the order, and this module only carries out what the guard released.

Fences, enforced in code (not by prompt):
  * Only the account in ROBINHOOD_ACCOUNT_ID is ever traded, and only after
    get_accounts confirms Robinhood itself marks it agentic_allowed. Robinhood
    rejects other accounts server-side too; this is the second lock.
  * Equities only. Anything that is not a plain stock/ETF order is refused here.
  * place_equity_order may come back awaiting approval in the Robinhood app.
    That is a second human gate on top of Vesper's Telegram/Discord card, and it
    is reported as status "pending_robinhood_approval", never as a fill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

Transport = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class RobinhoodError(RuntimeError):
    """Raised when Robinhood or the fences refuse a request."""


def _unwrap(result: Any) -> Dict[str, Any]:
    """MCP CallToolResult -> the payload under Robinhood's {"data", "guide"} envelope."""
    if getattr(result, "isError", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise RobinhoodError(text or "Robinhood tool call failed")
    payload = getattr(result, "structuredContent", None)
    if not payload:
        text = "".join(getattr(c, "text", "") for c in result.content)
        payload = json.loads(text) if text else {}
    return payload.get("data", payload) if isinstance(payload, dict) else {"data": payload}


def _mcp_transport(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """One short-lived MCP session per call. Sync on purpose: the executor calls
    brokers from asyncio.to_thread, where asyncio.run is safe."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from vesper.brokers.robinhood_auth import RH_MCP_URL, build_provider

    async def _run() -> Dict[str, Any]:
        async with streamablehttp_client(RH_MCP_URL, auth=build_provider()) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return _unwrap(await session.call_tool(name, args))

    return asyncio.run(_run())


class RobinhoodBrokerClient:
    def __init__(self, account_id: Optional[str] = None, transport: Optional[Transport] = None):
        self.account_id = account_id or os.getenv("ROBINHOOD_ACCOUNT_ID", "")
        self._call = transport or _mcp_transport
        self._injected = transport is not None
        self._verified = False

    def close(self) -> None:  # parity with PublicBrokerClient; sessions are per-call
        pass

    def __enter__(self) -> "RobinhoodBrokerClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def configured(self) -> bool:
        if not self.account_id:
            return False
        if self._injected:
            return True
        from vesper.brokers.robinhood_auth import FileTokenStorage

        return FileTokenStorage().logged_in()

    # -- fence -------------------------------------------------------------
    def _require_agentic_account(self) -> str:
        if not self.configured:
            raise RobinhoodError(
                "Robinhood not configured: set ROBINHOOD_ACCOUNT_ID and run "
                "`python -m vesper.brokers.robinhood_auth login`"
            )
        if not self._verified:
            accounts = self._call("get_accounts", {}).get("accounts", [])
            match = next((a for a in accounts if a.get("account_number") == self.account_id), None)
            if match is None or match.get("agentic_allowed") is not True:
                raise RobinhoodError(
                    "ROBINHOOD_ACCOUNT_ID is not an agentic-enabled account; refusing to trade it"
                )
            self._verified = True
        return self.account_id

    # -- reads -------------------------------------------------------------
    def get_accounts(self) -> Dict[str, Any]:
        try:
            return self._call("get_accounts", {})
        except Exception as e:
            logger.error("Robinhood get_accounts failed: %s", e)
            return {"status": "error", "message": str(e)}

    def get_portfolio(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            return self._call("get_portfolio", {"account_number": account_id or self.account_id})
        except Exception as e:
            logger.error("Robinhood get_portfolio failed: %s", e)
            return {"status": "error", "message": str(e)}

    def get_buying_power(self, account_id: Optional[str] = None) -> Optional[float]:
        bp = self.get_portfolio(account_id).get("buying_power")
        raw = bp.get("buying_power") if isinstance(bp, dict) else bp
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # -- orders ------------------------------------------------------------
    @staticmethod
    def _order_args(
        symbol: str, side: str, quantity: float, order_type: str, limit_price: Optional[float],
        asset_type: str,
    ) -> Dict[str, Any]:
        if str(asset_type).upper() not in ("EQUITY", "STOCK", "ETF"):
            raise RobinhoodError(
                f"Robinhood adapter trades equities only; refusing {asset_type} order for {symbol}"
            )
        otype = order_type.lower()
        if otype not in ("market", "limit"):
            raise RobinhoodError(f"unsupported order type {order_type!r} (market or limit only)")
        if otype == "limit" and not limit_price:
            raise RobinhoodError("limit order needs limit_price")
        args: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.lower(),
            "type": otype,
            "quantity": str(quantity),
            "time_in_force": "gfd",
        }
        if otype == "limit":
            args["limit_price"] = f"{float(limit_price):.2f}"
        return args

    def preview_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "LIMIT",
        limit_price: Optional[float] = None,
        account_id: Optional[str] = None,
        asset_type: str = "EQUITY",
    ) -> Dict[str, Any]:
        """Simulate via review_equity_order. Places nothing."""
        acct = self._require_agentic_account()
        args = self._order_args(symbol, side, quantity, order_type, limit_price, asset_type)
        return self._call("review_equity_order", {"account_number": acct, **args})

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "LIMIT",
        limit_price: Optional[float] = None,
        account_id: Optional[str] = None,
        asset_type: str = "EQUITY",
        ref_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit via place_equity_order. Pass the same ref_id when retrying a
        transport failure so a retry cannot double-place."""
        acct = self._require_agentic_account()
        args = self._order_args(symbol, side, quantity, order_type, limit_price, asset_type)
        res = self._call(
            "place_equity_order",
            {"account_number": acct, "ref_id": ref_id or str(uuid.uuid4()), **args},
        )
        if isinstance(res, dict) and res.get("customer_approval_required"):
            res = {**res, "status": "pending_robinhood_approval"}
        return res
