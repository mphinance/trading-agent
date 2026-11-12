"""The first-run wizard: a table-driven state machine, one method per state.

Rules this module keeps (FIRST_RUN.md, AUTODETECTION.md section 9, global constraints):

* Secret VALUES live in exactly two places: ``_Wizard._values`` (memory) and the ``WritePlan`` handed
  to ``write_profile``. A value is read from the environment in ONE function (``_copy_from_environment``),
  only after a per-item yes. Nothing says, logs or persists a value; the state file holds decision tokens.
* Nothing is pre-checked, nothing is required, every screen has Back where there is a place to go back to.
* Every write goes through ``write_profile`` into the one new profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from trading_agent.catalog.merge import BANNER
from trading_agent.detect.binaries import AI_CLIS, detect_ai_clis
from trading_agent.detect.env import detect_env, load_credential_specs
from trading_agent.detect.mcp_files import detect_mcp_entries
from trading_agent.detect.ports import probe_ports
from trading_agent.findings import Finding
from trading_agent.libraries import Library, keys_needed, listed_only
from trading_agent.platform import Platform
from trading_agent.profile.hermes_cli import HermesCli, HermesNotBootstrapped, HermesUnavailable, validate_profile_name
from trading_agent.profile.receipt import Receipt, ReceiptError
from trading_agent.profile.writer import (
    AiChoice, BrokerChoice, ProfileExists, WriteError, WritePlan, _validate_ai, write_profile,
)
from trading_agent.secrets import SecretStore, check_secret, redact_patterns, scrub
from trading_agent.wizard import screens as S
from trading_agent.wizard.io import BACK_INPUT, IO, Option
from trading_agent.wizard.state import State, WizardState

# ---------------------------------------------------------------- catalog helpers

_FIRST_SET = frozenset(S.FIRST_SCREEN_SLUGS)
_WRITABLE_KINDS = frozenset({"remote-url", "local-command"})  # the kinds writer.build_mcp_entry can write
_MODE_WORDS = ("PAPER", "DEMO", "SANDBOX", "MODE", "READ_ONLY", "READONLY", "_ENV")
_SECRET_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSPHRASE", "PIN", "ID")
_KEY_ENV_RE = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+")   # at least one underscore: a pasted key is not a name


def order_catalog(entries: Sequence[dict]) -> list[dict]:
    """First-screen slugs (those present), in list order, then the rest A to Z. Ordering only: nothing is dropped."""
    by_slug = {e["slug"]: e for e in entries}
    first = [by_slug[s] for s in S.FIRST_SCREEN_SLUGS if s in by_slug]
    rest = sorted((e for e in entries if e["slug"] not in _FIRST_SET),
                  key=lambda e: (str(e.get("name", "")).casefold(), e["slug"]))
    return first + rest


def region_is_uncurated(entry: dict) -> bool:
    return not entry.get("region_codes")


def search_catalog(entries: Sequence[dict], *, text: str = "", region: str | None = None,
                   status: str | None = None, trading: str | None = None,
                   route_kind: str | None = None) -> list[dict]:
    """Filter the merged catalog. With a region, entries without ``region_codes`` follow the matches
    as the "region not curated" group and are never hidden."""
    needle = text.strip().casefold()
    rows = []
    for e in order_catalog(entries):
        if needle:
            hay = " ".join([str(e.get("name", "")), e["slug"], str(e.get("region_raw", "")),
                            *map(str, e.get("region_codes") or [])]).casefold()
            if needle not in hay:
                continue
        if status and status not in (e.get("status") or []):
            continue
        if trading and e.get("trading") != trading:
            continue
        if route_kind and route_kind not in (e.get("route_kinds") or []):
            continue
        rows.append(e)
    if not region or not region.strip():
        return rows
    want = region.strip().upper()
    matched, uncurated = [], []
    for e in rows:
        if region_is_uncurated(e):
            uncurated.append(e)
        else:
            codes = {str(c).upper() for c in e["region_codes"]}
            if want in codes or "GLOBAL" in codes:
                matched.append(e)
    return matched + uncurated


def installable_routes(entry: dict) -> list[dict]:
    """Routes v1 can install: the entry must be selectable and the route a kind the writer can write."""
    if not (entry.get("wizard") or {}).get("selectable"):
        return []
    return [r for r in entry.get("routes") or [] if r.get("kind") in _WRITABLE_KINDS]


def is_installable(entry: dict) -> bool:
    return bool(installable_routes(entry))


# ---------------------------------------------------------------- dependencies

@dataclass
class Detectors:
    """Injected so tests never touch the real machine. Each returns Findings (names only)."""
    env: Callable[[], list[Finding]]
    mcp_files: Callable[[bool], list[Finding]]   # argument: whether to parse other apps' MCP files
    ai_clis: Callable[[], list[Finding]]
    ports: Callable[[], list[Finding]]


def default_detectors(platform: Platform) -> Detectors:
    specs = load_credential_specs()
    return Detectors(
        env=lambda: detect_env(platform, specs),
        mcp_files=lambda enabled: detect_mcp_entries(platform, enabled=enabled),
        ai_clis=lambda: detect_ai_clis(platform),
        ports=probe_ports,
    )


def default_verify_ai(choice: AiChoice) -> str:
    """Doctor does not exist yet."""
    return S.NOT_TESTED


@dataclass
class Deps:
    platform: Platform
    hermes: HermesCli
    detectors: Detectors
    catalog: list[dict]
    libraries: list[Library]
    store_factory: Callable[[Path], SecretStore]
    source_dir: Path
    online: bool = True
    verify_ai: Callable[[AiChoice], str] = default_verify_ai


# ---------------------------------------------------------------- AI provider facts

# (label, brand) for the "found a CLI" note. UNVERIFIED display names for gemini and agy.
_CLI_NAMES = {"claude": ("Claude Code", "Claude"), "codex": ("Codex", "Codex"),
              "gemini": ("Gemini CLI", "Gemini"), "agy": ("Antigravity", "Antigravity")}
_LOCAL = (("ollama", "Ollama"), ("lm-studio", "LM Studio"))
# key name -> (id fragment, label, cost line, AiChoice kind, base url)
_KEY_PROVIDERS = {
    S.OPENROUTER_KEY_ENV: ("OpenRouter", "One key, many models, pay as you go. Cheap models cost cents.",
                           "openrouter", ""),
    S.VENICE_KEY_ENV: ("Venice", "Uses your Venice account. Cost depends on the model you pick.",
                       "custom", S.VENICE_BASE_URL),
    S.OPENAI_KEY_ENV: ("OpenAI", "Pay as you go. Cost depends on the model you pick.",
                       "custom", S.OPENAI_BASE_URL),
}
_UNSUPPORTED_AI_KEYS = ("ANTHROPIC_API_KEY",)
_LOGIN_SHELL_WHERE = "login shell environment"  # detect/env.py: present in the login shell only, value unreachable
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_FINAL_MODES = frozenset({"reuse", "paste", "plain", "keep-existing", "skip"})

# the order states are walked; Back goes to _BACK unless a handler says otherwise
_BACK = {
    State.PRECHECK: State.START, State.SCAN: State.PRECHECK, State.AI_ROUTE: State.SCAN,
    State.AI_VERIFY: State.AI_ROUTE, State.BROKER_PICK: State.AI_VERIFY, State.BROKER_INSTALL: State.BROKER_PICK,
    State.BROKER_VERIFY: State.BROKER_INSTALL, State.DATA_KEYS: State.SKILLS, State.REVIEW: State.DATA_KEYS,
}


class _Quit(Exception):
    """The user chose to stop here. State is already saved."""


@dataclass
class _NameSpec:
    name: str
    secret: bool


def _looks_like_mode_switch(var: str) -> bool:
    """For header-derived names, which carry no secret flag: plain only if it reads like a mode switch AND not
    like a credential. When unsure the answer is False, so the value is hidden."""
    up = var.upper()
    if up.endswith(_SECRET_SUFFIXES):
        return False
    return any(w in up for w in _MODE_WORDS)


def _route_names(route: dict) -> list[_NameSpec]:
    """EVERY env var NAME and header NAME the route lists. A header's variable is ``header_env[h]`` or h itself.
    Mode switches (role mode-switch, or a header named like PAPER_TRADING) are plain, everything else is secret."""
    auth = route.get("auth") or {}
    out: list[_NameSpec] = []
    seen: set[str] = set()
    for v in auth.get("env_vars") or []:
        name = v.get("name")
        if not isinstance(name, str) or name in seen:
            continue
        secret = bool(v.get("secret", True)) and v.get("role") != "mode-switch"
        out.append(_NameSpec(name, secret))
        seen.add(name)
    header_env = auth.get("header_env") or {}
    for h in auth.get("headers") or []:
        var = header_env.get(h, h)
        if not isinstance(var, str) or var in seen:
            continue
        out.append(_NameSpec(var, not _looks_like_mode_switch(var)))
        seen.add(var)
    return out


# ---------------------------------------------------------------- the wizard

class _Wizard:
    def __init__(self, io: IO, deps: Deps) -> None:
        self.io = io
        self.d = deps
        self.p = deps.platform
        self.ws = WizardState.load(self.p)
        if self.ws.current is State.DONE:       # a finished run is history: a new run starts fresh
            self.ws = WizardState()
        self.ws.save(self.p)
        self._values: dict[str, str] = {}       # secret VALUES: memory only, never persisted
        self._findings: list[Finding] | None = None
        self._could_not: list[str] = []
        self.receipt: Receipt | None = None

    # ---- plumbing

    def say(self, text: str) -> None:
        self.io.say(text)

    def go(self, state: State) -> State:
        self.ws.current = state
        self.ws.save(self.p)
        return state

    def decide(self, key: str, value: str) -> None:
        self.ws.set(key, value)
        self.ws.save(self.p)

    def forget(self, *keys: str) -> None:
        for k in keys:
            self.ws.forget(k)
        self.ws.save(self.p)

    def dec(self, key: str, default: str = "") -> str:
        return self.ws.decisions.get(key, default)

    def safe(self, text: str) -> str:
        """Text that may have touched a value: known values scrubbed, then credential shapes redacted."""
        return redact_patterns(scrub(str(text), list(self._values.values())))

    def run(self) -> Receipt | None:
        self._resume_fixups()
        try:
            while True:
                cur = self.ws.current
                nxt = self._table()[cur]()
                if nxt is None:
                    return self.receipt
                self.go(nxt)
        except _Quit:
            return None

    def _table(self) -> dict[State, Callable[[], State | None]]:
        return {
            State.START: self._st_start, State.PRECHECK: self._st_precheck, State.SCAN: self._st_scan,
            State.AI_ROUTE: self._st_ai_route, State.AI_VERIFY: self._st_ai_verify,
            State.BROKER_PICK: self._st_broker_pick, State.BROKER_INSTALL: self._st_broker_install,
            State.BROKER_VERIFY: self._st_broker_verify, State.SKILLS: self._st_skills,
            State.DATA_KEYS: self._st_data_keys, State.REVIEW: self._st_review, State.WRITE: self._st_write,
            State.VERIFY: self._st_verify, State.DONE: self._st_done,
        }

    def _resume_fixups(self) -> None:
        """A run interrupted at WRITE or VERIFY: if the receipt shows the write finished, go on; else review again."""
        if self.ws.current not in (State.WRITE, State.VERIFY):
            return
        name = self.dec("profile_name")
        rec = None
        if name:
            try:
                rec = Receipt.load(self.p, name)
            except (ReceiptError, ValueError):
                rec = None
        if rec is not None and "skills" in rec.steps_done:
            self.receipt = rec
            self.go(State.VERIFY)
        else:
            self.go(State.REVIEW)

    # ---- findings (names only), loaded lazily so a resumed run never re-asks for what it already has

    def _run_detectors(self, apps: bool) -> None:
        found: list[Finding] = []
        could_not: list[str] = []
        for label, call in (("saved keys", self.d.detectors.env),
                            ("AI tools", self.d.detectors.ai_clis),
                            ("local model ports", self.d.detectors.ports),
                            ("other apps' MCP settings", lambda: self.d.detectors.mcp_files(apps))):
            try:
                found.extend(call())
            except Exception:  # noqa: BLE001 - a failing detector never aborts setup and never echoes its error
                could_not.append(label)
        self._findings, self._could_not = found, could_not

    def findings(self) -> list[Finding]:
        if self._findings is None:
            self._run_detectors(self.dec("scan.apps", "yes") == "yes")
        return self._findings or []

    def _present(self, fid: str) -> Finding | None:
        for f in self.findings():
            if f.id == fid and f.state == "present":
                return f
        return None

    def _finding_for(self, name: str, kinds: tuple[str, ...]) -> Finding | None:
        """The finding for env var NAME, but only of the given kinds. This is the AI/broker/data scoping:
        a Gemini exchange key (broker-key) is never an AI key, and an AI key is never a broker key."""
        for f in self.findings():
            if f.kind in kinds and f.names == (name,):
                return f
        return None

    # ---- START

    def _st_start(self) -> State:
        while True:
            a = self.io.choose(S.START, [Option("start", "Start"), Option("what", "What does this do?")])
            if a == "what":
                self.say(S.WHAT_IT_DOES)
                continue
            return State.PRECHECK

    # ---- PRECHECK

    def _hermes_check(self) -> str:
        """'ok', 'stop' or 'back'. Looks for the binary first and never runs an install command."""
        while True:
            problem: str | None = None
            if self.p.which("hermes") is None:
                self.say(S.HERMES_MISSING)
                self.say(S.HERMES_INSTALL_HINT.format(cmd=S.INSTALL_CMD))
                problem = "missing"
            else:
                try:
                    line = self.d.hermes.version()
                except HermesNotBootstrapped:
                    self.say(S.HERMES_NOT_BOOTSTRAPPED)
                    return "ok"
                except HermesUnavailable:
                    self.say(S.HERMES_FAILED)
                    problem = "failed"
                else:
                    m = _VERSION_RE.search(line)
                    if m is None:
                        self.say(S.HERMES_VERSION_UNREADABLE)
                        return "ok"
                    if tuple(int(x) for x in m.groups()) < S.HERMES_MIN_VERSION:
                        self.say(S.HERMES_OUT_OF_RANGE.format(requires=S.HERMES_REQUIRES, version=line))
                        self.say(S.HERMES_UPDATE_HINT)
                        problem = "old"
                    else:
                        return "ok"
            a = self.io.choose(S.HERMES_WHAT_NOW, [Option("again", "Check again"),
                                                    Option("stop", "Stop here for now"), Option("back", "Back")])
            if a == "again":
                continue
            if a == "back":
                return "back"
            raise _Quit

    def _st_precheck(self) -> State:
        if not self.d.online:
            self.say(S.OFFLINE_NOTE)
        r = self._hermes_check()
        if r == "back":
            return State.START
        name: str | None = self.dec("profile_name") or None
        if name is not None:
            a = self.io.choose(S.NAME_KEEP.format(name=name),
                               [Option("continue", "Continue"), Option("rename", "Pick another name"),
                                Option("back", "Back")])
            if a == "back":
                return State.START
            if a == "rename":
                name = None
        while True:
            if name is None:
                raw = self.io.ask(S.NAME_PROMPT).strip()
                if raw == BACK_INPUT:
                    return State.START
                cand = raw or S.DEFAULT_PROFILE_NAME
                try:
                    validate_profile_name(cand)
                except ValueError:
                    self.say(S.NAME_INVALID)   # never echoes what was typed
                    continue
                name = cand
            if self.d.hermes.profile_exists(name):
                a = self.io.choose(S.PROFILE_TAKEN.format(name=name),
                                   [Option("rename", "Pick another name"), Option("back", "Back")])
                if a == "back":
                    return State.START
                name = None
                continue
            break
        if name != self.dec("profile_name"):
            self._forget_keep_existing()
        self.decide("profile_name", name)
        return State.SCAN

    def _forget_keep_existing(self) -> None:
        for k, v in list(self.ws.decisions.items()):
            if k.startswith("val:") and v == "keep-existing":
                self.ws.forget(k)

    # ---- SCAN

    def _st_scan(self) -> State:
        a = self.io.choose(S.SCAN_PROMPT, [
            Option("scan", "Look around"),
            Option("scan-skip-apps", "Look around, but skip scanning other apps",
                   "Turns off reading other apps' MCP settings."),
            Option("back", "Back")])
        if a == "back":
            return State.PRECHECK
        apps = a == "scan"
        self.decide("scan.apps", "yes" if apps else "no")
        self._run_detectors(apps)
        self._show_findings()
        return State.AI_ROUTE

    def _show_findings(self) -> None:
        lines = []
        for f in self.findings():
            if f.state != "present":
                continue
            if f.kind in ("ai-key", "broker-key", "data-key"):
                lines.append(f"- {f.what}: {', '.join(f.names)} ({f.where})")
            else:
                lines.append(f"- {f.what} ({f.where})")
        self.say(S.SCAN_FOUND + "\n" + "\n".join(lines) if lines else S.SCAN_NOTHING)
        could = [S.SCAN_COULD_NOT.format(what=f.what) for f in self.findings() if f.state in ("unreadable", "blocked")]
        could += [S.SCAN_COULD_NOT.format(what=w) for w in self._could_not]
        if could:
            self.say("\n".join(could))
        self.say(S.SCAN_FOOTER)

    # ---- value engine (the only code that handles secret values)

    def _copy_from_environment(self, name: str) -> bool:
        """The ONE place a found key's value is read: only after a per-item yes. The value goes into
        ``self._values`` and is never returned, printed or persisted."""
        value = self.p.environ().get(name)
        if not value:
            return False
        try:
            check_secret(name, value)
        except ValueError:
            return False
        self._values[name] = value
        return True

    def _profile_dir(self) -> Path:
        return self.d.hermes.profile_dir(self.dec("profile_name") or S.DEFAULT_PROFILE_NAME)

    def _exists_in_profile(self, name: str) -> bool:
        try:
            return bool(self.d.store_factory(self._profile_dir()).exists(name))
        except Exception:  # noqa: BLE001 - cannot tell: fail safe and ask instead of risking an overwrite
            return True

    def _collision_kept(self, name: str) -> bool:
        """True if NAME is already in the target profile's .env and the user chose to keep it."""
        if not self._exists_in_profile(name):
            return False
        a = self.io.choose(S.EXISTS_PROMPT.format(name=name),
                           [Option("keep", "Keep what is there"), Option("replace", "Replace it")])
        if a == "keep":
            self._values.pop(name, None)
            self.decide(f"val:{name}", "keep-existing")
            return True
        return False

    def _paste(self, name: str, *, again: bool = False) -> bool:
        """Ask for a secret with no echo. True if a value was stored; empty input skips."""
        prompt = (S.PASTE_AGAIN if again else S.PASTE_PROMPT).format(name=name)
        while True:
            v = self.io.secret(prompt)
            if v == "":
                self._values.pop(name, None)
                self.decide(f"val:{name}", "skip")
                return False
            try:
                check_secret(name, v)
            except ValueError:
                self.say(S.BAD_VALUE)
                continue
            self._values[name] = v
            self.decide(f"val:{name}", "paste")
            return True

    def _plain(self, name: str, *, again: bool = False) -> bool:
        prompt = (S.PLAIN_AGAIN if again else S.PLAIN_PROMPT).format(name=name)
        while True:
            v = self.io.ask(prompt).strip()
            if v == "":
                self._values.pop(name, None)
                self.decide(f"val:{name}", "skip")
                return False
            try:
                check_secret(name, v)
            except ValueError:
                self.say(S.BAD_VALUE)
                continue
            self._values[name] = v
            self.decide(f"val:{name}", "plain")
            return True

    def _offer_reuse(self, name: str, finding: Finding | None) -> str:
        """'reused', 'declined' (remembered per finding id) or 'unavailable' (nothing copyable; paste instead)."""
        if finding is None or finding.state != "present":
            return "unavailable"
        if finding.reusable != "copy-with-consent":
            self.say(S.REFERENCE_ONLY.format(name=name))
            return "unavailable"
        if finding.where == _LOGIN_SHELL_WHERE:
            self.say(S.LOGIN_SHELL_ONLY.format(name=name))
            return "unavailable"
        if self.dec(f"declined:{finding.id}"):
            return "declined"
        if self.io.confirm(S.REUSE_PROMPT.format(name=name)):
            if self._copy_from_environment(name):
                self.decide(f"val:{name}", "reuse")
                return "reused"
            self.say(S.VANISHED.format(name=name))
            return "unavailable"
        self.decide(f"declined:{finding.id}", "1")
        return "declined"

    def _obtain(self, name: str, *, secret: bool, kinds: tuple[str, ...]) -> None:
        """Get a value for NAME (or an explicit skip). Already-decided names are left alone."""
        if self.dec(f"val:{name}") in _FINAL_MODES:
            return
        if self._collision_kept(name):
            return
        if not secret:
            self._plain(name)
            return
        if self._offer_reuse(name, self._finding_for(name, kinds)) == "reused":
            return
        a = self.io.choose(S.PASTE_ONE.format(name=name), [Option("paste", "Paste it"),
                                                            Option("skip", "Skip, finish later")])
        if a == "skip":
            self._values.pop(name, None)
            self.decide(f"val:{name}", "skip")
            return
        self._paste(name)

    def _drop_value(self, name: str) -> None:
        self._values.pop(name, None)
        self.forget(f"val:{name}")

    def _ensure_values(self) -> None:
        """Before REVIEW: any decided name whose value this process does not hold gets it back
        (re-read after a recorded yes, or asked again). Values are never saved between runs."""
        for name in self._expected_names():
            mode = self.dec(f"val:{name}")
            if name in self._values or mode not in ("reuse", "paste", "plain"):
                continue
            if mode == "reuse":
                if self._copy_from_environment(name):
                    continue
                self.say(S.VANISHED.format(name=name))
                self._paste(name)
            elif mode == "paste":
                self._paste(name, again=True)
            else:
                self._plain(name, again=True)

    # ---- what is expected in the plan (stale decisions for things no longer picked are ignored)

    def _ai_choice(self) -> AiChoice:
        kind = self.dec("ai.kind", "none")
        if kind == "cli":
            return AiChoice("cli", self.dec("ai.ref"))
        if kind == "custom":
            return AiChoice("custom", self.dec("ai.ref"), self.dec("ai.key_env"))
        return AiChoice(kind)  # type: ignore[arg-type]

    def _ai_value_name(self) -> str | None:
        kind = self.dec("ai.kind")
        if kind == "openrouter":
            return S.OPENROUTER_KEY_ENV
        if kind == "custom":
            return self.dec("ai.key_env") or None
        return None

    def _entry(self) -> dict | None:
        slug = self.dec("broker.slug")
        return next((e for e in self.d.catalog if e["slug"] == slug), None) if slug not in ("", "none") else None

    def _route(self) -> dict | None:
        entry = self._entry()
        if entry is None:
            return None
        return next((r for r in installable_routes(entry) if r.get("route_id") == self.dec("broker.route")), None)

    def _selected_libs(self) -> list[Library]:
        ids = [i for i in self.dec("skills").split(",") if i]
        return [lib for lib in listed_only(self.d.libraries) if lib.id in ids]

    def _data_names(self) -> tuple[str, ...]:
        return keys_needed(self._selected_libs())

    def _expected_names(self) -> list[str]:
        names: list[str] = []
        ai = self._ai_value_name()
        if ai:
            names.append(ai)
        route = self._route()
        if route:
            names += [n.name for n in _route_names(route)]
        names += list(self._data_names())
        return list(dict.fromkeys(names))

    # ---- AI_ROUTE (v1 rulings R23, R26, R29)

    def _clis_found(self) -> list[str]:
        return [n for n in AI_CLIS if self._present(f"cli:{n}") is not None]

    def _set_ai(self, kind: str, *, ref: str = "", key_env: str = "") -> None:
        old = self._ai_value_name()
        if old and old != (key_env or (S.OPENROUTER_KEY_ENV if kind == "openrouter" else "")):
            self._drop_value(old)
        self.decide("ai.kind", kind)
        self.decide("ai.ref", ref)
        self.decide("ai.key_env", key_env)

    def _ai_menu(self, private: bool) -> list[Option]:
        opts: list[Option] = []
        for key, label in _LOCAL:
            if self._present(f"ai-local:{key}") is not None:
                opts.append(Option(f"use:ai-local:{key}", f"{label} [Found on this computer]",
                                   "Free and private. Runs on your own machine; speed depends on it."))
        for name, (label, cost, _kind, _url) in _KEY_PROVIDERS.items():
            if private and name != S.VENICE_KEY_ENV:
                continue
            f = self._present(f"env:{name}")
            if f is not None and f.kind == "ai-key" and not self.dec(f"declined:{f.id}"):
                opts.append(Option(f"use:{f.id}", f"{label} [Key found in your settings]", cost))
        if not private:
            opts.append(Option("paste-openrouter", "OpenRouter: paste a key",
                               "One key, many models, pay as you go. Cheap models cost cents. "
                               f"Make a key at {S.OPENROUTER_KEYS_URL}, then paste it here. It is never shown."))
        opts.append(Option("paste-venice", "Venice: paste a key",
                           f"Uses your Venice account. Cost depends on the model. Make a key at {S.VENICE_KEYS_URL}."))
        if not private:
            opts.append(Option("custom", "Another OpenAI-compatible service (OpenAI and others)",
                               "You give me its address and the name of the setting that holds its key."))
        opts.append(Option("private", "Show all options" if private else "Keep it private",
                           "" if private else "Shows only a model on this computer, or Venice."))
        clis = self._clis_found()
        if clis:
            brand = _CLI_NAMES[clis[0]][1]
            opts.append(Option("skip", "Skip for now", f"No brain yet. Your {brand} sign-in stays untouched."))
        else:
            opts.append(Option("skip", "Skip for now", "No brain yet. You can add one later."))
        opts.append(Option("back", "Back"))
        # We pick, you confirm: the first option is the best CONFIGURABLE one found. Nothing is pre-checked.
        first = opts[0]
        if first.id not in ("private", "skip", "back"):
            opts[0] = Option(first.id, first.label + " (Recommended)", first.detail)
        return opts

    def _st_ai_route(self) -> State:
        for n in self._clis_found():
            label, brand = _CLI_NAMES[n]
            self.say(S.AI_CLI_NOTE.format(label=label, brand=brand))
        for name in _UNSUPPORTED_AI_KEYS:
            f = self._present(f"env:{name}")
            if f is not None:
                self.say(S.AI_UNSUPPORTED_KEY.format(name=name))
        if not any(self._present(f"ai-local:{k}") for k, _ in _LOCAL):
            self.say(S.AI_NO_LOCAL)
        private = self.dec("ai.private") == "1"
        while True:
            if private:
                self.say(S.AI_PRIVATE_ON)
            a = self.io.choose(S.AI_PROMPT, self._ai_menu(private))
            if a == "back":
                return State.SCAN
            if a == "private":
                private = not private
                self.decide("ai.private", "1" if private else "0")
                continue
            if a == "skip":
                clis = self._clis_found()
                if clis:
                    self._set_ai("cli", ref=clis[0])  # recorded only because the user chose to defer
                else:
                    self._set_ai("none")
                return State.AI_VERIFY
            if self._ai_pick(a):
                return State.AI_VERIFY

    def _ai_pick(self, a: str) -> bool:
        """Apply a menu choice. False means 'back to the menu' (declined, or a typed address was backed out of)."""
        if a.startswith("use:ai-local:"):
            self._set_ai(a.split(":", 2)[2])
            return True
        if a.startswith("use:env:"):
            name = a.split(":", 2)[2]
            _label, _cost, kind, url = _KEY_PROVIDERS[name]
            finding = self._present(f"env:{name}")
            if self._collision_kept(name):
                self._set_ai_for_key(kind, url, name)
                return True
            result = self._offer_reuse(name, finding)
            if result == "declined":
                return False
            self._set_ai_for_key(kind, url, name)
            if result == "unavailable":
                self._paste(name)
            return True
        if a == "paste-openrouter":
            self._set_ai_for_key("openrouter", "", S.OPENROUTER_KEY_ENV)
            self._ai_paste(S.OPENROUTER_KEY_ENV)
            return True
        if a == "paste-venice":
            self._set_ai_for_key("custom", S.VENICE_BASE_URL, S.VENICE_KEY_ENV)
            self._ai_paste(S.VENICE_KEY_ENV)
            return True
        if a == "custom":
            return self._ai_custom()
        raise AssertionError("unknown AI menu choice")

    def _set_ai_for_key(self, kind: str, url: str, name: str) -> None:
        if kind == "openrouter":
            self._set_ai("openrouter")
        else:
            self._set_ai("custom", ref=url, key_env=name)

    def _ai_paste(self, name: str) -> None:
        if self.dec(f"val:{name}") in _FINAL_MODES and name in self._values:
            return
        if self._collision_kept(name):
            return
        self._paste(name)

    def _ai_custom(self) -> bool:
        while True:
            url = self.io.ask(S.AI_ASK_ADDRESS).strip()
            if url == BACK_INPUT:
                return False
            try:
                _validate_ai(AiChoice("custom", url))
            except ValueError:
                self.say(S.AI_ADDRESS_INVALID)   # never echoes the address (it may carry a password)
                continue
            break
        while True:
            key_env = self.io.ask(S.AI_ASK_KEY_NAME).strip()
            if key_env == "" or (len(key_env) <= 64 and _KEY_ENV_RE.fullmatch(key_env)):
                break
            self.say(S.AI_KEY_NAME_INVALID)
        self._set_ai("custom", ref=url, key_env=key_env)
        if key_env:
            self._ai_paste(key_env)
        return True

    # ---- AI_VERIFY

    def _st_ai_verify(self) -> State:
        choice = self._ai_choice()
        if choice.kind in ("none", "cli"):
            self.say(S.AI_NOTHING_TO_TEST)
            opts = [Option("continue", "Continue"), Option("back", "Back")]
        elif not self.d.online:
            self.say(f"AI check: {S.NEEDS_INTERNET}.")
            self.decide("ai.verify", "offline")
            opts = [Option("continue", "Continue"), Option("back", "Back")]
        else:
            opts = [Option("test", "Test it"), Option("skip", "Skip the test"), Option("back", "Back")]
        a = self.io.choose(S.AI_VERIFY_PROMPT, opts)
        if a == "back":
            return State.AI_ROUTE
        if a == "test":
            self._run_ai_check(choice)
        elif a == "skip":
            self.decide("ai.verify", "skipped")
        return State.BROKER_PICK

    def _run_ai_check(self, choice: AiChoice) -> None:
        try:
            result = self.d.verify_ai(choice)
        except Exception:  # noqa: BLE001
            self.say(S.AI_CHECK_CRASHED)
            self.decide("ai.verify", "failed")
            return
        text = str(result).strip()
        if text.lower() == "passed":
            self.say(S.AI_CHECK_PASSED)
            self.decide("ai.verify", "passed")
        elif S.NOT_TESTED in text.lower():
            self.say(S.AI_CHECK_NOT_TESTED)
            self.decide("ai.verify", "not-tested")
        else:
            self.say(S.AI_CHECK_FAILED.format(detail=self.safe(text)[:300]))
            self.decide("ai.verify", "failed")

    # ---- BROKER_PICK

    @staticmethod
    def _trading_text(e: dict) -> str:
        return {"true": "yes", "false": "no", "draft-only": "draft only"}.get(str(e.get("trading")), "not stated")

    def _entry_option(self, e: dict, *, flag_region: bool = False) -> Option:
        tags = []
        st = e.get("status") or []
        if "official" in st:
            tags.append(S.TAG_OFFICIAL)
        if "community" in st:
            tags.append(S.TAG_COMMUNITY)
        if "aggregator-only" in st or "none" in st:
            tags.append(S.TAG_NO_DIRECT)
        if flag_region and region_is_uncurated(e):
            tags.append(S.REGION_NOT_CURATED)
        label = e.get("name", e["slug"]) + (f" [{'; '.join(tags)}]" if tags else "")
        detail = (f"{e.get('region_raw') or 'region not stated'}. Trading: {self._trading_text(e)}. "
                  f"Last checked {e.get('last_verified', 'not stated')}.")
        return Option(f"pick:{e['slug']}", label, detail)

    def _st_broker_pick(self) -> State:
        entries = order_catalog(self.d.catalog)
        first = [e for e in entries if e["slug"] in _FIRST_SET]
        while True:
            opts = [Option("none", S.BROKER_NONE_LABEL, S.BROKER_NONE_DETAIL),
                    *(self._entry_option(e) for e in first),
                    Option("all", S.BROKER_ALL_LABEL), Option("search", S.BROKER_SEARCH_LABEL), Option("back", "Back")]
            a = self.io.choose(S.BROKER_PROMPT, opts)
            if a == "back":
                return State.AI_VERIFY
            if a == "none":
                self._set_broker("none", "")
                return State.SKILLS
            if a == "all":
                rest = [e for e in entries if e["slug"] not in _FIRST_SET]
                picked = self._pick_from(rest, flag_region=False)
            elif a == "search":
                picked = self._search_menu()
            else:
                picked = a
            if picked is None:
                continue
            nxt = self._select_entry(picked.split(":", 1)[1])
            if nxt is not None:
                return nxt

    def _pick_from(self, rows: Sequence[dict], *, flag_region: bool) -> str | None:
        opts = [*(self._entry_option(e, flag_region=flag_region) for e in rows), Option("back", "Back")]
        a = self.io.choose(S.BROKER_PROMPT, opts)
        return None if a == "back" else a

    def _search_menu(self) -> str | None:
        text = self.io.ask(S.SEARCH_TEXT).strip()
        if text == BACK_INPUT:
            return None
        region = self.io.ask(S.SEARCH_REGION).strip()
        if region == BACK_INPUT:
            return None
        f = self.io.choose(S.SEARCH_FILTER, [
            Option("any", "Anything"), Option("official", "Official only"), Option("trade", "Can trade"),
            Option("remote", "Hosted server"), Option("local", "Local server"), Option("back", "Back")])
        if f == "back":
            return None
        rows = search_catalog(self.d.catalog, text=text, region=region or None,
                              status="official" if f == "official" else None,
                              trading="true" if f == "trade" else None,
                              route_kind=f if f in ("remote", "local") else None)
        if not rows:
            self.say(S.SEARCH_NONE)
            return None
        return self._pick_from(rows, flag_region=bool(region))

    def _set_broker(self, slug: str, route_id: str) -> None:
        if (slug, route_id) != (self.dec("broker.slug"), self.dec("broker.route")):
            for n in _route_names(self._route() or {}):
                self._drop_value(n.name)
        self.decide("broker.slug", slug)
        self.decide("broker.route", route_id)

    def _select_entry(self, slug: str) -> State | None:
        entry = next((e for e in self.d.catalog if e["slug"] == slug), None)
        if entry is None:
            return None
        routes = installable_routes(entry)
        if not routes:
            banner = (entry.get("wizard") or {}).get("banner") or BANNER
            self.say(S.NOT_INSTALLABLE.format(name=entry.get("name", slug), banner=banner,
                                              page=entry.get("page_path") or "the catalog page",
                                              url=entry.get("source_url") or "no address listed"))
            return None
        route = routes[0]
        if len(routes) > 1:
            opts = [Option(f"route:{r['route_id']}", self._route_label(r)) for r in routes] + [Option("back", "Back")]
            a = self.io.choose(S.ROUTE_PROMPT.format(name=entry.get("name", slug)), opts)
            if a == "back":
                return None
            route = next(r for r in routes if f"route:{r['route_id']}" == a)
        self._set_broker(slug, route["route_id"])
        return State.BROKER_INSTALL

    @staticmethod
    def _route_label(r: dict) -> str:
        how = (r.get("paper") or {}).get("how_to_enable")
        return f"{r['route_id']} ({r.get('kind')}, {r.get('maintainer', 'maintainer not stated')})" + (
            f": {how}" if how else "")

    # ---- BROKER_INSTALL

    def _describe_route(self, entry: dict, route: dict) -> None:
        name = entry.get("name", entry["slug"])
        lines = [S.INSTALL_PROMPT.format(name=name)]
        if route["kind"] == "local-command":
            inst = route.get("install") or {}
            lines.append(f"How it connects: it runs a local program: {inst.get('command_text', 'not stated')}")
            if inst.get("needs"):
                lines.append("It needs: " + ", ".join(map(str, inst["needs"])))
        else:
            lines.append(f"How it connects: it talks to {route.get('url', 'an address not stated')}")
        paper = route.get("paper") or {}
        if paper.get("how_to_enable"):
            lines.append(f"Paper trading, in the broker page's own words: {paper['how_to_enable']}")
        elif paper.get("supported"):
            lines.append(f"Paper trading: {paper['supported']}")
        sub = route.get("submit") or {}
        if sub.get("can_submit_order"):
            lines.append(f"Orders, per the broker page: can submit {sub['can_submit_order']}; "
                         f"confirmation {sub.get('confirmation', 'not stated')}.")
        ro = route.get("read_only") or {}
        if ro.get("available"):
            lines.append(f"Read-only option, per the broker page: {ro.get('how', 'available')}")
        advice = (route.get("auth") or {}).get("key_scope_advice")
        if advice:
            lines.append(f"Tip from the page: {advice}")
        if (route.get("auth") or {}).get("headers"):
            lines.append(S.UNSET_SENTENCE)
        elif _route_names(route):
            lines.append(S.UNSET_SENTENCE)
        if not _route_names(route):
            lines.append(S.OAUTH_ONLY)
        self.say("\n".join(lines))

    def _st_broker_install(self) -> State:
        entry, route = self._entry(), self._route()
        if entry is None or route is None:
            return State.BROKER_PICK
        self._describe_route(entry, route)
        names = _route_names(route)
        opts = [Option("continue", "Continue")]
        if any(self.dec(f"val:{n.name}") for n in names):
            opts.append(Option("redo", "Enter these values again"))
        opts.append(Option("back", "Back"))
        a = self.io.choose(f"Connect {entry.get('name', entry['slug'])}.", opts)
        if a == "back":
            return State.BROKER_PICK
        if a == "redo":
            for n in names:
                self._drop_value(n.name)
        for n in names:
            self._obtain(n.name, secret=n.secret, kinds=("broker-key",))
        return State.BROKER_VERIFY

    # ---- BROKER_VERIFY

    def _st_broker_verify(self) -> State:
        entry = self._entry()
        name = entry.get("name", entry["slug"]) if entry else "the broker"
        status = S.NEEDS_INTERNET if not self.d.online else S.NOT_TESTED
        self.decide("broker.verify", "offline" if not self.d.online else "pending")
        self.say(S.BROKER_VERIFY_LATER.format(status=status))
        a = self.io.choose(S.BROKER_VERIFY_PROMPT.format(name=name),
                           [Option("continue", "Continue"), Option("back", "Back")])
        return State.BROKER_INSTALL if a == "back" else State.SKILLS

    # ---- SKILLS (R27)

    def _st_skills(self) -> State:
        libs = listed_only(self.d.libraries)   # an unlisted library is never offered
        picked = {i for i in self.dec("skills").split(",") if i}
        picked &= {lib.id for lib in libs}
        if not libs:
            self.say(S.SKILLS_NONE_LISTED)
        while True:
            opts = []
            for lib in libs:
                mark = "[x]" if lib.id in picked else "[ ]"
                keys = f" Needs keys: {', '.join(lib.needs_keys)}." if lib.needs_keys else ""
                opts.append(Option(f"lib:{lib.id}", f"{mark} {lib.name} ({lib.license})",
                                   f"{lib.kind} library.{keys}"))
            opts += [Option("done", "Done"), Option("back", "Back")]
            a = self.io.choose(S.SKILLS_PROMPT, opts)
            if a == "back":
                return State.BROKER_VERIFY if self._route() else State.BROKER_PICK
            if a == "done":
                self.decide("skills", ",".join(sorted(picked)))
                self.decide("skills.done", "1")
                return State.DATA_KEYS
            lid = a.split(":", 1)[1]
            picked ^= {lid}
            self.decide("skills", ",".join(sorted(picked)))

    # ---- DATA_KEYS (R27: names come only from the picked libraries)

    def _st_data_keys(self) -> State:
        names = self._data_names()
        if not names:
            self.say(S.DATA_KEYS_NONE)
            a = self.io.choose("Optional data keys.", [Option("continue", "Continue"), Option("back", "Back")])
            return State.SKILLS if a == "back" else State.REVIEW
        a = self.io.choose(S.DATA_KEYS_PROMPT.format(names=", ".join(names)),
                           [Option("add", "Add keys"), Option("skip", "Skip"), Option("back", "Back")])
        if a == "back":
            return State.SKILLS
        for n in names:
            if a == "add":
                self._obtain(n, secret=True, kinds=("data-key",))
            elif not self.dec(f"val:{n}"):
                self.decide(f"val:{n}", "skip")
        if a == "add" and not self.d.online:
            self.say(S.DATA_KEYS_OFFLINE)
        return State.REVIEW

    # ---- REVIEW

    def _broker_name(self) -> str:
        e = self._entry()
        return e.get("name", e["slug"]) if e else ""

    def _review_text(self) -> str:
        choice = self._ai_choice()
        lines = [S.REVIEW_HEAD, S.REVIEW_PROFILE.format(name=self.dec("profile_name"))]
        brand = _CLI_NAMES.get(choice.ref, ("", "your CLI"))[1]
        lines.append(S.REVIEW_AI.get(choice.kind, S.REVIEW_AI["none"]).format(brand=brand, ref=choice.ref))
        if self._route() is not None:
            lines.append(S.REVIEW_BROKER.format(name=self._broker_name()))
        written = [n for n in self._expected_names() if n in self._values]
        if written:
            lines.append(S.REVIEW_KEYS.format(n=len(written), names=", ".join(written)))
        skills = [lib.id for lib in self._selected_libs()]
        if skills:
            lines.append(S.REVIEW_SKILLS.format(n=len(skills), y="y" if len(skills) == 1 else "ies",
                                                names=", ".join(skills)))
        lines.append(S.REVIEW_NOCHANGE)
        return "\n".join(lines)

    def _st_review(self) -> State:
        self._ensure_values()
        a = self.io.choose(self._review_text(), [Option("do", "Do it"), Option("back", "Back")])
        return State.DATA_KEYS if a == "back" else State.WRITE

    # ---- WRITE

    def _build_plan(self) -> WritePlan:
        route = self._route()
        broker = BrokerChoice(slug=self.dec("broker.slug"), route_id=self.dec("broker.route"), entry=route) if route else None
        env = {n: self._values[n] for n in self._expected_names() if n in self._values}
        return WritePlan(profile_name=self.dec("profile_name"), ai=self._ai_choice(), broker=broker,
                         skills=[lib.id for lib in self._selected_libs()], env_values=env)

    def _st_write(self) -> State:
        plan = self._build_plan()
        env = plan.env_values   # the dict this module built: emptied below so no value outlives WRITE
        try:
            self.receipt = write_profile(plan, hermes=self.d.hermes, source_dir=self.d.source_dir,
                                         store_factory=self.d.store_factory)
        except ProfileExists:
            self.go(State.REVIEW)
            self.say(S.WRITE_EXISTS.format(name=plan.profile_name))
            a = self.io.choose(S.WRITE_WHAT_NOW, [Option("rename", "Pick another name"), Option("back", "Back")])
            if a == "rename":
                self.forget("profile_name")
                return State.PRECHECK
            return State.REVIEW
        except WriteError as e:
            self.go(State.REVIEW)
            self.say(S.WRITE_FAILED.format(report=self.safe(e.report)))
            return self._write_failed()
        except Exception as e:  # noqa: BLE001
            self.go(State.REVIEW)
            detail = self.safe(e) if isinstance(e, ValueError) else type(e).__name__
            self.say(S.WRITE_FAILED_PLAIN.format(detail=detail))
            return self._write_failed()
        finally:
            if isinstance(env, dict):
                env.clear()
        self._values.clear()
        return State.VERIFY

    def _write_failed(self) -> State:
        a = self.io.choose(S.WRITE_WHAT_NOW, [Option("review", "Back to the review"), Option("stop", "Stop here")])
        if a == "stop":
            raise _Quit
        return State.REVIEW

    # ---- VERIFY and DONE

    def _verify_lines(self) -> list[str]:
        lines: list[str] = []
        kind = self.dec("ai.kind", "none")
        if kind not in ("none", "cli"):
            if not self.d.online:
                lines.append(f"AI check: {S.NEEDS_INTERNET}.")
            elif self.dec("ai.verify") == "passed":
                lines.append(S.AI_CHECK_PASSED)
            else:
                lines.append(S.AI_CHECK_NOT_TESTED)
        if self.dec("broker.slug", "none") != "none":
            status = S.NEEDS_INTERNET if not self.d.online else S.NOT_TESTED
            lines.append(f"Broker connection: {status}.")
        return lines

    def _st_verify(self) -> State:
        lines = self._verify_lines()
        if lines:
            self.say(S.VERIFY_HEAD + "\n" + "\n".join(lines))
        return State.DONE

    def _left_to_do(self) -> list[str]:
        out: list[str] = []
        kind = self.dec("ai.kind", "none")
        if kind in ("none", "cli"):
            out.append(S.LATER_AI)
        elif not self.d.online:
            out.append(S.LATER_AI_TEST.format(status=S.NEEDS_INTERNET))
        elif self.dec("ai.verify") != "passed":
            out.append(S.LATER_AI_TEST.format(status=S.NOT_TESTED))
        route = self._route()
        if route is None:
            out.append(S.LATER_BROKER)
        else:
            if not _route_names(route):
                out.append(S.LATER_OAUTH.format(name=self._broker_name()))
            status = S.NEEDS_INTERNET if not self.d.online else S.NOT_TESTED
            out.append(S.LATER_BROKER_TEST.format(name=self._broker_name(), status=status))
        for n in self._expected_names():
            if self.dec(f"val:{n}") == "skip":
                out.append(S.LATER_NAME.format(name=n))
        return out

    def _st_done(self) -> None:
        choice = self._ai_choice()
        ai = {"none": "none yet", "cli": "none yet", "openrouter": "OpenRouter", "custom": "OpenAI-compatible service",
              "ollama": "Ollama", "lm-studio": "LM Studio"}.get(choice.kind, "none yet")
        broker = self._broker_name() or "none"
        text = S.DONE_TITLE + "\n" + S.DONE_BODY.format(ai=ai, broker=broker, n=len(self._selected_libs()))
        later = self._left_to_do()
        if later:
            text += "\n" + S.FINISH_LATER_TITLE + "\n" + "\n".join(f"- {x}" for x in later)
        text += "\n" + S.DONE_UNDO.format(name=self.dec("profile_name"))
        self.say(text)
        return None


def run_wizard(io: IO, deps: Deps) -> Receipt | None:
    """Run (or resume) the wizard. Returns the Receipt that ``write_profile`` returned, or None if the
    user stopped, or the write did not complete."""
    return _Wizard(io, deps).run()
