"""Telegram delivery for fired alerts.

Telegram because it is free, reliable, and two-way — a reply path is the thing
that makes "ask a follow-up from your phone" possible later, which ntfy and
Pushover cannot do without extra machinery.

Credentials live in `../.env.telegram`, alongside the other two env files, so
they cannot be committed by construction (rule 2):

    TELEGRAM_BOT_TOKEN=123456:AA...
    TELEGRAM_CHAT_ID=987654321

Getting those: message @BotFather to create a bot and copy its token, send your
new bot any message, then read the chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

Degrades to configured=False when either is missing. An unconfigured notifier is
not an error — the alert still fires, still shows in the UI, and still logs; it
just does not reach the phone, and `sidecar` says so rather than pretending.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parent.parent / ".env.telegram"
API = "https://api.telegram.org"
TIMEOUT = 10

# Telegram's own limit is ~30 messages/second, nowhere near anything this sends,
# but a symbol oscillating on a level with several alerts on it can still burst.
# One second between sends is invisible to a human and keeps us far from it.
MIN_GAP_SEC = 1.0

MAX_LEN = 4096  # Telegram hard limit; truncate rather than get a 400


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class Telegram:
    def __init__(self) -> None:
        env = _load_env()
        # Environment wins over the file, so run.sh or systemd can override.
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
        self._last_send = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict:
        if not self.configured:
            missing = [k for k, v in (("TELEGRAM_BOT_TOKEN", self.token),
                                      ("TELEGRAM_CHAT_ID", self.chat_id)) if not v]
            return {"configured": False, "reason": "missing " + ", ".join(missing)}
        return {"configured": True, "chat_id": self.chat_id}

    def send(self, text: str) -> bool:
        """Send one message. Returns success; never raises."""
        if not self.configured:
            return False
        gap = time.monotonic() - self._last_send
        if gap < MIN_GAP_SEC:
            time.sleep(MIN_GAP_SEC - gap)
        try:
            r = requests.post(
                f"{API}/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:MAX_LEN],
                      "disable_web_page_preview": True},
                timeout=TIMEOUT,
            )
            self._last_send = time.monotonic()
            return r.status_code == 200
        except Exception:
            self._last_send = time.monotonic()
            return False


def format_alert(rec: dict, source: str | None = None, age: float | None = None) -> str:
    """Render a fire record for the phone.

    States the level's ORIGIN, not just its value. "SPY broke below 745.60"
    invites the question "says who?" three hours later; "below flip (745.60)"
    answers it, and a gamma level that was re-resolved this minute is a
    materially different claim from a number typed in last week.
    """
    arrow = "🔻" if rec["direction"] == "below" else "🔺"
    where = rec["level_ref"] or "level"
    lines = [
        f"{arrow} {rec['symbol']} broke {rec['direction']} {where} ${rec['level']:.2f}",
        f"   price ${rec['price']:.2f} (from ${rec['prev_price']:.2f})",
    ]
    if rec["level_ref"]:
        lines.append(f"   {rec['level_ref']} is live dealer structure, re-read this tick")
    if rec.get("note"):
        lines.append(f"   note: {rec['note']}")
    # A price this old is not a trigger anyone should act on without looking.
    if source and source != "webull":
        detail = f"   source: {source}"
        if age and age > 60:
            detail += f", {int(age)}s old — confirm before acting"
        lines.append(detail)
    return "\n".join(lines)
