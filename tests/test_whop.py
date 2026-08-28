"""Tests for Whop Commercial Licensing Client."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from vesper.whop import WhopClient


def test_whop_client_unconfigured_handling(monkeypatch):
    """Verify WhopClient reports unconfigured when key is missing or placeholder."""
    monkeypatch.delenv("WHOP_API_KEY", raising=False)
    with WhopClient() as client:
        assert not client.configured
        res = client.validate_license("test-license-key")
        assert res["valid"] is False
        assert "not configured" in res["reason"]
        assert client.check_user_access("user_123", "prod_456") is False


def test_whop_client_valid_and_invalid_license(monkeypatch):
    """Verify WhopClient parses valid and invalid license responses."""
    monkeypatch.setenv("WHOP_API_KEY", "whop_live_valid_key")

    with WhopClient() as client:
        assert client.configured

        # Mock valid license response
        mock_resp_valid = MagicMock()
        mock_resp_valid.status_code = 200
        mock_resp_valid.json.return_value = {
            "valid": True,
            "membership_id": "mem_123",
            "user_id": "usr_456",
            "email": "trader@example.com",
            "status": "active",
        }

        with patch.object(client._client, "post", return_value=mock_resp_valid):
            res = client.validate_license("valid-lic-123")
            assert res["valid"] is True
            assert res["email"] == "trader@example.com"

        # Mock invalid license response
        mock_resp_invalid = MagicMock()
        mock_resp_invalid.status_code = 400
        mock_resp_invalid.json.return_value = {"valid": False, "error": "License expired"}

        with patch.object(client._client, "post", return_value=mock_resp_invalid):
            res_inv = client.validate_license("expired-lic")
            assert res_inv["valid"] is False
            assert "License expired" in res_inv["reason"]
