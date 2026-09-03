"""Hermetic tests for core/schema.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from core.schema import SignalResult


class TestSignalResult:
    def test_success_factory(self):
        payload = {"ticker": "AAPL", "rsi": 55.0}
        res = SignalResult.success(data=payload)
        assert res.status == "success"
        assert res.data == payload
        assert res.metadata == {}
        assert res.error is None

        # With metadata
        meta = {"source": "test", "latency_ms": 12}
        res_meta = SignalResult.success(data=payload, metadata=meta)
        assert res_meta.metadata == meta

    def test_error_msg_factory(self):
        res = SignalResult.error_msg("Failed to fetch data")
        assert res.status == "error"
        assert res.error == "Failed to fetch data"
        assert res.data is None
        assert res.metadata == {}

        # With metadata
        res_meta = SignalResult.error_msg("Failed", metadata={"attempt": 2})
        assert res_meta.metadata == {"attempt": 2}

    def test_serialization(self):
        res = SignalResult.success({"score": 95}, metadata={"v": 1})
        dumped = res.model_dump()
        assert dumped["status"] == "success"
        assert dumped["data"] == {"score": 95}
        assert dumped["metadata"] == {"v": 1}
        assert dumped["error"] is None

    def test_validation_error_on_missing_status(self):
        with pytest.raises(ValidationError):
            SignalResult()
