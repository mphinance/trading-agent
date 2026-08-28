"""Whop Commercial Licensing & Membership Entitlement Gateway for Vesper / Trader Lady."""

from __future__ import annotations

import os
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)

WHOP_API_BASE = "https://api.whop.com/v5"


class WhopClient:
    """Whop Membership & License Verification Client."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("WHOP_API_KEY", "")
        self._client = httpx.Client(
            base_url=WHOP_API_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Vesper-TraderLady-WhopGateway/1.0",
            },
            timeout=10.0,
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "WhopClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("your_"))

    def validate_license(self, license_key: str) -> Dict[str, Any]:
        """Validate a user's Whop software license key."""
        if not self.configured:
            return {"valid": False, "reason": "WHOP_API_KEY not configured"}
        try:
            resp = self._client.post(
                f"/memberships/validate_license",
                json={"license_key": license_key},
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("valid"):
                return {
                    "valid": True,
                    "membership_id": data.get("membership_id"),
                    "user_id": data.get("user_id"),
                    "email": data.get("email"),
                    "status": data.get("status"),
                }
            return {"valid": False, "reason": data.get("error", "Invalid license")}
        except Exception as e:
            logger.error(f"Whop license validation failed: {e}")
            return {"valid": False, "reason": str(e)}

    def check_user_access(self, user_id: str, product_id: str) -> bool:
        """Verify if a user has an active membership for a specific product."""
        if not self.configured:
            return False
        try:
            resp = self._client.get(f"/users/{user_id}/memberships")
            if resp.status_code == 200:
                memberships = resp.json().get("data", [])
                for m in memberships:
                    if m.get("product_id") == product_id and m.get("status") == "active":
                        return True
            return False
        except Exception as e:
            logger.error(f"Whop user access check failed: {e}")
            return False
