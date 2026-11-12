"""Write the new Hermes profile, with a receipt after every step and an exact rollback.

Sequence (FIRST_RUN.md 5.2, T0 facts): refuse an existing profile; ``hermes profile install`` and
verify the directory exists; ``hermes -p NAME config set`` for ``auth.adopt_external_logins`` and the
provider keys; one ``mcp_servers`` entry in ``config.yaml`` (references only, never values); secret
values into the profile ``.env`` through the SecretStore; skills recorded by name only.

Everything created lives under ``<HERMES_HOME>/profiles/<name>/`` plus the receipt file.
Reports and ``WriteError.report`` pass through ``scrub`` and ``redact_patterns`` before leaving here.
"""
from __future__ import annotations

import copy
import os
import re
import secrets as _stdlib_secrets
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

import yaml

from trading_agent.profile.hermes_cli import (
    ForbiddenHermesArgs, HermesCli, validate_profile_name,
)
from trading_agent.profile.receipt import Receipt
from trading_agent.platform import RunResult
from trading_agent.secrets import SecretStore, check_secret, redact_patterns, scrub

__all__ = ["AiChoice", "BrokerChoice", "WritePlan", "ProfileExists", "WriteError", "ForbiddenHermesArgs",
           "build_mcp_entry", "write_profile", "rollback"]

_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SERVER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_BIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_SKILL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}")
_SHELL_TOKENS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"})
_PROFILE_PATH_RE = re.compile(r"^\s*Profile path:\s*(.+?)\s*$", re.MULTILINE)
_REPORT_LIMIT = 600

# A named ``providers:`` entry is the shape Hermes' runtime resolver reads for a custom endpoint
# (runtime_provider_custom._select_named_custom_provider; ``model.key_env`` is read only for registry
# providers). VERIFIED on Hermes v0.23.1 with a bogus key and no model call: ``resolve_runtime_provider``
# returned the entry's base_url and the key named by ``key_env`` (placeholder when the variable is unset).
CUSTOM_PROVIDER_NAME = "custom_endpoint"
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"


@dataclass(frozen=True)
class AiChoice:
    kind: Literal["cli", "none", "openrouter", "custom", "ollama", "lm-studio"]
    ref: str = ""        # CLI binary name for "cli", base_url for "custom", else ""
    key_env: str = ""    # "custom" only: NAME of the env var holding its key (optional)


@dataclass(frozen=True)
class BrokerChoice:
    slug: str
    route_id: str
    entry: dict          # the chosen catalog route (BROKER_SCHEMA.md 3.2)


@dataclass(frozen=True)
class WritePlan:
    profile_name: str
    ai: AiChoice | None
    broker: BrokerChoice | None
    skills: list[str]
    env_values: Mapping[str, str]

    def __repr__(self) -> str:  # never show values
        return (f"WritePlan(profile_name={self.profile_name!r}, ai={self.ai!r}, "
                f"broker={self.broker.slug if self.broker else None!r}, skills={self.skills!r}, "
                f"env_names={sorted(self.env_values)!r})")


class ProfileExists(Exception):
    """The profile name is taken. Nothing was written and nothing will be touched."""


class WriteError(Exception):
    """A write step failed and this run's changes were rolled back."""

    def __init__(self, report: str, receipt: Receipt) -> None:
        super().__init__(report)
        self.report = report
        self.receipt = receipt


class _StepError(Exception):
    pass


# ------------------------------------------------------------------ validation

def _check_env_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("invalid environment variable name (expected [A-Z_][A-Z0-9_]*)")


def _check_http_url(url: Any, *, what: str) -> str:
    if not isinstance(url, str) or not url or re.search(r"\s", url):
        raise ValueError(f"{what}: invalid URL")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise ValueError(f"{what}: invalid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{what}: URL must be http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{what}: URL must not carry credentials")
    if parts.query or parts.fragment:
        raise ValueError(f"{what}: URL must not carry a query or fragment")
    return url


def build_mcp_entry(route: Mapping[str, Any], env_names: set[str] | frozenset[str]) -> dict:
    """The ``mcp_servers`` entry for ROUTE: names and ``${NAME}`` references only.

    local-command: ``command`` / ``args`` from the page's command text, and an ``env:`` map that names
    every secret env var (and every non-secret one the user supplied a value for) as ``NAME: "${NAME}"``,
    because stdio children inherit ONLY listed variables (T0 answer 4).
    remote-url: ``url`` plus ``headers`` whose values are ``${NAME}`` for EVERY listed header. A header's
    env var is ``auth.header_env[header]`` or the header name itself. A header is never omitted: an
    unset ``${NAME}`` in a remote header makes Hermes fail closed at connect, naming the variable.
    """
    kind = route.get("kind")
    auth = route.get("auth") or {}
    if kind == "local-command":
        text = (route.get("install") or {}).get("command_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("local route has no command text")
        try:
            tokens = shlex.split(text)
        except ValueError:
            raise ValueError("local route command text is not parseable") from None
        if not tokens or any(t in _SHELL_TOKENS or "$(" in t or "`" in t for t in tokens):
            raise ValueError("local route command text is not a plain command")
        entry: dict[str, Any] = {"command": tokens[0], "args": tokens[1:]}
        env: dict[str, str] = {}
        for var in auth.get("env_vars") or []:
            name = var.get("name")
            _check_env_name(name)
            if var.get("secret") or name in env_names:
                env[name] = "${" + name + "}"
        if env:
            entry["env"] = env
        return entry
    if kind == "remote-url":
        entry = {"url": _check_http_url(route.get("url"), what="remote route")}
        header_env = auth.get("header_env") or {}
        headers: dict[str, str] = {}
        for header in auth.get("headers") or []:
            if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*|[A-Z][A-Z0-9_]*", header):
                raise ValueError("invalid header name in route")
            var = header_env.get(header, header)
            _check_env_name(var)
            headers[header] = "${" + var + "}"  # always a reference; an unset one fails closed in Hermes
        if headers:
            entry["headers"] = headers
        return entry
    raise ValueError("this route kind cannot be written as an MCP server entry")


def _provider_settings(ai: AiChoice | None) -> list[tuple[str, str]]:
    """Ordered ``config set`` pairs for the provider. ``provider`` first: switching provider clears a
    stale ``model.base_url`` in Hermes, so base_url must come after it."""
    if ai is None:
        return []
    kind = ai.kind
    if kind == "none":
        return []
    if kind == "cli":
        return []  # recorded in the receipt only; nothing defines how a logged-in CLI powers Hermes yet
    if kind == "openrouter":
        return [("model.provider", "openrouter")]
    if kind == "custom":
        base = f"providers.{CUSTOM_PROVIDER_NAME}"
        out = [("model.provider", CUSTOM_PROVIDER_NAME), (f"{base}.name", CUSTOM_PROVIDER_NAME),
               (f"{base}.base_url", ai.ref)]
        if ai.key_env:
            out.append((f"{base}.key_env", ai.key_env))
        return out
    # Local servers need no key: bare ``custom`` + a loopback ``model.base_url`` resolves with the
    # no-key placeholder (VERIFIED the same way on v0.23.1 for both ports).
    if kind == "ollama":
        return [("model.provider", "custom"), ("model.base_url", OLLAMA_BASE_URL)]
    if kind == "lm-studio":
        return [("model.provider", "custom"), ("model.base_url", LM_STUDIO_BASE_URL)]
    raise ValueError("unknown AI choice kind")


def _validate_ai(ai: AiChoice | None) -> None:
    if ai is None:
        return
    if ai.kind not in ("cli", "none", "openrouter", "custom", "ollama", "lm-studio"):
        raise ValueError("unknown AI choice kind")
    if ai.kind == "cli":
        if not isinstance(ai.ref, str) or not _BIN_RE.fullmatch(ai.ref):
            raise ValueError("cli choice needs a plain binary name")
    elif ai.kind == "custom":
        _check_http_url(ai.ref, what="custom provider base_url")
        if ai.key_env:
            _check_env_name(ai.key_env)
    elif ai.ref:
        raise ValueError(f"{ai.kind} choice takes no ref")
    if ai.kind != "custom" and ai.key_env:
        raise ValueError(f"{ai.kind} choice takes no key_env")


def _validate_plan(plan: WritePlan) -> dict | None:
    """Everything that can be refused up front, before a single hermes process runs."""
    validate_profile_name(plan.profile_name)
    _validate_ai(plan.ai)
    entry = None
    if plan.broker is not None:
        if not _SERVER_RE.fullmatch(plan.broker.slug or ""):
            raise ValueError("invalid broker slug")
        entry = build_mcp_entry(plan.broker.entry, frozenset(plan.env_values))
    for name, value in plan.env_values.items():
        check_secret(name, value)
    for s in plan.skills:
        if not isinstance(s, str) or not _SKILL_RE.fullmatch(s):
            raise ValueError("invalid skill name")
    return entry


# ------------------------------------------------------------------ reports

def _report(text: str, plan_values: list[str]) -> str:
    # Platform.run decodes with universal newlines, so a value containing CR comes back with LF:
    # scrub the newline-translated form of every value as well as the original.
    forms = set(plan_values)
    for v in plan_values:
        forms.add(v.replace("\r\n", "\n").replace("\r", "\n"))
    return redact_patterns(scrub(text, forms))


def _tail(text: str, plan_values: list[str]) -> str:
    """Scrub and redact the RAW text first, only then normalize whitespace and truncate, then run
    the result through scrub/redact once more. Normalizing or cutting first would change a secret's
    form (so scrub misses it) or leave a prefix of it."""
    text = _report(text, plan_values)
    text = " ".join(text.split())
    text = text[:_REPORT_LIMIT] + ("..." if len(text) > _REPORT_LIMIT else "")
    return _report(text, plan_values)


def _hermes_failure(what: str, res: RunResult, plan_values: list[str]) -> _StepError:
    if res.timed_out:
        return _StepError(f"{what}: hermes timed out")
    # each stream separately and raw: a secret never spans the stderr/stdout boundary
    detail = " ".join(part for part in (_report(res.stderr or "", plan_values),
                                        _report(res.stdout or "", plan_values)) if part)
    detail = _tail(detail, plan_values)
    return _StepError(f"{what}: hermes exited {res.returncode}" + (f": {detail}" if detail.strip() else ""))


# ------------------------------------------------------------------ file helpers

def _inside(path: Path, base: Path) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(base)]) == os.path.realpath(base)
    except ValueError:
        return False


def _atomic_write_text(path: Path, text: str, mode: int) -> None:
    tmp = path.parent / f".{path.name}.{_stdlib_secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


MARKER_NAME = ".trading-agent-run"


def _write_marker(pdir: Path, run_id: str) -> None:
    """Ownership proof: a random run id inside the profile dir, 0600. Rollback requires it."""
    marker = pdir / MARKER_NAME
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(run_id + "\n")


def _marker_matches(pdir: Path, run_id: str) -> bool:
    marker = pdir / MARKER_NAME
    try:
        if not run_id or marker.is_symlink() or not marker.is_file():
            return False
        return marker.read_text(encoding="utf-8").strip() == run_id
    except OSError:
        return False


def _add_mcp_server(pdir: Path, name: str, entry: dict) -> None:
    """Merge one ``mcp_servers`` entry into the profile config.yaml with a YAML library
    (a text append mis-nests under a trailing ``auth:`` block, T0 gotcha)."""
    cfg_path = pdir / "config.yaml"
    if cfg_path.is_symlink() or not _inside(cfg_path, pdir):
        raise _StepError("config.yaml is not a regular file inside the profile")
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else None
    except yaml.YAMLError:
        raise _StepError("config.yaml is not valid YAML") from None
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise _StepError("config.yaml top level is not a mapping")
    servers = cfg.get("mcp_servers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise _StepError("config.yaml mcp_servers is not a mapping")
    if name in servers:
        raise _StepError(f"mcp server {name} already present in config.yaml")
    servers[name] = entry
    cfg["mcp_servers"] = servers
    mode = stat.S_IMODE(cfg_path.stat().st_mode) if cfg_path.exists() else 0o600
    _atomic_write_text(cfg_path, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True,
                                                default_flow_style=False), mode)


# ------------------------------------------------------------------ write

def write_profile(plan: WritePlan, *, hermes: HermesCli, source_dir: Path,
                  store_factory: Callable[[Path], SecretStore]) -> Receipt:
    mcp_entry = _validate_plan(plan)  # ValueError, value-free, before anything runs
    name = plan.profile_name
    platform = hermes.platform
    if hermes.profile_exists(name):
        raise ProfileExists(f"profile '{name}' already exists; nothing was changed")

    pdir = hermes.profile_dir(name)
    receipt = Receipt(
        profile_name=name,
        ai_choice={"kind": plan.ai.kind, "ref": plan.ai.ref} if plan.ai else None,
        broker={"slug": plan.broker.slug, "route_id": plan.broker.route_id} if plan.broker else None,
        skills=list(plan.skills),
        run_id=_stdlib_secrets.token_hex(16),
    )
    values = [v for v in plan.env_values.values()]
    failure: str | None = None
    interrupt: BaseException | None = None
    try:
        _run_steps(plan, mcp_entry, receipt, hermes, pdir, Path(source_dir), store_factory)
    except _StepError as e:
        failure = str(e)  # built from already scrubbed text
    except ForbiddenHermesArgs:
        failure = "internal error: a forbidden hermes argument was requested"
    except Exception as e:  # noqa: BLE001 - every failure after install must roll back
        failure = f"{type(e).__name__}: {_tail(str(e), values)}"
    except BaseException as e:  # KeyboardInterrupt etc: roll back, then let it propagate
        interrupt = e
    if failure is None and interrupt is None:
        return receipt

    ran = copy.deepcopy(receipt)  # what ran, for the caller; the live receipt is cleared by rollback
    acted, problems = _rollback_collect(receipt, hermes, store_factory, pdir)
    if interrupt is not None:
        raise interrupt
    step = ran.steps_done[-1] if ran.steps_done else "start"
    text = f"setup failed after step '{step}': {failure}"
    if "install" in ran.steps_done:
        text += "; rolled back" if acted and not problems else (
            f"; rollback incomplete ({'; '.join(problems)})" if problems else "; nothing rolled back (ownership not proven)")
    raise WriteError(_report(text, values), ran) from None


def _run_steps(plan: WritePlan, mcp_entry: dict | None, receipt: Receipt, hermes: HermesCli, pdir: Path,
               source_dir: Path, store_factory: Callable[[Path], SecretStore]) -> None:
    name = plan.profile_name
    platform = hermes.platform

    # 2. install, then VERIFY (exit 0 alone proves nothing: "Install cancelled." exits 0)
    res = hermes.profile_install(source_dir, name)
    out = res.stdout or ""
    values = list(plan.env_values.values())
    if res.returncode != 0 or res.timed_out:
        if "already exists" not in out and pdir.is_dir() and not pdir.is_symlink():
            # a half-made dir we verified absent a moment ago: claim it so rollback can remove it
            _write_marker(pdir, receipt.run_id)
            receipt.created_paths.append(str(pdir))
            receipt.steps_done.append("install")
        raise _hermes_failure("install", res, values)
    m = _PROFILE_PATH_RE.search(out)
    if m and os.path.realpath(m.group(1)) != os.path.realpath(pdir):
        raise _StepError("install: hermes reported a different profile path than expected; left untouched")
    if not pdir.is_dir() or pdir.is_symlink():
        raise _StepError("install: hermes exited 0 but the profile directory does not exist")
    _write_marker(pdir, receipt.run_id)
    receipt.created_paths.append(str(pdir))
    receipt.steps_done.append("install")
    receipt.save(platform)

    # 3a. config via hermes: adopt switch, then provider keys
    settings = [("auth.adopt_external_logins", "false"), *_provider_settings(plan.ai)]
    for key, value in settings:
        res = hermes.config_set(name, key, value)
        if res.returncode != 0 or res.timed_out:
            raise _hermes_failure(f"config set {key}", res, values)
    receipt.created_paths.append(str(pdir / "config.yaml"))
    receipt.steps_done.append("config")
    receipt.save(platform)

    # 3b. broker entry: references only
    if plan.broker is not None and mcp_entry is not None:
        _add_mcp_server(pdir, plan.broker.slug, mcp_entry)
    receipt.steps_done.append("broker")
    receipt.save(platform)

    # 4. secret values: only through the store, only NAMES go in the receipt
    if plan.env_values:
        store = store_factory(pdir)
        for env_name, value in plan.env_values.items():
            store.set(env_name, value)
            receipt.env_names_written.append(env_name)
            if str(pdir / ".env") not in receipt.created_paths:
                receipt.created_paths.append(str(pdir / ".env"))
            receipt.save(platform)
    receipt.steps_done.append("secrets")
    receipt.save(platform)

    # 5. skills: recorded by name only; installation is a later task
    receipt.steps_done.append("skills")
    receipt.save(platform)


# ------------------------------------------------------------------ rollback

def _rollback_collect(receipt: Receipt, hermes: HermesCli, store_factory: Callable[[Path], SecretStore],
                      pdir: Path) -> tuple[bool, list[str]]:
    """Undo what RECEIPT says this run created. Returns (acted, short value-free problem notes).

    Ownership is proven by the marker file in the profile dir matching the receipt's run id; without
    it (a recreated profile, a tampered receipt, a pre-existing profile) nothing is touched."""
    problems: list[str] = []
    platform = hermes.platform
    if "install" not in receipt.steps_done:
        return False, problems  # we never created anything: touch nothing

    root = Path(os.path.realpath(pdir.parent))
    owned = (pdir.is_dir() and not pdir.is_symlink() and root.name == "profiles"
             and Path(os.path.realpath(pdir)) == root / pdir.name and _marker_matches(pdir, receipt.run_id))
    if not owned:
        return False, problems

    if receipt.env_names_written:
        try:
            store = store_factory(pdir)
            for env_name in receipt.env_names_written:
                try:
                    store.remove(env_name)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"could not remove {env_name} from .env ({type(e).__name__})")
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not open the secret store ({type(e).__name__})")

    delete_note = None
    try:
        res = hermes.profile_delete(receipt.profile_name)
        if res.returncode != 0 or res.timed_out:
            delete_note = "hermes profile delete failed"
    except Exception as e:  # noqa: BLE001
        delete_note = f"hermes profile delete raised {type(e).__name__}"
    if pdir.exists():
        # Fallback, only for the exact directory this run proved it owns, never a symlink.
        try:
            if not pdir.is_symlink() and Path(os.path.realpath(pdir)) == root / pdir.name:
                shutil.rmtree(pdir)
        except OSError as e:
            problems.append(f"could not remove profile directory ({type(e).__name__})")
    if pdir.exists() and delete_note:
        problems.append(delete_note)

    if not problems:
        receipt.remove_file(platform)  # only if the saved receipt carries this run id
        receipt.steps_done.clear()
        receipt.created_paths.clear()
        receipt.env_names_written.clear()
    return True, problems


def rollback(receipt: Receipt, *, hermes: HermesCli, store_factory: Callable[[Path], SecretStore]) -> bool:
    """Remove what this receipt created. Returns True if it acted, False if it did nothing because
    ownership of the profile could not be proven. Safe to call twice. Never touches another profile,
    the default profile, a symlink target, or anything outside the profile directory."""
    validate_profile_name(receipt.profile_name)
    pdir = hermes.profile_dir(receipt.profile_name)
    acted, problems = _rollback_collect(receipt, hermes, store_factory, pdir)
    if problems:
        raise WriteError(_report("rollback incomplete: " + "; ".join(problems), []), receipt)
    return acted
