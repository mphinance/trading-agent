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

try:
    import requests
except ModuleNotFoundError as e:  # pragma: no cover - operator ergonomics
    # Reached by running `python3 notify.py` instead of the venv's python. The
    # bare ImportError names the module but not the cause, and the cause is
    # always the same: the deps live in .venv (python3.10, since the Webull SDK
    # pins <3.14) and the system interpreter has none of them.
    import sys
    _venv = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
    print(f"{e}\n", file=sys.stderr)
    print(f"Ran with: {sys.executable}", file=sys.stderr)
    if _venv.exists():
        print(f"Use the project venv instead:\n\n    {_venv} {Path(__file__).name} "
              f"{' '.join(sys.argv[1:]) or '--setup'}\n", file=sys.stderr)
    else:
        print("No .venv here. Create one:\n\n"
              "    python3.10 -m venv .venv\n"
              "    ./.venv/bin/pip install -r requirements.txt\n", file=sys.stderr)
    raise SystemExit(1)

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


def _self_cmd() -> str:
    """How to re-invoke this script, using the interpreter actually running it.

    Hardcoding `python3` here is wrong on the deployment this is written for:
    venus runs sidecar from a python3.10 venv (the Webull SDK pins <3.14), and
    the system python3 has none of the dependencies, so `python3 notify.py`
    dies on `import requests` before doing anything. Deriving the command from
    sys.executable means the instructions are correct wherever they are printed.
    """
    import sys
    exe = Path(sys.executable)
    try:
        rel = exe.relative_to(Path.cwd())
        exe_str = f"./{rel}"
    except ValueError:
        exe_str = str(exe)
    return f"{exe_str} notify.py"


def _setup() -> int:
    """`notify.py --setup` — mint a topic and save it.

    Writes `../.env.notify` with mode 0600. The file holds the topic, which is
    the only thing standing between your alerts and anyone else, so it gets the
    same treatment as a token. Refuses to overwrite an existing NTFY_TOPIC —
    replacing it silently would leave the phone subscribed to a dead one.

    Deliberately does NOT send a test here. You cannot subscribe to a topic that
    does not exist yet, so a test fired at this moment always arrives before the
    phone is listening and is always missed. Subscribe first, then `--test`.
    """
    path = PARENT / ".env.notify"
    existing = _load_env()
    if existing.get("NTFY_TOPIC"):
        print(f"NTFY_TOPIC is already set (in {path} or the environment).")
        print(f"Run `{_self_cmd()} --test` to send to it, or delete the line")
        print("from that file first if you want a new topic.")
        return 1

    topic = pick_topic()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"NTFY_TOPIC={topic}\n")
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"could not write {path}: {e}")
        return 1

    print(f"wrote {path} (mode 600)\n")
    print("1. On your phone, install ntfy:")
    print("     iOS      App Store   -> 'ntfy'")
    print("     Android  Play Store or F-Droid -> 'ntfy'")
    print("   Leave the server as the default (ntfy.sh). There is no account to make.\n")
    print("2. Tap + / Subscribe to topic, and enter this EXACTLY:\n")
    print(f"     {topic}\n")
    print(f"3. Come back here and run:  {_self_cmd()} --test\n")
    print("4. Restart sidecar to pick up the topic.\n")
    print("Keep that topic off camera. ntfy.sh has no accounts, so the topic IS")
    print("the credential: anyone who reads it off a stream gets your alerts,")
    print("and can send you fake ones. Rotate by deleting the line and re-running.")
    return 0


def _test() -> int:
    """`notify.py --test` — send to whatever is configured, repeatably.

    Separate from --setup precisely so it can be run again: the first test
    always races the phone's subscription, and "did it work" is a question you
    need to be able to ask more than once.
    """
    n = Notifier()
    if not n.configured:
        print(n.status()["reason"])
        print(f"\nRun `{_self_cmd()} --setup` for the no-signup ntfy path.")
        return 1
    names = ", ".join(c.name for c in n.active)
    if n.send("If you can read this, sidecar alerts will reach your phone.",
              "sidecar is connected"):
        print(f"sent via {names}. If nothing arrives, check the topic in the app "
              "matches ../.env.notify exactly.")
        return 0
    print(f"FAILED to send via {names} — check network access.")
    return 1


if __name__ == "__main__":
    import sys
    if "--setup" in sys.argv:
        raise SystemExit(_setup())
    if "--test" in sys.argv:
        raise SystemExit(_test())
    print(json.dumps(Notifier().status(), indent=2))
    raise SystemExit(0)
