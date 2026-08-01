"""Delivery channels, fan-out, and the rule that the topic never leaks.

Nothing here touches the network — every channel is either unconfigured or a
local double. The live ntfy.sh check stays a manual step; a green build must
not depend on a third-party service being up.
"""

from __future__ import annotations

import pytest

import notify


class Recorder(notify._Channel):
    name = "recorder"
    configured = True

    def __init__(self):
        self.sent = []

    def status(self):
        return {"name": self.name, "configured": True}

    def send(self, text, title=""):
        self.sent.append((title, text))
        return True


class Dead(notify._Channel):
    name = "dead"
    configured = True

    def status(self):
        return {"name": self.name, "configured": True}

    def send(self, text, title=""):
        return False


TOPIC = "sidecar-deadbeefcafe0123456789abcdef01"


# --------------------------------------------------------------------------
# The topic is a credential
# --------------------------------------------------------------------------

def test_status_never_returns_the_topic():
    """This panel is streamed. A topic read off a frame is someone else's feed."""
    n = notify.Ntfy({"NTFY_TOPIC": TOPIC})
    assert n.configured
    assert TOPIC not in str(n.status())
    assert "deadbeef" not in str(n.status())


def test_notifier_status_never_returns_the_topic():
    nf = notify.Notifier(channels=[notify.Ntfy({"NTFY_TOPIC": TOPIC})])
    assert TOPIC not in str(nf.status())


def test_minted_topics_are_long_and_unique():
    a, b = notify.pick_topic(), notify.pick_topic()
    assert a != b
    assert len(a) >= 30, "a guessable topic is a readable and spoofable alert feed"


# --------------------------------------------------------------------------
# Unconfigured is honest, not an error
# --------------------------------------------------------------------------

def test_unconfigured_notifier_reports_why_and_sends_nothing():
    nf = notify.Notifier(channels=[notify.Ntfy({}), notify.Telegram({})])
    assert nf.configured is False
    assert nf.send("anything") is False
    assert "NTFY_TOPIC" in nf.status()["reason"]


def test_unconfigured_channels_name_what_is_missing():
    assert "NTFY_TOPIC" in notify.Ntfy({}).status()["reason"]
    tg = notify.Telegram({"TELEGRAM_BOT_TOKEN": "x"}).status()
    assert "TELEGRAM_CHAT_ID" in tg["reason"] and "TELEGRAM_BOT_TOKEN" not in tg["reason"]


def test_telegram_needs_both_halves():
    assert notify.Telegram({"TELEGRAM_BOT_TOKEN": "x"}).configured is False
    assert notify.Telegram({"TELEGRAM_CHAT_ID": "1"}).configured is False
    assert notify.Telegram({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1"}).configured


# --------------------------------------------------------------------------
# Fan-out
# --------------------------------------------------------------------------

def test_one_dead_channel_does_not_mark_a_delivered_alert_undelivered():
    live = Recorder()
    nf = notify.Notifier(channels=[Dead(), live])
    nf._last_send = -999  # skip the inter-send sleep
    assert nf.send("body", "title") is True
    assert live.sent == [("title", "body")]


def test_send_reaches_every_configured_channel():
    a, b = Recorder(), Recorder()
    nf = notify.Notifier(channels=[a, b])
    nf._last_send = -999
    nf.send("body", "title")
    assert a.sent and b.sent


def test_all_channels_failing_reports_failure():
    nf = notify.Notifier(channels=[Dead(), Dead()])
    nf._last_send = -999
    assert nf.send("body") is False


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

@pytest.fixture
def rec():
    return {"symbol": "SPY", "direction": "below", "level": 745.0, "level_ref": "flip",
            "price": 744.0, "prev_price": 746.5, "note": "trending down"}


def test_title_is_ascii_because_ntfy_headers_are_latin1(rec):
    """An emoji in the Title header 500s; the same character in the body is fine."""
    assert notify.alert_title(rec).isascii()


def test_body_keeps_the_direction_arrow(rec):
    assert "🔻" in notify.format_alert(rec)
    up = dict(rec, direction="above")
    assert "🔺" in notify.format_alert(up)


def test_body_states_the_levels_origin_not_just_its_value(rec):
    """'broke below 745.60' invites 'says who?' three hours later."""
    body = notify.format_alert(rec)
    assert "flip" in body and "745.00" in body
    assert "live dealer structure" in body


def test_note_is_carried_through(rec):
    assert "trending down" in notify.format_alert(rec)


def test_stale_source_is_flagged_for_confirmation(rec):
    body = notify.format_alert(rec, source="tdpro-spot", age=300.0)
    assert "tdpro-spot" in body and "confirm before acting" in body


def test_live_source_adds_no_noise(rec):
    assert "source:" not in notify.format_alert(rec, source="webull", age=2.0)


def test_static_level_alert_does_not_claim_live_structure():
    static = {"symbol": "GLD", "direction": "above", "level": 62.5, "level_ref": None,
              "price": 63.0, "prev_price": 62.0, "note": ""}
    assert "live dealer structure" not in notify.format_alert(static)
