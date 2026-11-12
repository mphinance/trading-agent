"""The first-run wizard: a table-driven state machine, one method per state.

Rules this module keeps (FIRST_RUN.md, AUTODETECTION.md section 9, global constraints):

* Secret VALUES live only in ``ValueMixin._values`` (memory) and the ``WritePlan`` handed to ``write_profile``.
  Consent to copy a found key is per consumer and per process (values.py); a restart asks again.
* The state file holds decision tokens and finding ids, never a value, and every saved answer is re-validated
  on load against its domain (a hand-edited or hostile file cannot steer the wizard).
* Nothing is pre-checked, nothing is required, every screen has Back where there is a place to go back to.
* Every write goes through ``write_profile`` into the one new profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from trading_agent.detect.binaries import AI_CLIS, detect_ai_clis
from trading_agent.detect.env import detect_env, load_credential_specs
from trading_agent.detect.mcp_files import detect_mcp_entries
from trading_agent.detect.ports import probe_ports
from trading_agent.findings import Finding
from trading_agent.libraries import Library, listed_only
from trading_agent.platform import Platform
from trading_agent.profile.hermes_cli import (
    HermesCli, HermesNotBootstrapped, HermesUnavailable, parse_hermes_version, validate_profile_name,
)
from trading_agent.profile.receipt import Receipt, ReceiptError
from trading_agent.profile.writer import (
    AiChoice, BrokerChoice, ProfileExists, WriteError, WritePlan, receipt_owns_profile, validate_ai_choice,
    write_profile,
)
from trading_agent.secrets import SecretStore, redact_patterns, scrub
from trading_agent.wizard import screens as S
from trading_agent.wizard.catalog import (  # noqa: F401 - re-exported: the public catalog API lives here too
    NameSpec, entry_tags, installable_routes, is_installable, order_catalog, plain, region_is_uncurated,
    route_destination, route_fingerprint, route_label, route_names, search_catalog, submit_words, trading_text,
)
from trading_agent.wizard.io import BACK_INPUT, IO, Option
from trading_agent.wizard.state import State, WizardState
from trading_agent.wizard.values import BackSignal, Consumer, ValueMixin, cap
from urllib.parse import urlsplit

_FIRST_SET = frozenset(S.FIRST_SCREEN_SLUGS)
_KEY_ENV_RE = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+")   # at least one underscore: a pasted key is not a name
_AI_KINDS = ("none", "cli", "openrouter", "custom", "ollama", "lm-studio")


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
# key name -> (label, cost line, AiChoice kind, base url)
_KEY_PROVIDERS = {
    S.OPENROUTER_KEY_ENV: ("OpenRouter", "One key, many models, pay as you go. Cheap models cost cents.",
                           "openrouter", ""),
    S.VENICE_KEY_ENV: ("Venice", "Uses your Venice account. Cost depends on the model you pick.",
                       "custom", S.VENICE_BASE_URL),
    S.OPENAI_KEY_ENV: ("OpenAI", "Pay as you go. Cost depends on the model you pick.",
                       "custom", S.OPENAI_BASE_URL),
}
_UNSUPPORTED_AI_KEYS = ("ANTHROPIC_API_KEY",)
_VERSION_PIN = S.HERMES_MIN_VERSION


class _Quit(Exception):
    """The user chose to stop here. State is already saved."""


def _ai_provider_label(kind: str, ref: str) -> str:
    if kind == "custom":
        return {S.VENICE_BASE_URL: "Venice", S.OPENAI_BASE_URL: "OpenAI"}.get(ref, "an OpenAI-compatible service")
    return {"openrouter": "OpenRouter", "ollama": "Ollama", "lm-studio": "LM Studio"}.get(kind, "no AI yet")


# ---------------------------------------------------------------- the wizard

class _Wizard(ValueMixin):
    def __init__(self, io: IO, deps: Deps) -> None:
        self.io = io
        self.d = deps
        self.p = deps.platform
        self._save_failed = False
        self.ws = WizardState.load(self.p)
        if self.ws.current is State.DONE:       # a finished run is history: a new run starts fresh
            self.ws = WizardState()
        self._init_values()
        self._findings: list[Finding] | None = None
        self._could_not: list[str] = []
        self.receipt: Receipt | None = None
        self._validate_decisions()
        self._save()

    # ---- plumbing

    def say(self, text: str) -> None:
        self.io.say(text)

    def _save(self) -> None:
        try:
            self.ws.save(self.p)
        except OSError:
            if not self._save_failed:
                self._save_failed = True
                self.say(S.STATE_NOT_SAVED)

    def go(self, state: State) -> State:
        self.ws.current = state
        self._save()
        return state

    def decide(self, key: str, value: str) -> None:
        self.ws.set(key, value)
        self._save()

    def forget(self, *keys: str) -> None:
        for k in keys:
            self.ws.forget(k)
        self._save()

    def dec(self, key: str, default: str = "") -> str:
        return self.ws.decisions.get(key, default)

    def profile_dir(self) -> Path:
        return self.d.hermes.profile_dir(self.dec("profile_name") or S.DEFAULT_PROFILE_NAME)

    def safe(self, text: object) -> str:
        """Text that may have touched a value: known values scrubbed, then credential shapes redacted."""
        return redact_patterns(scrub(str(text), list(self._values.values())))

    def run(self) -> Receipt | None:
        self._resume_fixups()
        table = self._table()
        try:
            while True:
                nxt = table[self.ws.current]()
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

    # ---- saved state: validate every answer against its domain

    def _validate_decisions(self) -> None:
        dropped = False
        name = self.dec("profile_name")
        if name:
            try:
                validate_profile_name(name)
            except ValueError:
                self.ws.forget("profile_name")
                dropped = True
        dropped |= self._validate_ai_decisions()
        dropped |= self._validate_broker_decisions()
        ids = [i for i in self.dec("skills").split(",") if i]
        ok = [i for i in ids if i in {lib.id for lib in listed_only(self.d.libraries)}]
        if ok != ids:
            self.ws.set("skills", ",".join(ok))
            dropped = True
        if self.dec("scan.apps") not in ("", "yes", "no"):
            self.ws.forget("scan.apps")
            dropped = True
        if self.ws.current.value in ("WRITE", "VERIFY") and not self.dec("profile_name"):
            self.ws.current = State.PRECHECK
        if dropped:
            self.say(S.STATE_DROPPED)
        self._prune(self._expected_pairs())

    def _validate_ai_decisions(self) -> bool:
        keys = ("ai.kind", "ai.ref", "ai.key_env")
        if not any(self.dec(k) for k in keys):
            return False
        try:
            if self.dec("ai.kind") not in _AI_KINDS:
                raise ValueError("kind")
            validate_ai_choice(self._ai_choice())
            return False
        except ValueError:
            for k in keys:
                self.ws.forget(k)
            return True

    def _validate_broker_decisions(self) -> bool:
        slug = self.dec("broker.slug")
        if slug in ("", "none"):
            return False
        route = self._route()
        if route is None:
            for k in ("broker.slug", "broker.route", "broker.fp"):
                self.ws.forget(k)
            return True
        if self.dec("broker.fp") != route_fingerprint(route):
            self._revoke_consumer(self._broker_key())
            self.ws.set("broker.fp", route_fingerprint(route))
            self.say(S.STALE_NOTE)
        return False

    def _resume_fixups(self) -> None:
        """Interrupted at WRITE or VERIFY: trust a saved receipt only if the profile it describes still exists
        and carries its ownership marker; otherwise review again."""
        if self.ws.current not in (State.WRITE, State.VERIFY):
            return
        rec = None
        name = self.dec("profile_name")
        if name:
            try:
                rec = Receipt.load(self.p, name)
            except (ReceiptError, ValueError):
                rec = None
        if rec is not None and "skills" in rec.steps_done and receipt_owns_profile(rec, self.d.hermes):
            self.receipt = rec
            self.go(State.VERIFY)
        else:
            self.go(State.REVIEW)

    # ---- findings (names only), loaded lazily and always live

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
        return next((f for f in self.findings() if f.id == fid and f.state == "present"), None)

    # ---- START

    def _st_start(self) -> State:
        while True:
            a = self.io.choose(S.START, [Option("start", "Start"), Option("what", "What does this do?")])
            if a == "what":
                self.say(S.WHAT_IT_DOES)
                continue
            return State.PRECHECK

    # ---- PRECHECK

    def _version_problem(self, line: str) -> str | None:
        """None if fine, 'old' if below the pin or a pre-release, 'unreadable' if no version could be read."""
        parsed = parse_hermes_version(line)
        if parsed is None:
            return "unreadable"
        triple, pre = parsed
        return "old" if pre or triple < _VERSION_PIN else None

    def _check_once(self) -> tuple[str, list[Option]]:
        """One look at Hermes: ('ok'|'problem', extra options for the next prompt)."""
        if self.p.which("hermes") is None:
            self.say(S.HERMES_MISSING)
            self.say(S.HERMES_INSTALL_HINT.format(cmd=S.INSTALL_CMD))
            return "problem", []
        try:
            line = self.d.hermes.version()
        except HermesNotBootstrapped:
            self.say(S.HERMES_NOT_BOOTSTRAPPED)
            return "ok", []
        except HermesUnavailable:
            self.say(S.HERMES_FAILED)
            return "problem", []
        problem = self._version_problem(line)
        if problem is None:
            return "ok", []
        if problem == "old":
            self.say(S.HERMES_OUT_OF_RANGE.format(requires=S.HERMES_REQUIRES, version=line))
            self.say(S.HERMES_UPDATE_HINT)
            return "problem", []
        self.say(S.HERMES_VERSION_UNREADABLE)
        return "problem", [Option("continue", "Continue anyway")]

    def _hermes_check(self) -> str:
        """'ok' or 'back'; raises _Quit on Stop. Never runs an install command."""
        while True:
            result, extra = self._check_once()
            if result == "ok":
                return "ok"
            a = self.io.choose(S.HERMES_WHAT_NOW, [*extra, Option("again", "Check again"),
                                                    Option("stop", "Stop here for now"), Option("back", "Back")])
            if a == "continue":
                return "ok"
            if a == "back":
                return "back"
            if a == "stop":
                raise _Quit

    def _next_name(self, current: str | None) -> str | None:
        """Ask for a valid profile name. None means Back."""
        if current is not None:
            return current
        while True:
            raw = self.io.ask(S.NAME_PROMPT).strip()
            if raw == BACK_INPUT:
                return None
            cand = raw or S.DEFAULT_PROFILE_NAME
            try:
                validate_profile_name(cand)
            except ValueError:
                self.say(S.NAME_INVALID)   # never echoes what was typed
                continue
            return cand

    def _st_precheck(self) -> State:
        if not self.d.online:
            self.say(S.OFFLINE_NOTE)
        if self._hermes_check() == "back":
            return State.START
        name: str | None = self.dec("profile_name") or None
        if name is not None:
            a = self.io.choose(S.NAME_KEEP.format(name=name), [
                Option("continue", "Continue"), Option("rename", "Pick another name"), Option("back", "Back")])
            if a == "back":
                return State.START
            name = None if a == "rename" else name
        while True:
            name = self._next_name(name)
            if name is None:
                return State.START
            if not self.d.hermes.profile_exists(name):
                break
            a = self.io.choose(S.PROFILE_TAKEN.format(name=name),
                               [Option("rename", "Pick another name"), Option("back", "Back")])
            if a == "back":
                return State.START
            name = None
        if name != self.dec("profile_name"):
            self._forget_keep_existing()
        self.decide("profile_name", name)
        return State.SCAN

    def _forget_keep_existing(self) -> None:
        for k, v in list(self.ws.decisions.items()):
            if v == "keep-existing":
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

    # ---- what is chosen and what that implies (consumers of values)

    def _ai_choice(self) -> AiChoice:
        kind = self.dec("ai.kind", "none")
        if kind == "cli":
            return AiChoice("cli", self.dec("ai.ref"))
        if kind == "custom":
            return AiChoice("custom", self.dec("ai.ref"), self.dec("ai.key_env"))
        return AiChoice(kind, self.dec("ai.ref"), self.dec("ai.key_env"))  # type: ignore[arg-type]

    def _ai_value_name(self) -> str | None:
        kind = self.dec("ai.kind")
        if kind == "openrouter":
            return S.OPENROUTER_KEY_ENV
        return (self.dec("ai.key_env") or None) if kind == "custom" else None

    def _entry(self) -> dict | None:
        slug = self.dec("broker.slug")
        return next((e for e in self.d.catalog if e["slug"] == slug), None) if slug not in ("", "none") else None

    def _route(self) -> dict | None:
        entry = self._entry()
        if entry is None:
            return None
        return next((r for r in installable_routes(entry) if r.get("route_id") == self.dec("broker.route")), None)

    def _broker_key(self) -> str:
        return f"broker:{self.dec('broker.slug')}/{self.dec('broker.route')}"

    def _broker_name(self) -> str:
        e = self._entry()
        return e.get("name", e["slug"]) if e else ""

    def _selected_libs(self) -> list[Library]:
        ids = [i for i in self.dec("skills").split(",") if i]
        return [lib for lib in listed_only(self.d.libraries) if lib.id in ids]

    def _ai_consumer(self) -> Consumer:
        kind, ref = self.dec("ai.kind"), self.dec("ai.ref")
        who = _ai_provider_label(kind, ref)
        if kind == "custom" and ref not in (S.VENICE_BASE_URL, S.OPENAI_BASE_URL):
            host = (urlsplit(ref).hostname or "an address not stated")[:80]   # ASCII printable: validated on input
            return Consumer("ai", f"the AI service at {host}", f"AI service at {host}", ("ai-key",))
        return Consumer("ai", f"the {who} AI connection", who, ("ai-key",))

    def _consumers(self) -> list[tuple[Consumer, list[NameSpec]]]:
        out: list[tuple[Consumer, list[NameSpec]]] = []
        ai = self._ai_value_name()
        if ai:
            out.append((self._ai_consumer(), [NameSpec(ai, True)]))
        route = self._route()
        if route:
            nm = self._broker_name()
            out.append((Consumer(self._broker_key(), f"the {nm} connection", nm, ("broker-key",)),
                        route_names(route)))
        for lib in self._selected_libs():
            if lib.needs_keys:
                out.append((Consumer(f"lib:{lib.id}", f"the {lib.name} library", lib.name, ("data-key",)),
                            [NameSpec(n, True) for n in lib.needs_keys]))
        return [(c, specs) for c, specs in out if c.key not in self.refused]

    def _expected_pairs(self) -> set[tuple[str, str]]:
        return {(c.key, s.name) for c, specs in self._consumers() for s in specs}

    def _data_consumers(self) -> list[tuple[Consumer, list[NameSpec]]]:
        return [(c, specs) for c, specs in self._consumers() if c.key.startswith("lib:")]

    def _data_names(self) -> list[str]:
        return sorted({s.name for _, specs in self._data_consumers() for s in specs})

    def _ensure_values(self) -> None:
        """Before REVIEW: every expected name is decided for its consumer. After a restart nothing is held,
        so each one is asked about again (same prompts, per consumer)."""
        self._prune(self._expected_pairs())
        for c, specs in self._consumers():
            for s in specs:
                self._obtain(c, s)
        self.resolve_sharing()

    def _plan_ai(self) -> AiChoice:
        """What the plan configures: nothing for an AI left out because it was refused a shared value."""
        return AiChoice("none") if "ai" in self.refused else self._ai_choice()

    def _plan_route(self) -> dict | None:
        return None if self._broker_key() in self.refused else self._route()

    def _plan_libs(self) -> list[Library]:
        return [lib for lib in self._selected_libs() if f"lib:{lib.id}" not in self.refused]

    def _plan_values(self) -> dict[str, str]:
        self._prune(self._expected_pairs())
        granted = {n for _, n in self._grants}
        return {n: v for n, v in self._values.items() if n in granted}

    # ---- AI_ROUTE (v1 rulings R23, R26, R29)

    def _clis_found(self) -> list[str]:
        return [n for n in AI_CLIS if self._present(f"cli:{n}") is not None]

    def _set_ai(self, kind: str, *, ref: str = "", key_env: str = "") -> None:
        self.refused.discard("ai")
        if (kind, ref, key_env) != (self.dec("ai.kind"), self.dec("ai.ref"), self.dec("ai.key_env")):
            self._revoke_consumer("ai")     # a different provider or address never inherits an earlier answer
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
            f = self._present(f"env:{name}")
            if (not private or name == S.VENICE_KEY_ENV) and f is not None and f.kind == "ai-key" \
                    and not self.dec(f"declined:{f.id}"):
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
        detail = f"No brain yet. Your {_CLI_NAMES[clis[0]][1]} sign-in stays untouched." if clis \
            else "No brain yet. You can add one later."
        opts += [Option("skip", "Skip for now", detail), Option("back", "Back")]
        first = opts[0]   # We pick, you confirm: the first option is the best CONFIGURABLE one. Nothing is pre-checked.
        if first.id not in ("private", "skip", "back"):
            opts[0] = Option(first.id, first.label + " (Recommended)", first.detail)
        return opts

    def _say_ai_notes(self) -> None:
        clis = self._clis_found()
        if clis:
            labels = [_CLI_NAMES[n] for n in clis]
            tmpl = S.AI_CLI_NOTE if len(clis) == 1 else S.AI_CLI_NOTE_MANY
            self.say(tmpl.format(label=" and ".join(x[0] for x in labels),
                                 brand=" and ".join(dict.fromkeys(x[1] for x in labels))))
        for name in _UNSUPPORTED_AI_KEYS:
            if self._present(f"env:{name}") is not None:
                self.say(S.AI_UNSUPPORTED_KEY.format(name=name))
        if not any(self._present(f"ai-local:{k}") for k, _ in _LOCAL):
            self.say(S.AI_NO_LOCAL)

    def _st_ai_route(self) -> State:
        self._say_ai_notes()
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
            elif a == "skip":
                clis = self._clis_found()
                self._set_ai("cli", ref=clis[0]) if clis else self._set_ai("none")  # cli only on an explicit defer
                return State.AI_VERIFY
            elif self._ai_pick(a):
                return State.AI_VERIFY

    def _ai_pick(self, a: str) -> bool:
        """Apply a menu choice. False means 'back to the menu' (declined, backed out, or not a choice I know)."""
        if a.startswith("use:ai-local:"):
            self._set_ai(a.split(":", 2)[2])
            return True
        if a.startswith("use:env:") and a.split(":", 2)[2] in _KEY_PROVIDERS:
            return self._ai_use_found_key(a.split(":", 2)[2])
        if a == "paste-openrouter":
            return self._ai_paste("openrouter", "", S.OPENROUTER_KEY_ENV)
        if a == "paste-venice":
            return self._ai_paste("custom", S.VENICE_BASE_URL, S.VENICE_KEY_ENV)
        if a == "custom":
            return self._ai_custom()
        self.say(S.INVALID_CHOICE)
        return False

    def _set_ai_for_key(self, kind: str, url: str, name: str) -> None:
        self._set_ai("openrouter") if kind == "openrouter" else self._set_ai("custom", ref=url, key_env=name)

    def _ai_use_found_key(self, name: str) -> bool:
        _label, _cost, kind, url = _KEY_PROVIDERS[name]
        old = (self.dec("ai.kind"), self.dec("ai.ref"), self.dec("ai.key_env"))
        self._set_ai_for_key(kind, url, name)
        if self._obtain(self._ai_consumer(), NameSpec(name, True), how="card") == "declined":
            if old[0]:   # declined: the menu comes back with the earlier choice as it was
                self._set_ai(old[0], ref=old[1], key_env=old[2])
            else:
                self.forget("ai.kind", "ai.ref", "ai.key_env")
            return False
        return True

    def _ai_paste(self, kind: str, url: str, name: str) -> bool:
        self._set_ai_for_key(kind, url, name)
        self._obtain(self._ai_consumer(), NameSpec(name, True), how="paste")
        return True

    def _ask_address(self) -> str | None:
        while True:
            url = self.io.ask(S.AI_ASK_ADDRESS).strip()
            if url == BACK_INPUT:
                return None
            try:
                validate_ai_choice(AiChoice("custom", url))
            except ValueError:
                self.say(S.AI_ADDRESS_INVALID)   # never echoes the address (it may carry a password)
                continue
            return url

    def _ask_key_name(self) -> str | None:
        while True:
            key_env = self.io.ask(S.AI_ASK_KEY_NAME).strip()
            if key_env == BACK_INPUT:
                return None
            if key_env == "" or (len(key_env) <= 64 and _KEY_ENV_RE.fullmatch(key_env)):
                return key_env
            self.say(S.AI_KEY_NAME_INVALID)

    def _ai_custom(self) -> bool:
        url = self._ask_address()
        key_env = self._ask_key_name() if url is not None else None
        if url is None or key_env is None:
            return False
        self._set_ai("custom", ref=url, key_env=key_env)
        if key_env:
            self._obtain(self._ai_consumer(), NameSpec(key_env, True), how="paste")
        return True

    # ---- AI_VERIFY

    def _st_ai_verify(self) -> State:
        choice = self._ai_choice()
        real = self.d.verify_ai is not default_verify_ai
        if choice.kind in ("none", "cli"):
            prompt, opts = S.AI_NOTHING_TO_TEST, [Option("continue", "Continue"), Option("back", "Back")]
        elif not self.d.online:
            self.decide("ai.verify", "offline")
            prompt, opts = S.AI_OFFLINE_PROMPT, [Option("continue", "Continue"), Option("back", "Back")]
        elif not real:
            prompt, opts = S.AI_VERIFY_STUB, [Option("continue", "Continue"), Option("back", "Back")]
        else:
            prompt = S.AI_VERIFY_PROMPT
            opts = [Option("test", "Test it"), Option("skip", "Skip the test"), Option("back", "Back")]
        a = self.io.choose(prompt, opts)
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

    def _entry_option(self, e: dict, *, flag_region: bool = False) -> Option:
        tags = entry_tags(e, flag_region=flag_region)
        label = e.get("name", e["slug"]) + (f" [{'; '.join(tags)}]" if tags else "")
        detail = (f"{e.get('region_raw') or 'region not stated'}. Trading: {trading_text(e)}. "
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
            picked = (self._pick_from(entries, flag_region=False) if a == "all"
                      else self._search_menu() if a == "search" else a)
            nxt = self._select_entry(picked.split(":", 1)[1]) if picked and picked.startswith("pick:") else None
            if nxt is not None:
                return nxt

    def _pick_from(self, rows: Sequence[dict], *, flag_region: bool) -> str | None:
        opts = [*(self._entry_option(e, flag_region=flag_region) for e in rows), Option("back", "Back")]
        a = self.io.choose(S.BROKER_PROMPT, opts)
        return None if a == "back" else a

    def _search_menu(self) -> str | None:
        text = self.io.ask(S.SEARCH_TEXT).strip()
        region = self.io.ask(S.SEARCH_REGION).strip() if text != BACK_INPUT else BACK_INPUT
        if BACK_INPUT in (text, region):
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
        if region and all(region_is_uncurated(e) for e in self.d.catalog):
            self.say(S.REGION_NONE_CURATED)
        if not rows:
            self.say(S.SEARCH_NONE)
            return None
        return self._pick_from(rows, flag_region=bool(region))

    def _set_broker(self, slug: str, route_id: str) -> None:
        self.refused.discard(self._broker_key())
        self.refused.discard(f"broker:{slug}/{route_id}")
        if (slug, route_id) != (self.dec("broker.slug"), self.dec("broker.route")):
            self.forget("broker.fp")
        self.decide("broker.slug", slug)
        self.decide("broker.route", route_id)
        route = self._route()
        if route is not None:
            self.decide("broker.fp", route_fingerprint(route))
        self._prune(self._expected_pairs())

    def _select_entry(self, slug: str) -> State | None:
        entry = next((e for e in self.d.catalog if e["slug"] == slug), None)
        if entry is None:
            return None
        routes = installable_routes(entry)
        if not routes:
            self.say(S.NOT_INSTALLABLE.format(
                name=entry.get("name", slug), banner=(entry.get("wizard") or {}).get("banner") or S.BANNER,
                page=entry.get("page_path") or "the catalog page", url=entry.get("source_url") or "no address listed"))
            return None
        route = routes[0]
        if len(routes) > 1:
            opts = [Option(f"route:{r['route_id']}", route_label(r)) for r in routes] + [Option("back", "Back")]
            a = self.io.choose(S.ROUTE_PROMPT.format(name=entry.get("name", slug)), opts)
            if a == "back":
                return None
            route = next(r for r in routes if f"route:{r['route_id']}" == a)
        self._set_broker(slug, route["route_id"])
        return State.BROKER_INSTALL

    # ---- BROKER_INSTALL

    def _describe_route(self, entry: dict, route: dict) -> None:
        lines = [S.INSTALL_PROMPT.format(name=entry.get("name", entry["slug"]))]
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
            lines.append(f"Paper trading: {plain(paper['supported'])}")
        if (route.get("submit") or {}).get("can_submit_order"):
            lines.append("Orders, per the broker page: " + submit_words(route["submit"]) + ".")
        if (route.get("read_only") or {}).get("available"):
            lines.append(f"Read-only option, per the broker page: {route['read_only'].get('how', 'available')}")
        if (route.get("auth") or {}).get("key_scope_advice"):
            lines.append(f"Tip from the page: {route['auth']['key_scope_advice']}")
        lines.append(S.UNSET_SENTENCE if route_names(route) else S.OAUTH_ONLY)
        self.say("\n".join(lines))

    def _st_broker_install(self) -> State:
        entry, route = self._entry(), self._route()
        if entry is None or route is None:
            return State.BROKER_PICK
        self._describe_route(entry, route)
        c, specs = next((c, s) for c, s in self._consumers() if c.key == self._broker_key())
        opts = [Option("continue", "Continue")]
        if any(self._decided(c, s.name) for s in specs):
            opts.append(Option("redo", "Enter these values again"))
        a = self.io.choose(f"Connect {self._broker_name()}.", [*opts, Option("back", "Back")])
        if a == "back":
            return State.BROKER_PICK
        if a == "redo":
            self._revoke_consumer(c.key)
            self.refused.discard(c.key)
        try:
            for s in specs:
                self._obtain(c, s)
        except BackSignal:
            return State.BROKER_PICK
        return State.BROKER_VERIFY

    # ---- BROKER_VERIFY

    def _st_broker_verify(self) -> State:
        status = S.NEEDS_INTERNET if not self.d.online else S.NOT_TESTED
        self.decide("broker.verify", "offline" if not self.d.online else "pending")
        self.say(S.BROKER_VERIFY_LATER.format(name=self._broker_name() or "the broker", status=status))
        a = self.io.choose(S.BROKER_VERIFY_PROMPT.format(name=self._broker_name() or "the broker"),
                           [Option("continue", "Continue"), Option("back", "Back")])
        return State.BROKER_INSTALL if a == "back" else State.SKILLS

    # ---- SKILLS (R27)

    def _st_skills(self) -> State:
        libs = listed_only(self.d.libraries)   # an unlisted library is never offered
        picked = {i for i in self.dec("skills").split(",") if i} & {lib.id for lib in libs}
        if not libs:
            self.say(S.SKILLS_NONE_LISTED)
        while True:
            opts = []
            for lib in libs:
                keys = f" Needs keys: {', '.join(lib.needs_keys)}." if lib.needs_keys else ""
                opts.append(Option(f"lib:{lib.id}", f"{'[x]' if lib.id in picked else '[ ]'} {lib.name} ({lib.license})",
                                   f"{lib.kind} library.{keys}"))
            a = self.io.choose(S.SKILLS_PROMPT, [*opts, Option("done", "Done"), Option("back", "Back")])
            if a == "back":
                return State.BROKER_VERIFY if self._route() else State.BROKER_PICK
            if a == "done":
                self.decide("skills.done", "1")
                return State.DATA_KEYS
            if a.startswith("lib:") and a[4:] in {lib.id for lib in libs}:
                picked ^= {a[4:]}
                self.refused.discard(a)
                self.decide("skills", ",".join(sorted(picked)))
            else:
                self.say(S.INVALID_CHOICE)

    # ---- DATA_KEYS (R27: names come only from the picked libraries)

    def _st_data_keys(self) -> State:
        names = self._data_names()
        if not names:
            self.say(S.DATA_KEYS_NONE)
            return State.REVIEW
        a = self.io.choose(S.DATA_KEYS_PROMPT.format(names=", ".join(names)),
                           [Option("add", "Add keys"), Option("skip", "Skip"), Option("back", "Back")])
        if a == "back":
            return State.SKILLS
        try:
            for c, specs in self._data_consumers():
                for s in specs:
                    self._data_key(c, s, add=a == "add")
        except BackSignal:
            return State.SKILLS
        if a == "add" and not self.d.online:
            self.say(S.DATA_KEYS_OFFLINE)
        return State.REVIEW

    def _data_key(self, c: Consumer, s: NameSpec, *, add: bool) -> None:
        """Add and Skip each re-decide: switching drops what the other choice left behind."""
        if add:
            self.refused.discard(c.key)
            if self.dec(self._dk(c.key, s.name)) == "skip":
                self.forget(self._dk(c.key, s.name))
            self._obtain(c, s)
        else:
            self._mark(c, s.name, "skip")

    # ---- REVIEW

    def _review_ai_line(self) -> str:
        choice = self._plan_ai()
        who = _ai_provider_label(choice.kind, choice.ref)
        ai_name = self._ai_value_name() if choice.kind != "none" else None
        has_key = any(k == "ai" and n == ai_name for k, n in self._grants) \
            or self.dec(self._dk("ai", ai_name or "")) == "keep-existing"
        nokey = S.REVIEW_NO_KEY if ai_name and not has_key else ""
        brand = _CLI_NAMES.get(choice.ref, ("", "your CLI"))[1]
        return S.REVIEW_AI.get(choice.kind, S.REVIEW_AI["none"]).format(brand=brand, ref=choice.ref, who=who, nokey=nokey)

    def _readers(self, name: str) -> list[str]:
        return [c.label for c, specs in self._consumers() if any(s.name == name for s in specs)]

    def _review_text(self) -> str:
        lines = [S.REVIEW_HEAD, S.REVIEW_PROFILE.format(name=self.dec("profile_name")), self._review_ai_line()]
        route = self._plan_route()
        if route is not None:
            lines.append(S.REVIEW_BROKER.format(name=self._broker_name(), dest=route_destination(route)))
        values = self._plan_values()
        if values:
            lines.append(S.REVIEW_KEYS.format(names=", ".join(values)))
            lines += [S.REVIEW_KEYS_FOR.format(name=n, who=" and ".join(self._readers(n))) for n in values]
        lines += [S.REVIEW_LEFT_OUT.format(label=cap(label), name=name) for label, name in self.left_out.values()]
        skills = [lib.id for lib in self._plan_libs()]
        if skills:
            lines.append(S.REVIEW_SKILLS.format(n=len(skills), y="y" if len(skills) == 1 else "ies",
                                                names=", ".join(skills)))
        lines.append(S.REVIEW_NOCHANGE)
        return "\n".join(lines)

    def _st_review(self) -> State:
        try:
            self._ensure_values()
        except BackSignal:
            return State.DATA_KEYS if self._data_names() else State.SKILLS
        a = self.io.choose(self._review_text(), [Option("do", "Do it"), Option("back", "Back")])
        if a == "back":
            return State.DATA_KEYS if self._data_names() else State.SKILLS
        return State.WRITE

    # ---- WRITE

    def _build_plan(self) -> WritePlan:
        self.resolve_sharing()   # again, immediately before the write
        route = self._plan_route()
        broker = BrokerChoice(slug=self.dec("broker.slug"), route_id=self.dec("broker.route"), entry=route) if route else None
        return WritePlan(profile_name=self.dec("profile_name"), ai=self._plan_ai(), broker=broker,
                         skills=[lib.id for lib in self._plan_libs()], env_values=self._plan_values())

    def _st_write(self) -> State:
        plan = self._build_plan()
        env = plan.env_values   # the dict this module built: emptied below so no value outlives WRITE
        try:
            self.receipt = write_profile(plan, hermes=self.d.hermes, source_dir=self.d.source_dir,
                                         store_factory=self.d.store_factory)
        except ProfileExists:
            return self._write_exists(plan.profile_name)
        except HermesUnavailable:
            self.go(State.REVIEW)
            self.say(S.WRITE_FAILED_PLAIN.format(detail="I could not find Hermes on this computer. "
                                                         "Install it (see the PRECHECK step), then try again."))
            return self._write_failed()
        except WriteError as e:
            self.go(State.REVIEW)
            self.say(S.WRITE_FAILED.format(report=self.safe(e.report)))
            return self._write_failed()
        except Exception as e:  # noqa: BLE001
            self.go(State.REVIEW)
            self.say(S.WRITE_FAILED_PLAIN.format(detail=self.safe(e) if isinstance(e, ValueError) else type(e).__name__))
            return self._write_failed()
        finally:
            if isinstance(env, dict):
                env.clear()
        self._values.clear()
        self._grants.clear()
        return State.VERIFY

    def _write_exists(self, name: str) -> State:
        self.go(State.REVIEW)
        self.say(S.WRITE_EXISTS.format(name=name))
        a = self.io.choose(S.WRITE_WHAT_NOW, [Option("rename", "Pick another name"), Option("back", "Back")])
        if a == "rename":
            self.forget("profile_name")
            return State.PRECHECK
        return State.REVIEW

    def _write_failed(self) -> State:
        a = self.io.choose(S.WRITE_WHAT_NOW, [Option("review", "Back to the review"), Option("stop", "Stop here")])
        if a == "stop":
            raise _Quit
        return State.REVIEW

    # ---- VERIFY and DONE

    def _verify_lines(self) -> list[str]:
        lines: list[str] = []
        if self.dec("ai.kind", "none") not in ("none", "cli"):
            if not self.d.online:
                lines.append(f"AI check: {S.NEEDS_INTERNET}.")
            else:
                lines.append(S.AI_CHECK_PASSED if self.dec("ai.verify") == "passed" else S.AI_CHECK_NOT_TESTED)
        if self._plan_route() is not None:
            lines.append(f"Broker connection: {S.NEEDS_INTERNET if not self.d.online else S.NOT_TESTED}"
                         " (testing is not built yet).")
        return lines

    def _st_verify(self) -> State:
        lines = self._verify_lines()
        if lines:
            self.say(S.VERIFY_HEAD + "\n" + "\n".join(lines))
        return State.DONE

    def _left_to_do(self) -> list[str]:
        out: list[str] = []
        kind = self._plan_ai().kind
        if kind in ("none", "cli"):
            out.append(S.LATER_AI)
        elif not self.d.online:
            out.append(S.LATER_AI_TEST.format(status=S.NEEDS_INTERNET))
        elif self.dec("ai.verify") != "passed":
            out.append(S.LATER_AI_TEST.format(status=S.NOT_TESTED))
        route = self._plan_route()
        if route is None:
            out.append(S.LATER_BROKER)
        else:
            if not route_names(route):
                out.append(S.LATER_OAUTH.format(name=self._broker_name()))
            out.append(S.LATER_BROKER_TEST.format(
                name=self._broker_name(), status=S.NEEDS_INTERNET if not self.d.online else S.NOT_TESTED))
        written = set(self.receipt.env_names_written) if self.receipt else set()
        out += [S.LATER_NAME.format(name=s.name, who=c.who) for c, specs in self._consumers() for s in specs
                if self.dec(self._dk(c.key, s.name)) == "skip" and s.name not in written]
        out += [S.LATER_LEFT_OUT.format(label=cap(label), name=name) for label, name in self.left_out.values()]
        return out

    def _st_done(self) -> None:
        name = self.dec("profile_name")
        choice = self._plan_ai()
        text = S.DONE_TITLE + "\n" + S.DONE_BODY.format(
            ai=_ai_provider_label(choice.kind, choice.ref),
            broker=(self._broker_name() or "none") if self._plan_route() else "none", n=len(self._plan_libs()))
        later = self._left_to_do()
        if later:
            text += "\n" + S.FINISH_LATER_TITLE + "\n" + "\n".join(f"- {x}" for x in later)
        text += "\n" + S.DONE_UNDO.format(name=name, receipt=Receipt.path(self.p, name))
        self.say(text)
        return None


def run_wizard(io: IO, deps: Deps) -> Receipt | None:
    """Run (or resume) the wizard. Returns the Receipt that ``write_profile`` returned, or None if the
    user stopped, or the write did not complete."""
    return _Wizard(io, deps).run()
