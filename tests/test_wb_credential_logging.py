"""The Webull SDK must never install its own loggers.

TradeClient._init_logger (and DataClient's equivalent) auto-attach a stdout
stream logger AND a file logger unless a flag is already set on the ApiClient.
On any request error those sinks log the entire request including headers --
so `x-app-key`, the live Webull app key, lands on stdout and in
webull_trade_sdk.log in plaintext. Observed 2026-08-29 from a single rejected
order-preview call. The user streams this work (CLAUDE.md rule 5), so a
credential on stdout is a credential on camera.
"""

from __future__ import annotations

import wb


class _FakeApiClient:
    """Stands in for the SDK ApiClient. Records whether the SDK would have been
    allowed to install loggers, and blows up if anyone actually calls the
    logger setters."""

    def __init__(self, *a, **k):
        self.endpoints = []

    def add_endpoint(self, *a, **k):
        self.endpoints.append(a)

    def set_stream_logger(self, *a, **k):
        raise AssertionError("SDK installed a stdout logger — it dumps x-app-key on error")

    def set_file_logger(self, *a, **k):
        raise AssertionError("SDK installed a file logger — it writes x-app-key in plaintext")


def _build(monkeypatch):
    monkeypatch.setattr(wb, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(wb, "TradeClient", lambda c: object())
    monkeypatch.setattr(wb, "DataClient", lambda c: object())
    monkeypatch.setattr(wb, "credentials", lambda: ("key", "secret", "us"))
    return wb.Webull()


def test_sdk_logger_installation_is_suppressed(monkeypatch):
    client = _build(monkeypatch)._api
    assert client._stream_logger_set is True
    assert client._file_logger_set is True


def test_both_flags_set_not_just_one(monkeypatch):
    """The SDK's guard is `if not stream_set and not file_set` -- an AND -- so
    one flag technically suffices. Both are asserted anyway: relying on the
    shape of a third party's boolean condition is exactly the kind of thing
    that breaks silently on an SDK bump, and the failure mode is a leaked
    credential rather than a crash."""
    client = _build(monkeypatch)._api
    assert client._stream_logger_set and client._file_logger_set


def test_flags_are_set_before_the_sdk_clients_are_constructed(monkeypatch):
    """Order matters: TradeClient reads the flags in its own __init__, so
    setting them after construction would be too late. The fake's setters
    raise, so this test fails loudly if the ordering regresses."""
    seen = {}

    def _trade(c):
        seen["stream"] = getattr(c, "_stream_logger_set", False)
        seen["file"] = getattr(c, "_file_logger_set", False)
        return object()

    monkeypatch.setattr(wb, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(wb, "TradeClient", _trade)
    monkeypatch.setattr(wb, "DataClient", lambda c: object())
    monkeypatch.setattr(wb, "credentials", lambda: ("key", "secret", "us"))
    wb.Webull()

    assert seen == {"stream": True, "file": True}, (
        "flags must be set BEFORE TradeClient is constructed, or it installs its loggers first"
    )
