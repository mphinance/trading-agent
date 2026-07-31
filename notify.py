"""Delivery for fired alerts. Two channels, either or both.

**ntfy** — no account, no email, no signup. Pick a topic, install the app,
subscribe to that topic. This is the zero-friction option and the default
recommendation.

**Telegram** — free and two-way, so it is the channel that could later carry a
reply path ("ask a follow-up from your phone"). Costs a @BotFather signup, which
now wants an email address.

Config lives in the parent directory so it cannot be committed by construction
(rule 2). Any of `../.env.notify`, `../.env.telegram` will do; later files and
real environment variables win, so `run.sh` and systemd can override:

    NTFY_TOPIC=daddy-alerts-8f3a91c4b207e5
    NTFY_SERVER=https://ntfy.sh          # optional, for a self-hosted server
    TELEGRAM_BOT_TOKEN=123456:AA...
    TELEGRAM_CHAT_ID=987654321

**An ntfy topic IS the credential.** There are no accounts and no access control:
anyone who knows the topic name can read every alert you publish to it, and
anyone can publish to it. So it must be long and random — `pick_topic()` mints
one — and it must never appear on screen. This panel gets streamed (rule 5),
which is why `status()` deliberately does not return the topic and no route ever
sends it to the browser. A topic read off a video frame is a subscription
someone else keeps.

Nothing configured is not an error: the alert still fires, still shows in the
UI, and is still logged. It just does not reach the phone, and sidecar says so
rather than pretending.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

import requests

PARENT = Path(__file__).resolve().parent.parent
ENV_PATHS = (PARENT / ".env.notify", PARENT / ".env.telegram")

TELEGRAM_API = "https://api.telegram.org"
NTFY_DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT = 10

# A symbol oscillating on a level with several alerts on it can burst. One
# second between sends is invisible to a human and keeps both services happy
# (Telegram's own ceiling is ~30/s, far above anything this produces).
MIN_GAP_SEC = 1.0

# Telegram truncates at 4096; ntfy is more generous. Use the smaller so a
# message reads identically on both channels.
MAX_LEN = 4096


def pick_topic(prefix: str = "sidecar") -> str:
    """Mint a topic nobody will guess. 128 bits of randomness, URL-safe."""
    return f"{prefix}-{secrets.token_hex(16)}"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in ENV_PATHS:
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            continue
    return out


class _Channel:
    name = "channel"

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    def status(self) -> dict:
        raise NotImplementedError

    def send(self, text: str, title: str = "") -> bool:
        raise NotImplementedError


class Ntfy(_Channel):
    name = "ntfy"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = env if env is not None else _load_env()
        self.topic = os.environ.get("NTFY_TOPIC") or env.get("NTFY_TOPIC", "")
        self.server = (os.environ.get("NTFY_SERVER") or env.get("NTFY_SERVER")
                       or NTFY_DEFAULT_SERVER).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    def status(self) -> dict:
        # Never return the topic. It is the whole credential, and this ends up
        # rendered in a panel that gets streamed.
        if not self.configured:
            return {"name": self.name, "configured": False, "reason": "NTFY_TOPIC not set"}
        return {"name": self.name, "configured": True, "server": self.server}

    def send(self, text: str, title: str = "") -> bool:
        if not self.configured:
            return False
        try:
            headers = {"Priority": "high", "Tags": "chart_with_downwards_trend"}
            if title:
                # Header values must be latin-1; emoji in a title would 500 here
                # while the identical text in the body is fine.
                headers["Title"] = title.encode("ascii", "ignore").decode() or "sidecar"
            r = requests.post(f"{self.server}/{self.topic}",
                              data=text[:MAX_LEN].encode("utf-8"),
                              headers=headers, timeout=TIMEOUT)
            return r.status_code < 300
        except Exception:
            return False


class Telegram(_Channel):
    name = "telegram"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = env if env is not None else _load_env()
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict:
        if not self.configured:
            missing = [k for k, v in (("TELEGRAM_BOT_TOKEN", self.token),
                                      ("TELEGRAM_CHAT_ID", self.chat_id)) if not v]
            return {"name": self.name, "configured": False,
                    "reason": "missing " + ", ".join(missing)}
        return {"name": self.name, "configured": True}

    def send(self, text: str, title: str = "") -> bool:
        if not self.configured:
            return False
        body = f"{title}\n{text}" if title else text
        try:
            r = requests.post(
                f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": body[:MAX_LEN],
                      "disable_web_page_preview": True},
                timeout=TIMEOUT,
            )
            return r.status_code == 200
        except Exception:
            return False


class Notifier:
    """Fans a message out to every configured channel.

    Success is ANY channel accepting it. Running both is a reasonable thing to
    want — one is the phone you carry, the other the one you read at the desk —
    and a single failing channel should not mark a delivered alert undelivered.
    """

    def __init__(self, channels: list[_Channel] | None = None) -> None:
        env = _load_env()
        self.channels = channels if channels is not None else [Ntfy(env), Telegram(env)]
        self._last_send = 0.0

    @property
    def configured(self) -> bool:
        return any(c.configured for c in self.channels)

    @property
    def active(self) -> list[_Channel]:
        return [c for c in self.channels if c.configured]

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "channels": [c.status() for c in self.channels],
            "reason": None if self.configured else (
                "no delivery channel — set NTFY_TOPIC (no signup) or TELEGRAM_BOT_TOKEN "
                "+ TELEGRAM_CHAT_ID in ../.env.notify"),
        }

    def send(self, text: str, title: str = "") -> bool:
        active = self.active
        if not active:
            return False
        gap = time.monotonic() - self._last_send
        if gap < MIN_GAP_SEC:
            time.sleep(MIN_GAP_SEC - gap)
        ok = False
        for c in active:
            ok = c.send(text, title) or ok
        self._last_send = time.monotonic()
        return ok


def format_alert(rec: dict, source: str | None = None, age: float | None = None) -> str:
    """Render a fire record for the phone.

    States the level's ORIGIN, not just its value. "SPY broke below 745.60"
    invites the question "says who?" three hours later; "below flip (745.60)"
    answers it, and a gamma level re-resolved this minute is a materially
    different claim from a number typed in last week.
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


def alert_title(rec: dict) -> str:
    """Short ASCII title for the notification shade."""
    return f"{rec['symbol']} broke {rec['direction']} {rec['level']:.2f}"


def _setup() -> int:
    """`python3 notify.py --setup` — mint a topic, save it, send a test.

    Writes `../.env.notify` with mode 0600. The file holds the topic, which is
    the only thing standing between your alerts and anyone else, so it gets the
    same treatment as a token. Refuses to overwrite an existing NTFY_TOPIC —
    replacing it silently would leave the phone subscribed to a dead one.
    """
    path = PARENT / ".env.notify"
    existing = _load_env()
    if existing.get("NTFY_TOPIC"):
        print(f"NTFY_TOPIC is already set (in {path} or the environment). "
              "Delete it first if you want a new one.")
        return 1

    topic = pick_topic()
    line = f"NTFY_TOPIC={topic}\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"could not write {path}: {e}")
        return 1

    print(f"wrote {path} (mode 600)\n")
    print("On your phone: install ntfy (App Store / Play Store / F-Droid),")
    print("tap Subscribe, and enter this topic EXACTLY:\n")
    print(f"    {topic}\n")
    print("Keep it off camera. There are no accounts on ntfy.sh — the topic IS")
    print("the credential, and anyone who reads it off a stream gets your alerts.\n")

    if Ntfy({"NTFY_TOPIC": topic}).send(
            "If you can read this, sidecar alerts will reach your phone.",
            "sidecar is connected"):
        print("test notification sent. Subscribe first, then re-run to see it arrive.")
    else:
        print("test notification FAILED to send — check network access to ntfy.sh")
        return 1
    print("\nRestart sidecar to pick up the new topic.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_setup() if "--setup" in sys.argv else
                     (print(json.dumps(Notifier().status(), indent=2)) or 0))
