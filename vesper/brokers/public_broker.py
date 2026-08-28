"""Public.com Broker Integration for Vesper."""

from __future__ import annotations

import os
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)


class PublicBrokerClient:
    """Public.com Trading & Agentic Brokerage API Client."""

    BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "https://api.public.com")

    def __init__(
        self,
        api_key: Optional[str] = None,
        account_id: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("PUBLIC_API_SECRET_KEY", "")
        self.account_id = account_id or os.getenv("PUBLIC_ACCOUNT_ID", "")
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Vesper-Quant-Agent/1.0",
            },
            timeout=15.0,
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "PublicBrokerClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def configured(self) -> bool:
        """Returns True if the API key is present."""
        return bool(self.api_key and not self.api_key.startswith("your_"))

    def get_accounts(self) -> Dict[str, Any]:
        """Fetch list of user accounts on Public."""
        if not self.configured:
            return {"status": "error", "message": "Public.com API key not configured."}
        try:
            resp = self._client.get("/v1/accounts")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch Public.com accounts: {e}")
            return {"status": "error", "message": str(e)}

    def get_portfolio(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetch portfolio positions and balances."""
        acc = account_id or self.account_id
        if not self.configured or not acc:
            return {"status": "error", "message": "Public.com API key or Account ID missing."}
        try:
            resp = self._client.get(f"/v1/accounts/{acc}/portfolio")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch Public.com portfolio: {e}")
            return {"status": "error", "message": str(e)}

    def preview_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LIMIT",
        limit_price: Optional[float] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Preview an equity/option order on Public.com before execution."""
        if not self.configured:
            return {
                "status": "dry_run",
                "message": "Public.com key unconfigured. Simulating order preview.",
                "data": {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "estimated_cost": (limit_price or 0.0) * quantity,
                }
            }
        try:
            payload = {
                "accountId": account_id or self.account_id,
                "symbol": symbol,
                "side": side.upper(),
                "type": order_type.upper(),
                "quantity": quantity,
                "limitPrice": limit_price,
            }
            resp = self._client.post("/v1/orders/preview", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to preview order on Public.com: {e}")
            return {"status": "error", "message": str(e)}

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LIMIT",
        limit_price: Optional[float] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a live order to Public.com."""
        if not self.configured:
            return {
                "status": "error",
                "message": "Cannot place live order: Public.com API secret key not configured in .env",
            }
        try:
            payload = {
                "accountId": account_id or self.account_id,
                "symbol": symbol,
                "side": side.upper(),
                "type": order_type.upper(),
                "quantity": quantity,
                "limitPrice": limit_price,
                "timeInForce": "DAY",
            }
            resp = self._client.post("/v1/orders", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to place order on Public.com: {e}")
            return {"status": "error", "message": str(e)}
