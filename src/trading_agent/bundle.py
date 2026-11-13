"""Export a profile to one bundle file (or a readable folder) and import a bundle as a new profile.

A bundle is a ``.tgz`` whose members are exactly ``bundle.json`` (names and labels, never a value) and
``profile.tar.gz`` (a filtered copy of Hermes' own ``profile export``). It never holds a key, ``.env``,
``auth.json``, a key or token file, chat history, logs or the ``.trading-agent-run`` ownership marker.
It is NOT encrypted.

Export (security first): Hermes drops ``.env`` and ``auth.json``. This module then keeps only ``SOUL.md``,
``config.yaml``, ``memories/`` and user skills (stock skills Hermes seeded and still match its
``skills/.bundled_manifest`` hash are left out, with the manifest and ``skills/.hub/``, because Hermes reseeds
them), and applies one explicit rule:

* HARD refusal, nothing written: a file named like a credential anywhere in the profile (``.env*``,
  ``auth.json``, ``*.pem``, ``*.key``, ``token*``); a kept file holding a value from the profile's own ``.env``
  (values of 8 or more characters, read in memory for this comparison only, never printed or stored); a kept
  file holding a high-confidence token shape (``sk-``, ``Bearer``, ``ghp_`` and other vendor prefixes, PEM
  private keys, JWTs, ``user:password@`` in a URL, a long value in a key-named URL parameter); a literal value
  in ``config.yaml`` under a key-like name or anywhere in an ``env:`` or ``headers:`` map.
* NAMED WARNING with a confirm, default No: a plain long hex or base64 run, or a token-like URL value, in a
  user file (usually a hash or an example). Without a way to ask, this is a refusal too.

Import (hostile input): every member of both archives is checked by name and type BEFORE anything is
written, under fixed caps; ``bundle.json`` is validated field by field and never trusted for a path; the
``config.yaml`` must hold no literal secret and may not turn off command approvals, and what it can run
(MCP servers, hooks, quick commands, approval modes, Hermes settings) is shown before a default-No confirm. The
profile is created through ``hermes profile import`` with a receipt and the ownership marker made here, so
``trading-agent undo`` removes it and a failure or Ctrl-C midway rolls back. Keys are asked for by name, never
reused from the environment.
"""
from __future__ import annotations

import copy
import dataclasses
import gzip
import hashlib
import io
import io as _iomod
import json
import os
import re
import secrets as _rand
import shutil
import stat
import tarfile
import tempfile
import time
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trading_agent.profile import writer as W
from trading_agent.profile.hermes_cli import ForbiddenHermesArgs, HermesCli, validate_profile_name
from trading_agent.profile.receipt import Receipt
from trading_agent.secrets import (
    _BEARER, _SENSITIVE_NAME, _SK_KEY, _TOKENISH, _URL_PARAM, SecretStore, check_secret, redact_patterns, scrub,
)
from trading_agent.wizard import screens as S
from trading_agent.wizard.io import BACK_INPUT, IO, Option

FORMAT = 1
BUNDLE_JSON = "bundle.json"
PROFILE_ARCHIVE = "profile.tar.gz"

# Caps (stated in docs/MOVING.md). Module level so tests can shrink them.
MAX_BUNDLE_BYTES = 50 * 2**20          # the bundle file as stored (compressed)
MAX_MANIFEST_BYTES = 64 * 2**10        # bundle.json
MAX_PROFILE_COMPRESSED = 50 * 2**20    # profile.tar.gz inside the bundle
MAX_PROFILE_EXPANDED = 200 * 2**20     # the profile archive once inflated
MAX_MEMBER_BYTES = 50 * 2**20          # one file in the profile archive
MAX_MEMBERS = 5000                     # entries in the profile archive
MAX_RATIO = 100                        # inflated / compressed, checked once past RATIO_FLOOR
RATIO_FLOOR = 2**20
CHUNK = 2**20
MAX_PATH = 512
MIN_VALUE_LEN = 8                      # shorter .env values (true, paper, 1) are settings, not secrets
MAX_ENV_NAMES = 32
MAX_CONFIG_BYTES = 2**20               # config.yaml parsed for the literal-secret and what-it-runs checks
MAX_CONFIG_NODES = 100_000             # YAML alias bombs expand to more than this and are refused
MAX_CONFIG_DEPTH = 64
MAX_SHOWN = 20                         # servers, hooks or quick commands listed before "and N more"
CLIP = 120

KEPT_FILES = ("SOUL.md", "config.yaml")
KEPT_DIRS = ("memories", "skills")
CONTENT_LABELS = ("memories", "skills", "config", "SOUL.md")
EXCLUDES = ("keys", "oauth tokens")
AI_KINDS = frozenset({"cli", "none", "openrouter", "custom", "ollama", "lm-studio"})

_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SKILL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}")
_COMMIT_RE = re.compile(r"[0-9A-Za-z._-]{1,64}")
_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}")
_KEY_ENV_RE = re.compile(r"(?m)^\s*key_env:\s*['\"]?([A-Z_][A-Z0-9_]*)")
# Names a bundle may never ask the user to fill: they change how programs load or where they connect.
_DENY_NAMES = frozenset({"PATH", "HOME", "SHELL", "USER", "LOGNAME", "PWD", "TMPDIR", "IFS", "ENV", "BASH_ENV",
                         "NODE_OPTIONS", "NODE_PATH", "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB", "SSL_CERT_FILE",
                         "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY",
                         "ALL_PROXY", "NO_PROXY", "LANG", "EDITOR", "VISUAL", "PAGER", "BROWSER", "PROMPT_COMMAND",
                         "SSH_ASKPASS", "SUDO_ASKPASS", "GCONV_PATH", "OPENSSL_CONF", "JAVA_TOOL_OPTIONS",
                         "_JAVA_OPTIONS", "CLASSPATH"})
_DENY_PREFIXES = ("LD_", "DYLD_", "PYTHON", "XDG_", "GIT_", "UV_", "PIP_", "NPM_CONFIG", "LC_", "HERMES_", "MALLOC_")

# High-confidence token shapes: a hit is a HARD refusal (redact_patterns' sk- and Bearer rules, plus these).
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_VENDOR = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}"
                     r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|glpat-[A-Za-z0-9_-]{20,})")
_USERINFO_PW = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{0,31}://[^/\s@:]+:[^/\s@]+@")
_HARD_SHAPES = (_SK_KEY, _BEARER, _PEM, _JWT, _VENDOR, _USERINFO_PW)
# What Hermes 0.23's export leaves of a real token (agent.redact.redact_sensitive_text(force=True), observed):
# a known prefix, up to 4 more characters, "...", the last 4 (ghp_xa...NGp7, sk-or-...89ab, AKIAAB...MNOP,
# eyJhbG...sR8U), "Bearer abcdef...xyzQ", or "[REDACTED PRIVATE KEY]". Short tokens become "***" (not detectable
# here: only the on-disk scan sees those).
_MASKED = re.compile(r"(?<![A-Za-z0-9_\-])(?:sk-|gh[pousr]_|github_|xox[abposr]-|AKIA|AIza|glpat-|eyJ)"
                     r"[A-Za-z0-9_\-]{0,4}\.\.\.[A-Za-z0-9_\-]{4}(?![A-Za-z0-9_\-])"
                     r"|\[REDACTED[^\]\n]{0,40}\]"
                     r"|(?i:\bbearer\s+[A-Za-z0-9._~+/=\-]{6}\.\.\.[A-Za-z0-9._~+/=\-]{4})")

# config.yaml literal-value rule. Key names are split into words (snake, kebab and camel case).
_SECRET_WORDS = frozenset({"key", "keys", "apikey", "secret", "secrets", "token", "password", "passwd", "pwd",
                           "signature", "bearer",
                           "passphrase", "auth", "authorization", "credential", "credentials", "cookie", "cookies"})
_NOT_A_VALUE_LAST = frozenset({"env", "url", "uri", "endpoint", "file", "path", "dir", "type", "mode", "method",
                               "provider", "name", "header", "scheme", "kind", "style", "required", "enabled"})
_COUNT_WORDS = frozenset({"max", "min", "limit", "limits", "count", "budget", "length", "size", "ttl", "timeout",
                          "threshold"})
# Hermes 0.23 default settings whose names hold a secret word but whose values are not secrets. Derived by walking
# hermes_cli/config_defaults.DEFAULT_CONFIG for secret-word keys (tests/fixtures/hermes_default_config_sample.yaml
# holds those defaults). A path here, or under it, is never a literal secret.
SAFE_CONFIG_PATHS = frozenset({
    "terminal.docker_shared_container_key", "compression.threshold_tokens", "compression.proactive_prune_tokens",
    "compression.proactive_prune_min_reclaim_tokens", "compression.micro_compact_defrag_threshold_tokens",
    "display.spinner_token_flow", "dashboard.show_token_analytics", "dashboard.basic_auth.username",
    "dashboard.basic_auth.session_ttl_seconds", "dashboard.session_token_auth.enabled", "dashboard.drain_auth.scope",
    "dashboard.drain_auth.min_secret_chars", "voice.record_key", "delegation.compression_threshold_tokens",
    "discord.dm_role_auth_guild", "auth.codex_login_flow", "tools.tool_search.listing_max_tokens", "vault.key_storage",
    "secrets.bitwarden.project_id", "secrets.bitwarden.cache_ttl_seconds", "secrets.bitwarden.encrypted_cache",
    "secrets.bitwarden.override_existing", "secrets.bitwarden.auto_install", "secrets.onepassword.account",
    "secrets.onepassword.cache_ttl_seconds", "secrets.onepassword.override_existing", "proxy.credential_source",
    "desktop.password_store", "credential_pool_strategies",
})
_STRICT_REF = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")
_ANY_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)")
_IMPORTED_AT = re.compile(r"(?m)Imported profile '[^'\n]*' at (.+?)\s*$")
_RUNTIME_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})

# ---------------------------------------------------------------- copy (no em or en dashes)

CONTAINS = "This contains: {items}."
CONTAINS_NOTHING = "This contains: an empty profile."
NOT_CONTAINED = "It does not contain: keys, sign-ins."
LEFT_HERE = "Chat history, logs and workspace files stay on this computer. The bundle is not encrypted."
IMPORT_TRUST = ("A bundle's config.yaml can start programs (MCP servers) when the agent runs. "
                "Import only a bundle you made or trust.")
IMPORT_CONFIRM = "Import it as a new profile called {name}?"
RISKY_HEAD = "The bundle's config.yaml sets up these:"
RUN_WARNING = ("These can run programs on this computer. Import only a bundle you made or trust; "
               "the answer defaults to No.")
REFUSED_SETTING = ("The bundle's config.yaml sets {path}, which can run programs on this computer or turn off "
                   "command approvals. An imported bundle may not do that. Nothing was imported.")
NAME_TAKEN_NOW = "The name {name} was taken while you were answering. Nothing was imported."
SOFT_HEAD = ("These files hold text shaped like a key. It is probably a hash or an example, but check before "
             "you share the bundle:")
SOFT_CONFIRM = "Export them as they are?"
SOFT_NO_ASK = ("I did not export anything: {rels} hold text shaped like a key. Run the export in a terminal to "
               "review them, or edit them first.")
EXPORT_NO = "Nothing was written. Edit those files and export again."
IMPORT_NO = "Nothing was imported."
IMPORT_NAME_PROMPT = ("Name for the imported profile (letters, digits, hyphen, underscore). "
                      "Type < to go back.")
COMMAND_TAKEN = ("A command called {name} already exists on this computer, and Hermes would replace it. "
                 "I will not do that.")
KEY_WHO = "The imported profile"
IMPORT_DONE = "Imported the profile {name}."
FINISH_LATER_TITLE = S.FINISH_LATER_TITLE
LATER_KEY = "- Add {name} to the profile's private .env file (you skipped it)."
IMPORT_UNDO = "To remove it, run: trading-agent undo --profile {name}"

NOT_READABLE = "I could not read that bundle file. Nothing was imported."
NOT_A_BUNDLE = "That file is not a bundle this tool made, or it is damaged. Nothing was written."
TOO_LARGE = "The archive is too large or too compressed to be safe. Nothing was written."
TOO_MANY = "The archive has too many files to be safe (more than {n}). Nothing was written."
UNSAFE_MEMBER = ("The archive holds an unsafe entry (a link, a device, a duplicate name or a path outside "
                 "the profile). Nothing was written.")
BAD_LAYOUT = "The profile archive in the bundle is not laid out the way Hermes makes it. Nothing was imported."
BAD_FORMAT = "This bundle uses a format this version does not know. Nothing was imported."
BAD_MANIFEST = "The bundle's description file (bundle.json) is not valid. Nothing was imported."
NOT_ALLOWED = ("The bundle holds files a profile bundle never carries (keys, chat history, scheduled jobs "
               "or other extras). Nothing was imported.")
SECRET_FILE = ("I did not export anything: {rel} looks like a key or token. Move it out of the profile "
               "and try again.")
SECRET_CONTENT = "I did not export anything: {rel} {why}. Edit that file to remove it, then export again."
WHY_ENV_VALUE = "holds a value from this profile's .env file"
WHY_TOKEN = "holds something shaped like a key or token"
WHY_SOFT = "has a long hex or base64 run, or a token-like web address value"
WHY_MASKED = ("holds something Hermes changes on export because it looks like a key, so a real key would be "
              "exported in part (a placeholder shaped like a key also trips this)")
DISK_LINK = "I did not export anything: {rel} became a link while I was checking it. Try again."
DISK_GONE = "I did not export anything: {rel} disappeared while I was checking it. Try again."
DISK_SPECIAL = "I did not export anything: {rel} is not a regular file any more. Try again."
DISK_UNREADABLE = "I did not export anything: I could not read {rel} on disk. Check its permissions and try again."
DISK_TOO_BIG = "I did not export anything: {rel} is too large to check (over 50 MB)."
DISK_CHANGED = "I did not export anything: {rel} changed while I was checking it. Try again."
WHY_ASSIGNMENT = "holds what looks like a password, token or key written after its name"
NOT_IMPORTED_HEAD = "These settings were not imported:"
NOT_IMPORTED_TAIL = ("Your bundle's config.yaml had them; add them yourself with `hermes config set` if you "
                     "trust them.")
CONFIG_NOT_MAPPING = "The bundle's config.yaml is not a list of settings. Nothing was imported."
WHY_SOFT_NAME = "has a name shaped like a key"
CONFIG_LITERAL = ("I did not export anything: config.yaml holds a value written out in full at {paths}. Move each "
                  "value to the profile's .env file and reference it as ${{NAME}}.")
CONFIG_LITERAL_IMPORT = ("The bundle's config.yaml holds values written out in full at {paths}, where only "
                         "${{NAME}} references belong. Nothing was imported.")
CONFIG_UNREADABLE = "config.yaml could not be read as YAML, so I could not check it. Nothing was written."
CONFIG_COMPLEX = "config.yaml is too large or too deeply nested to check. Nothing was written."
CONFIG_ODD_KEYS = ("config.yaml repeats a setting name, or has a setting name that is not text, so I could not "
                   "check it the way Hermes reads it. Nothing was written.")
NOT_A_FOLDER = "That path is a file or a link, not a folder. Nothing was written."
NO_WRITE = "I cannot write there (permission denied). Nothing was written."
NO_PROFILE = "I found no profile called {name}."
EXPORT_FAILED = "Hermes could not export the profile ({detail}). Nothing was written."
OUT_EXISTS = "That file already exists. I never overwrite; pick a new name."
DIR_NOT_EMPTY = "That folder is not empty. I never overwrite; pick a new or empty folder."
NO_PARENT = "The folder to write into does not exist."


class BundleError(Exception):
    """One plain, value-free line. Nothing was written (export) or nothing was created (import)."""


class ExportCancelled(BundleError):
    """The user said No to exporting files with a named warning. Nothing was written."""


# ---------------------------------------------------------------- manifest

@dataclass(frozen=True)
class BundleManifest:
    format: int
    created_at: str
    profile_name: str
    hermes_version: str
    catalog_commit: str | None
    broker: dict | None
    libraries: tuple[str, ...]
    ai_kind: str | None
    env_names_needed: tuple[str, ...]
    contents: tuple[str, ...]
    excludes: tuple[str, ...]

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        for k in ("libraries", "env_names_needed", "contents", "excludes"):
            d[k] = list(d[k])
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "BundleManifest":
        """Untrusted input: every field checked for type, length and character set. Raises BundleError."""
        if not isinstance(d, dict):
            raise BundleError(BAD_MANIFEST)
        fmt = d.get("format")
        if type(fmt) is not int or fmt != FORMAT:
            raise BundleError(BAD_FORMAT)
        fields = {f.name for f in dataclasses.fields(cls)}
        if set(d) != fields:
            raise BundleError(BAD_MANIFEST)
        try:
            return cls(
                format=FORMAT,
                created_at=_text(d["created_at"], 64),
                profile_name=_profile_name(d["profile_name"]),
                hermes_version=_text(d["hermes_version"], 200),
                catalog_commit=None if d["catalog_commit"] is None else _match(d["catalog_commit"], _COMMIT_RE),
                broker=_broker(d["broker"]),
                libraries=_list(d["libraries"], 100, lambda v: _match(v, _SKILL_RE)),
                ai_kind=None if d["ai_kind"] is None else _choice(d["ai_kind"], AI_KINDS),
                env_names_needed=_env_names(d["env_names_needed"]),
                contents=_list(d["contents"], 10, lambda v: _choice(v, CONTENT_LABELS)),
                excludes=_list(d["excludes"], 10, lambda v: _choice(v, EXCLUDES)),
            )
        except (ValueError, TypeError):
            raise BundleError(BAD_MANIFEST) from None


def _text(v: Any, cap: int) -> str:
    if not isinstance(v, str) or len(v) > cap or not v.isprintable():
        raise ValueError
    return v


def _match(v: Any, pattern: re.Pattern) -> str:
    if not isinstance(v, str) or not pattern.fullmatch(v):
        raise ValueError
    return v


def _choice(v: Any, allowed) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise ValueError
    return v


def _list(v: Any, cap: int, check: Callable[[Any], str]) -> tuple[str, ...]:
    if not isinstance(v, list) or len(v) > cap:
        raise ValueError
    out = tuple(check(x) for x in v)
    if len(set(out)) != len(out):
        raise ValueError
    return out


def _profile_name(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError
    validate_profile_name(v)
    return v


def _broker(v: Any) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) != {"slug", "route_id"}:
        raise ValueError
    return {"slug": _match(v["slug"], _SLUG_RE), "route_id": _match(v["route_id"], _SLUG_RE)}


def env_name_allowed(name: str) -> bool:
    return bool(_ENV_NAME_RE.fullmatch(name)) and name not in _DENY_NAMES and not name.startswith(_DENY_PREFIXES)


def _env_names(v: Any) -> tuple[str, ...]:
    def one(x: Any) -> str:
        if not isinstance(x, str) or not env_name_allowed(x):
            raise ValueError
        return x
    return _list(v, MAX_ENV_NAMES, one)


def summary_lines(m: BundleManifest) -> list[str]:
    head = CONTAINS.format(items=", ".join(m.contents)) if m.contents else CONTAINS_NOTHING
    return [head, NOT_CONTAINED, LEFT_HERE]


# ---------------------------------------------------------------- bounded archive reading

def _gunzip_bounded(src: Path, dst: Path, *, max_in: int, max_out: int) -> int:
    """Inflate SRC into a NEW file DST, stopping as soon as the output passes MAX_OUT or the ratio cap.
    A decompression bomb is never fully inflated."""
    try:
        size = os.stat(src).st_size
    except OSError:
        raise BundleError(NOT_READABLE) from None
    if size > max_in:
        raise BundleError(TOO_LARGE)
    total = 0
    try:
        with gzip.open(src, "rb") as g, open(dst, "xb") as out:
            while True:
                chunk = g.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_out or (total > RATIO_FLOOR and total > MAX_RATIO * max(size, 1)):
                    raise BundleError(TOO_LARGE)
                out.write(chunk)
    except (OSError, EOFError, zlib.error):
        raise BundleError(NOT_A_BUNDLE) from None
    return total


def _name_ok(name: str) -> bool:
    if not name or len(name) > MAX_PATH or "\\" in name or name.startswith("/"):
        return False
    if re.match(r"[A-Za-z]:", name) or not all(c.isprintable() for c in name):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _read_entries(tar_path: Path, *, max_members: int, max_member: int, max_total: int
                  ) -> list[tuple[str, bool, bytes]]:
    """(name, is_dir, data) for every member, after checking each one: a safe relative name, a regular file
    or directory only, no duplicates (also case- and Unicode-insensitive), within the caps."""
    entries: list[tuple[str, bool, bytes]] = []
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(tar_path, "r:") as tar:
            for count, m in enumerate(tar, 1):
                if count > max_members:
                    raise BundleError(TOO_MANY.format(n=max_members))
                if not _name_ok(m.name) or not (m.isreg() or m.isdir()) or m.issparse():
                    raise BundleError(UNSAFE_MEMBER)
                key = unicodedata.normalize("NFC", m.name).casefold()
                if key in seen:
                    raise BundleError(UNSAFE_MEMBER)
                seen.add(key)
                if m.isdir():
                    entries.append((m.name, True, b""))
                    continue
                total += m.size
                if m.size > max_member or total > max_total:
                    raise BundleError(TOO_LARGE)
                f = tar.extractfile(m)
                data = f.read() if f is not None else b""
                if len(data) != m.size:
                    raise BundleError(NOT_A_BUNDLE)
                entries.append((m.name, False, data))
    except tarfile.TarError:
        raise BundleError(NOT_A_BUNDLE) from None
    return entries


def _load_profile_archive(gz_path: Path, work: Path) -> list[tuple[str, bool, bytes]]:
    """The profile archive as (path relative to its one top directory, is_dir, data); the top dir itself
    is dropped. Raises BundleError for anything unsafe or not shaped like a Hermes profile export."""
    tar_path = work / f"inflated-{_rand.token_hex(4)}.tar"
    _gunzip_bounded(gz_path, tar_path, max_in=MAX_PROFILE_COMPRESSED, max_out=MAX_PROFILE_EXPANDED)
    entries = _read_entries(tar_path, max_members=MAX_MEMBERS, max_member=MAX_MEMBER_BYTES,
                            max_total=MAX_PROFILE_EXPANDED)
    tar_path.unlink()
    tops = {name.split("/", 1)[0] for name, _, _ in entries}
    if len(tops) != 1:
        raise BundleError(BAD_LAYOUT)
    top = tops.pop()
    out = []
    for name, is_dir, data in entries:
        if name == top:
            if not is_dir:
                raise BundleError(BAD_LAYOUT)
            continue
        out.append((name[len(top) + 1:], is_dir, data))
    return out


def _credential_named(rel: str) -> bool:
    for part in rel.lower().split("/"):
        if part.startswith((".env", "token")) or part == "auth.json" or part.endswith((".pem", ".key")):
            return True
    return False


def _kept(rel: str) -> bool:
    return rel in KEPT_FILES or rel.split("/", 1)[0] in KEPT_DIRS


def _contents(entries) -> tuple[str, ...]:
    rels = [rel for rel, _, _ in entries]
    have = {
        "memories": any(r.split("/", 1)[0] == "memories" for r in rels),
        "skills": any(r.split("/", 1)[0] == "skills" for r in rels),
        "config": "config.yaml" in rels,
        "SOUL.md": "SOUL.md" in rels,
    }
    return tuple(label for label in CONTENT_LABELS if have[label])


def _printable(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def _hard_reason(data: bytes, values: list[str]) -> str | None:
    """Why DATA must never leave this computer, or None. Exact .env values and high-confidence token shapes."""
    text = data.decode("utf-8", errors="replace")
    if values and scrub(text, values) != text:
        return WHY_ENV_VALUE
    if any(p.search(text) for p in _HARD_SHAPES):
        return WHY_TOKEN
    for m in _URL_PARAM.finditer(text):
        if any(k in m.group(2).lower() for k in _SENSITIVE_NAME) and _TOKENISH.fullmatch(m.group(3)):
            return WHY_TOKEN
    return None


def _soft_reason(data: bytes) -> str | None:
    """A plain hex or base64 run or a token-like URL value: usually a hash or an example. Warn by name."""
    text = data.decode("utf-8", errors="replace")
    return WHY_SOFT if redact_patterns(text) != text else None


def _clip(value: Any) -> str:
    text = redact_patterns(_printable(str(value)))
    return text if len(text) <= CLIP else text[:CLIP] + "..."


# ---------------------------------------------------------------- stock skills Hermes seeds

def _skill_name(skill_md: bytes, fallback: str) -> str:
    """Hermes' read_skill_name: the frontmatter ``name:`` in the first 4000 characters, else FALLBACK."""
    lines = [ln.strip() for ln in skill_md.decode("utf-8-sig", errors="replace")[:4000].split("\n")]
    if "---" not in lines:
        return fallback
    block = lines[lines.index("---") + 1:]
    block = block[:block.index("---")] if "---" in block else block
    values = (ln.split(":", 1)[1].strip().strip("\"'") for ln in block if ln.startswith("name:"))
    return next((v for v in values if v), fallback)


def _is_cache_file(rel: str, rels: set[str]) -> bool:
    parts = rel.split("/")
    return any(part in _RUNTIME_CACHE_DIRS for part in parts[:-1]) or (
        rel.endswith((".pyc", ".pyo")) and rel.rsplit(".", 1)[0] + ".py" in rels)


def _dir_hash(files: list[tuple[str, bytes]], *, include_cache: bool) -> str:
    """Hermes' tools/skills_sync.dir_hash over (path relative to the skill dir, bytes), VERIFIED against a real
    0.23 profile (44 of 44 seeded skills matched). md5 is Hermes' own change detector, not a security check."""
    rels = {r for r, _ in files}
    h = hashlib.md5(usedforsecurity=False)
    for rel, data in sorted(files, key=lambda f: tuple(f[0].split("/"))):
        if include_cache or not _is_cache_file(rel, rels):
            h.update(rel.encode("utf-8"))
            h.update(data)
    return h.hexdigest()


def _disk_bytes(pdir: Path, rel: str, budget: list[int]) -> bytes:
    """The bytes of PDIR/REL as they are on disk, read-only. Every component is opened relative to its parent with
    O_NOFOLLOW, the leaf also with O_NONBLOCK so a FIFO swapped in cannot hang the read, and the opened fd must be a
    regular file. Raises BundleError with a line that says what went wrong (never any content)."""
    import errno
    shown = _printable(redact_patterns(rel))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    fd = -1
    try:
        fd = os.open(pdir, flags_dir)
        parts = rel.split("/")
        for part in parts[:-1]:
            nxt = os.open(part, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
    except OSError as e:
        if fd >= 0:
            os.close(fd)
        if e.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1)):
            raise BundleError(DISK_LINK.format(rel=shown)) from None
        if e.errno == errno.ENOENT:
            raise BundleError(DISK_GONE.format(rel=shown)) from None
        if e.errno == errno.ENXIO:
            raise BundleError(DISK_SPECIAL.format(rel=shown)) from None
        raise BundleError(DISK_UNREADABLE.format(rel=shown)) from None
    os.close(fd)
    try:
        st = os.fstat(leaf)
        if not stat.S_ISREG(st.st_mode):
            raise BundleError(DISK_SPECIAL.format(rel=shown))
        if st.st_size > MAX_MEMBER_BYTES:
            raise BundleError(DISK_TOO_BIG.format(rel=shown))
        budget[0] -= st.st_size
        if budget[0] < 0:
            raise BundleError(TOO_LARGE)
        chunks, total = [], 0
        while True:
            chunk = os.read(leaf, CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_MEMBER_BYTES:
                raise BundleError(DISK_TOO_BIG.format(rel=shown))
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise BundleError(DISK_UNREADABLE.format(rel=shown)) from None
    finally:
        os.close(leaf)


# Text files Hermes 0.23's export force-redacts (hermes_cli/profiles_export_scrub.py, _EXPORT_REDACT_SUFFIXES and
# _should_redact_export_file). A difference between such a file and its disk copy means Hermes masked something.
_HERMES_REDACT_SUFFIXES = frozenset({
    ".md", ".txt", ".yaml", ".yml", ".json", ".jsonl", ".toml", ".ini", ".cfg", ".conf", ".py", ".sh", ".bash",
    ".zsh", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".xml", ".csv"})


def _hermes_redacts(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return (suffix in _HERMES_REDACT_SUFFIXES or name == ".cursorrules" or name.endswith(".env.example")
            or name.startswith("config.yaml."))


# A secret written after its name: "db password: X", "the token is X", "api_key = X". Real Hermes leaves some of
# these whole (a space before the key word), so the difference rule alone would miss them. Hard refusal.
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:password|passwd|passphrase|pwd|secret|token|api[ _-]?key|access[ _-]?key"
    r"|private[ _-]?key|client[ _-]?secret|auth[ _-]?token)s?(?![A-Za-z0-9_])[\"']?\s*(?:[:=]|\bis\b)\s*[\"']?"
    r"(?P<v>[^\s\"'`,;]{8,})")


def _assignment_reason(data: bytes) -> str | None:
    text = data.decode("utf-8", errors="replace")
    for m in _ASSIGN.finditer(text):
        v = m.group("v")
        if v.startswith(("${", "***")) or not any(c.isdigit() for c in v) or not any(c.isalpha() for c in v):
            continue
        return WHY_ASSIGNMENT
    return None


def _disk_skill_files(skill_dir: Path, budget: list[int]) -> list[tuple[str, bytes]] | None:
    """(path relative to SKILL_DIR, bytes) for every regular file under it, read from disk. None if it holds a
    link or anything odd, or the shared read BUDGET runs out: such a dir is never treated as stock."""
    out: list[tuple[str, bytes]] = []
    for root, dirs, names in os.walk(skill_dir, followlinks=False):
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                return None
        for n in names:
            full = os.path.join(root, n)
            st = os.lstat(full)
            if not stat.S_ISREG(st.st_mode):
                return None
            budget[0] -= st.st_size
            if budget[0] < 0:
                return None
            with open(full, "rb") as f:
                out.append((Path(full).relative_to(skill_dir).as_posix(), f.read()))
    return out


def _stock_skill_files(entries, pdir: Path) -> set[str]:
    """Archive paths of skill files Hermes seeded and the user never changed: the skill dir's hash ON DISK
    equals the entry for its name in ``skills/.bundled_manifest`` (``name:md5`` lines). The disk is used, not
    the archive, because Hermes' export rewrites secret-looking example text inside files (VERIFIED on 0.23.x).
    When the match ignored runtime cache folders, files in them are NOT stock (a user file there is kept).
    Read-only; links are not followed."""
    man_path = pdir / "skills" / ".bundled_manifest"
    try:
        if man_path.is_symlink() or not man_path.is_file() or man_path.stat().st_size > MAX_CONFIG_BYTES:
            return set()
        raw = man_path.read_bytes()
    except OSError:
        return set()
    manifest: dict[str, str] = {}
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        name, sep, digest = line.strip().partition(":")
        if sep and re.fullmatch(r"[0-9a-f]{32}", digest.strip()):
            manifest[name.strip()] = digest.strip()
    rels = [rel for rel, _, _ in entries]
    budget = [MAX_PROFILE_EXPANDED]
    stock: set[str] = set()
    for rel in rels:
        if not rel.startswith("skills/") or rel.rsplit("/", 1)[-1] != "SKILL.md" or rel.count("/") < 2:
            continue
        base = rel[: -len("SKILL.md")]
        skill_dir = pdir / base
        try:
            if any(q.is_symlink() for q in (skill_dir, *skill_dir.parents) if pdir in q.parents or q == pdir) \
                    or not skill_dir.is_dir():
                continue
            files = _disk_skill_files(skill_dir, budget)
        except OSError:
            continue
        if files is None:
            continue
        md = dict(files).get("SKILL.md")
        want = manifest.get(_skill_name(md, base.rstrip("/").rsplit("/", 1)[-1])) if md is not None else None
        if not want:
            continue
        names = {r for r, _ in files}
        if want == _dir_hash(files, include_cache=False):
            hashed = {r for r in names if not _is_cache_file(r, names)}
        elif want == _dir_hash(files, include_cache=True):
            hashed = names
        else:
            continue
        stock.update(base + r for r in hashed)
    return stock


def _orphan_description(rel: str, kept_files) -> bool:
    parts = rel.split("/")
    if len(parts) != 3 or parts[0] != "skills" or parts[2] != "DESCRIPTION.md":
        return False
    prefix = f"skills/{parts[1]}/"
    return not any(r.startswith(prefix) and r != rel for r, _, _ in kept_files)


def _hermes_skill_state(rel: str) -> bool:
    return rel == "skills/.bundled_manifest" or rel == "skills/.hub" or rel.startswith("skills/.hub/")


def _masked_reason(data: bytes) -> str | None:
    """Hermes' export masks a real token as its first 6 characters, ``...`` and its last 4 (``ghp_xa...NGp7``)
    or as ``[REDACTED ...]``. Seeing that shape in the archive means a real key sat in the file."""
    text = data.decode("utf-8", errors="replace")
    return WHY_MASKED if _MASKED.search(text) else None


# ---------------------------------------------------------------- config.yaml checks

_YAML11_YN = re.compile(r"^(?:y|Y|n|N)$")


def _yaml_classes() -> tuple[type, type]:
    """(loader, dumper) that read and write config.yaml the way Hermes 0.23 reads it (hermes_yaml.py: ruamel,
    YAML 1.1, so bare y and n are booleans, and duplicate keys are errors). PyYAML is YAML 1.1 already except for
    y and n. The loader also refuses a setting name that is not text. Built once, lazily."""
    global _YAML_CLASSES
    if _YAML_CLASSES is None:
        import yaml

        class Loader(yaml.SafeLoader):
            bool_values = {**yaml.SafeLoader.bool_values, "y": True, "n": False}

            def construct_mapping(self, node, deep=False):
                if isinstance(node, yaml.MappingNode):
                    self.flatten_mapping(node)
                seen: set[str] = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=True)
                    if not isinstance(key, str) or key in seen:
                        raise BundleError(CONFIG_ODD_KEYS)
                    seen.add(key)
                return super().construct_mapping(node, deep)

        class Dumper(yaml.SafeDumper):
            def ignore_aliases(self, data):
                return True

        for cls in (Loader, Dumper):
            cls.add_implicit_resolver("tag:yaml.org,2002:bool", _YAML11_YN, list("yYnN"))
        _YAML_CLASSES = (Loader, Dumper)
    return _YAML_CLASSES


_YAML_CLASSES: tuple[type, type] | None = None


def _load_config(data: bytes) -> Any:
    if len(data) > MAX_CONFIG_BYTES:
        raise BundleError(CONFIG_COMPLEX)
    import yaml
    loader, _ = _yaml_classes()
    try:
        return yaml.load(data.decode("utf-8"), Loader=loader)  # noqa: S506 - a SafeLoader subclass
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError, TypeError):
        raise BundleError(CONFIG_UNREADABLE) from None


def _dump_config(tree: Any) -> bytes:
    """R50: the bytes an import writes are always a fresh dump of the tree that was checked, and they must read
    back as exactly that tree (y and n as text are quoted, no anchors). Comments are not kept."""
    import yaml
    _, dumper = _yaml_classes()
    tree = {} if tree is None else tree
    out = yaml.dump(tree, Dumper=dumper, sort_keys=False, allow_unicode=True, default_flow_style=False).encode()
    if _load_config(out) != tree:
        raise BundleError(CONFIG_UNREADABLE)
    return out


def _key_words(key: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
    return [w for w in re.split(r"[^a-z0-9]+", spaced) if w]


def _safe_key(key: str) -> bool:
    """A key whose value is a name, address, kind or count, never a secret (``key_env``, ``token_url``,
    ``auth_type``, ``token_limit``). Also exempts it from a secret-named parent."""
    words = _key_words(key)
    return bool(words) and (words[-1] in _NOT_A_VALUE_LAST or any(w in _COUNT_WORDS for w in words))


def _secret_word(w: str) -> bool:
    """Singular or plural: tokens, passwords, secrets, credentials, signatures, api_keys."""
    return w in _SECRET_WORDS or (len(w) > 3 and w.endswith("s") and w[:-1] in _SECRET_WORDS)


def _secret_key(key: str) -> bool:
    """A key whose value would be a secret: a key-like word (key, secret, token, password, auth, credential,
    cookie, signature, bearer; singular or plural), unless ``_safe_key`` says the value is a name, address, kind
    or count."""
    words = _key_words(key)
    return any(_secret_word(w) for w in words) and not _safe_key(key)


def _safe_path(path: tuple[str, ...]) -> bool:
    return any(".".join(path[:i]) in SAFE_CONFIG_PATHS for i in range(1, len(path) + 1))


def _map_key(key: str) -> bool:
    """A key whose MAP value is an env or header map: every literal inside it is a secret (strict rule)."""
    k = key.lower()
    return k in ("env", "headers") or k.endswith(("_headers", "_env"))


def _is_reference(value: str) -> bool:
    rest = _STRICT_REF.sub("", value)
    return rest != value and re.fullmatch(r"(?i)\s*(?:bearer|token|basic)?\s*", rest) is not None


def _literal_problem(value: Any, strict: bool, secret: bool) -> bool:
    """Booleans and null are never secrets. Strings are fine when empty or a ``${NAME}`` reference. Any other
    scalar (string, number, date, binary) under a secret key or in an env/header map is a literal secret."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str) and (value == "" or _is_reference(value)):
        return False
    return strict or secret


def _args_key(key: str) -> bool:
    """``args``, ``argv``, ``chromium_args``, ``docker_extra_args``: a list of command line arguments."""
    words = _key_words(key)
    return bool(words) and words[-1] in ("args", "argv")


def _secret_flag(item: str) -> tuple[bool, str | None]:
    """(ITEM is a flag whose name holds a secret word, the value after ``=`` or None). ``-k`` counts too."""
    if not item.startswith("-") or item.startswith("---"):
        return False, None
    name, eq, value = item.lstrip("-").partition("=")
    secret = name == "k" or (bool(name) and _secret_key(name))
    return secret, (value if eq else None)


def _args_literals(v: Any, path: tuple[str, ...]) -> list[str]:
    """Paths of literal secrets in a command line: the item after a secret-word flag (``--api-key X``, ``-k X``)
    and the value part of ``--token=X``. A ``${NAME}`` reference is fine. A string is split on spaces and
    named by the whole setting's path."""
    if isinstance(v, str):
        items, named = v.split(), lambda i: ".".join(path)
    elif isinstance(v, list):
        items, named = v, lambda i: ".".join((*path, str(i)))
    else:
        return []
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str):
            continue
        secret, value = _secret_flag(item)
        if not secret:
            continue
        if value is not None:
            if value and not _is_reference(value):
                out.append(named(i))
        elif i + 1 < len(items):
            nxt = items[i + 1]
            if nxt is None or isinstance(nxt, bool) or (isinstance(nxt, str) and (
                    nxt == "" or nxt.startswith("--") or _is_reference(nxt))):
                continue
            out.append(named(i + 1))
    return list(dict.fromkeys(out))


def _walk_config(cfg: Any) -> tuple[list[str], set[str]]:
    """(dotted key paths holding a literal secret, Hermes setting names referenced in env maps). Iterative and
    capped, so an alias bomb or a deep nest is refused instead of walked. Secret-ness is inherited by every
    value under a secret-named key, except keys ``_safe_key`` exempts."""
    literal: list[str] = []
    hermes_names: set[str] = set()
    stack: list[tuple[Any, tuple[str, ...], bool, bool]] = [(cfg, (), False, False)]
    seen = 0
    while stack:
        node, path, strict, secret = stack.pop()
        seen += 1
        if seen > MAX_CONFIG_NODES or len(path) > MAX_CONFIG_DEPTH:
            raise BundleError(CONFIG_COMPLEX)
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                if strict and key.startswith("HERMES_"):
                    hermes_names.add(key)
                if _args_key(key):
                    literal.extend(_args_literals(v, (*path, key)))
                child_strict = strict or (isinstance(v, dict) and _map_key(key)) or (
                    isinstance(v, list) and key.lower() in ("env", "headers"))
                child_path = (*path, key)
                child_secret = (_secret_key(key) or (secret and not _safe_key(key))) and not _safe_path(child_path)
                stack.append((v, child_path, child_strict, child_secret))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                stack.append((v, (*path, str(i)), strict, secret))
        else:
            if isinstance(node, str):
                hermes_names.update(n for n in _ANY_REF.findall(node) if n.startswith("HERMES_"))
            if _literal_problem(node, strict, secret):
                literal.append(".".join(path))
    return sorted(literal), hermes_names


def _paths_text(paths: list[str]) -> str:
    shown = [_clip(p)[:200] for p in paths[:5]]
    return ", ".join(shown) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


_MISSING = object()


def _get(cfg: Any, path: str) -> Any:
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _on(v: Any) -> bool:
    """Hermes reads these switches with Python ``bool()``, so even the string "false" turns them on."""
    return bool(v)


def _not_plain_true(v: Any) -> bool:
    return not (v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "on", "1")))


def _not_manual_or_smart(v: Any) -> bool:
    """Hermes loads YAML 1.1, where bare y, n, yes, no, on, off are booleans and False normalizes to off. Anything
    but the exact strings manual or smart is treated as weakening approvals."""
    return not (isinstance(v, str) and v in ("manual", "smart"))


def _not_plainly_false(v: Any) -> bool:
    if v is None or v is False or (isinstance(v, int) and not isinstance(v, bool) and v == 0):
        return False
    return not (isinstance(v, str) and v.strip().lower() in ("", "false", "no", "n", "off", "0"))


def _globby(v: Any) -> bool:
    items = v if isinstance(v, list) else [v]
    return any(not isinstance(x, str) or any(c in x for c in "*?[") for x in items)


def _nonempty(v: Any) -> bool:
    return v not in (None, "", [], {})


# R47 tier 1: settings an imported config.yaml may not carry at all. Each runs a program at every start or
# weakens the command approval gate (Hermes 0.23: agent/secret_sources/command.py runs secrets.command through
# /bin/sh -c from hermes_cli/env_loader.apply_all; skills.inline_shell runs shell found in skill text;
# plugins.host.launcher and terminal.shell_init_files run at session start; the rest switch approvals or
# redaction off). Checked before anything else, by key path only.
REFUSED_SETTINGS: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    ("secrets.command.enabled", _on),
    ("skills.inline_shell", _on),
    ("command_allowlist", _globby),
    ("delegation.subagent_auto_approve", _on),
    ("plugins.host.launcher", _nonempty),
    ("terminal.shell_init_files", _nonempty),
    ("security.redact_secrets", _not_plain_true),
    ("hooks_auto_accept", _not_plainly_false),
    ("approvals.mode", _not_manual_or_smart),
)
# R47 tier 2: shown before the default-No confirm. Leaves under these key names, at any depth, are programs.
SHOWN_LEAF_KEYS = frozenset({"command", "args", "binary_path", "executable_path", "launcher", "ssh_path", "cmd",
                             "exec", "shell", "program", "script", "cwd"})
# Values under these key names (or ending in _url, _endpoint, _proxy) are addresses the agent talks to.
SHOWN_ADDRESS_KEYS = frozenset({"base_url", "url", "endpoint", "proxy"})
# Whole settings shown as they are.
SHOWN_PATHS = ("lsp.servers", "plugins.enabled", "terminal.env_passthrough", "terminal.client_env_passthrough",
               "terminal.docker_forward_env", "skills.auto_load", "skills.external_dirs")
# Top-level keys of Hermes 0.23's own config: hermes_cli/config_defaults.DEFAULT_CONFIG, listed from the
# installed package on 2026-11-13, plus mcp_servers (read by the MCP loader, not in the defaults). A bundle key
# outside this set is named in the summary, because this tool cannot say what it does.
KNOWN_TOP_KEYS = frozenset({
    "_config_version", "agent", "approvals", "attachments", "auth", "auxiliary", "bedrock", "bot_desktop",
    "bot_mode", "browser", "checkpoints", "code_execution", "command_allowlist", "compression", "computer_use",
    "context", "context_file_max_chars", "context_file_read_timeout", "credential_pool_strategies", "cron",
    "curator", "dashboard", "database", "delegation", "desktop", "discord", "display", "doctor", "fallback",
    "fallback_providers", "file_read_max_chars", "gateway", "goals", "hooks", "hooks_auto_accept",
    "hosted_rooms", "human_delay", "kanban", "local_runtime", "logging", "loops", "lsp", "matrix", "mattermost",
    "max_concurrent_sessions", "max_live_sessions", "mcp", "mcp_discovery_timeout",
    "mcp_single_query_discovery_timeout", "memory", "moa", "model", "model_catalog", "model_overrides",
    "models_dev", "monitoring", "network", "nous", "onboarding", "openrouter", "paste_collapse_char_threshold",
    "paste_collapse_threshold", "paste_collapse_threshold_fallback", "personalities", "platform_hints",
    "plugins", "prefill_messages_file", "privacy", "prompt_caching", "providers", "proxy", "quick_commands",
    "runtime", "secrets", "security", "session", "sessions", "skills", "slack", "streaming", "stt", "telegram",
    "telemetry", "terminal", "timezone", "tool_loop_guardrails", "tool_output", "tools", "toolsets", "tts",
    "updates", "vault", "vertex", "vision", "voice", "wake_word", "web", "webapp", "whatsapp", "worktrees",
    "x_search", "mcp_servers",
})
_SPECIAL_SUBTREES = ("mcp_servers", "hooks", "quick_commands")


# R49: the ONLY config.yaml settings an import brings in, as LEAF paths ("*" = any one key or list index). A leaf
# keeps a plain value only (text, number, true/false, empty, or a list of those) that passes its rule in
# SAFE_VALUE_RULES; a mapping under a leaf name is never kept whole. Everything else is dropped and listed, including
# any child Hermes does not define. Sources (Hermes 0.23.1, read-only, 2026-11-13):
#   D = hermes_cli/config_defaults.DEFAULT_CONFIG (the default config, walked leaf by leaf)
#   P = hermes_cli/config_providers._KNOWN_PROVIDER_KEYS and _normalize_custom_provider_entry (provider entries)
#   M = hermes_cli/runtime_provider_backends.py and the model_cfg.get(...) reads (the model section)
#   F = hermes_cli/fallback_config.py (fallback_providers entries)
#   H = hermes_cli/personality.py (personalities: plain prompt text)
_PROVIDER_LEAVES = (                                            # P, minus key_cmd, ssl_*, extra_* and headers
    "provider", "name", "base_url", "url", "api", "api_key", "key_env", "api_key_env", "api_mode", "transport",
    "model", "default_model", "models", "models.*", "models.*.id", "models.*.name", "models.*.context_length",
    "models.*.max_tokens", "models_discovered", "context_length", "max_tokens", "temperature", "reasoning_effort",
    "rate_limit_delay", "request_timeout_seconds", "stale_timeout_seconds", "discover_models", "capabilities.*",
    "catalog_provider",
)
_MODEL_LEAVES = ("provider", "default", "model", "name", "base_url", "api_key", "key_env", "api_key_env",  # M
                 "api_mode", "context_length", "max_tokens", "temperature", "reasoning_effort", "supports_vision")
_DISPLAY_LEAVES = (                                             # D, minus tool_progress_command, status_phrases
    "compact", "personality", "resume_display", "resume_exchanges", "resume_max_user_chars",
    "resume_max_assistant_chars", "resume_max_assistant_lines", "resume_skip_tool_only", "busy_input_mode",
    "busy_steer_ack_enabled", "cli_multiline_shortcuts", "interface", "cli_layout", "tui_native",
    "tui_auto_resume_recent", "resume_last_session", "tui_agents_nudge", "bell_on_complete", "bell_on_prompt",
    "show_reasoning", "reasoning_full", "memory_notifications", "background_process_notifications", "streaming",
    "timestamps", "timestamp_format", "final_response_markdown", "persistent_output", "persistent_output_max_lines",
    "cli_rebuild_scrollback_on_redraw", "persist_prompts", "inline_diffs", "inline_images", "file_mutation_verifier",
    "credits_notices", "turn_completion_explainer", "show_cost", "battery", "focus_view", "focus_saved_tool_progress",
    "skin", "language", "tui_status_indicator", "cli_refresh_interval", "vim_mode", "user_message_preview.first_lines",
    "user_message_preview.last_lines", "interim_assistant_messages", "suppress_warning_notifications",
    "show_commentary", "tool_preview_length", "friendly_tool_labels", "turn_summary", "spinner_token_flow",
    "tool_progress_grouping", "reasoning_style", "ephemeral_system_ttl", "platforms.*.streaming",
    "runtime_footer.enabled", "runtime_footer.fields", "status_bar.fields", "copy_shortcut", "pet.enabled",
    "pet.slug", "pet.render_mode", "pet.scale", "pet.unicode_cols",
)
_COMPRESSION_LEAVES = (                                         # D, minus codex_app_server_auto
    "enabled", "checkpoint_required", "progress_notices", "threshold", "threshold_tokens", "target_ratio", "tail_mode",
    "protect_last_n", "min_tail_user_messages", "max_attempts", "proactive_prune_tokens",
    "proactive_prune_min_result_chars", "proactive_prune_min_reclaim_tokens", "micro_compact",
    "micro_compact_every_n_turns", "micro_compact_defrag_threshold_tokens", "hygiene_hard_message_limit",
    "hygiene_timeout_seconds", "hygiene_total_ceiling_seconds", "hygiene_failure_cooldown_seconds",
    "hygiene_max_turn_hold_seconds", "context_timeout_seconds", "context_total_ceiling_seconds", "protect_first_n",
    "abort_on_summary_failure", "codex_gpt55_autoraise", "codex_gpt55_autoraise_notice", "codex_responses_native",
    "codex_responses_compact_threshold", "in_place", "model_thresholds.*", "idle_compact_after_seconds",
    "prepare_ahead",
)


def _under(prefix: str, leaves) -> set[str]:
    return {f"{prefix}.{leaf}" for leaf in leaves}


KNOWN_SAFE_CONFIG = frozenset({
    "_config_version",                                                                          # D (written by Hermes)
    "model", *_under("model", _MODEL_LEAVES),                                                   # M (model may be text)
    *_under("providers.*", _PROVIDER_LEAVES),                                                   # P
    *_under("custom_providers.*", _PROVIDER_LEAVES),                                            # P (legacy list form)
    *_under("fallback_providers.*", _PROVIDER_LEAVES),                                          # F (same entry shape)
    "fallback.min_switch_reset_seconds",                                                        # D
    "auth.adopt_external_logins",                                                               # D
    "context.engine",                                                                           # D, value rule below
    *_under("context.memory_trim", ("enabled", "cooldown_seconds", "log_every_n", "info_log_min_delta_mb")),  # D
    *_under("display", _DISPLAY_LEAVES),                                                        # D
    *_under("memory", ("memory_enabled", "user_profile_enabled", "memory_char_limit", "user_char_limit",
                       "nudge_interval")),                     # D, minus provider, write_approval, prefetch_spill
    *_under("compression", _COMPRESSION_LEAVES),                                                # D
    *_under("streaming", ("enabled", "transport", "edit_interval", "buffer_threshold", "cursor",
                          "fresh_final_after_seconds")),                                        # D
    *_under("tool_output", ("max_bytes", "max_lines", "max_line_length")),                      # D
    *_under("human_delay", ("mode", "min_ms", "max_ms")),                                       # D
    "personalities.*", *_under("personalities.*", ("system_prompt", "tone", "style", "description")),  # H
    "prompt_caching.cache_ttl", "timezone", "privacy.redact_pii", "goals.max_turns",            # D
    *_under("loops", ("min_interval_seconds", "max_ticks", "self_paced_floor_seconds",
                      "self_paced_ceiling_seconds")),                                           # D
    *_under("tool_loop_guardrails", ("warnings_enabled", "hard_stop_enabled", "non_interactive_hard_stop_enabled",
                                     "warn_after.exact_failure", "warn_after.same_tool_failure",
                                     "warn_after.idempotent_no_progress", "hard_stop_after.exact_failure",
                                     "hard_stop_after.same_tool_failure", "hard_stop_after.idempotent_no_progress",
                                     "loop_caps.max_web_searches", "loop_caps.max_subagents")),  # D
    *_under("vision", ("embed_target_bytes", "max_calls_per_image", "replay_recent_images")),   # D
    "context_file_max_chars", "file_read_max_chars",                                            # D
    *_under("agent", ("max_turns", "text_verbosity", "reasoning_effort", "reasoning_overrides.*", "disabled_toolsets",
                      "service_tier")),                                                         # D
    "skills.template_vars",                                                                     # D (a true/false switch)
})
# Subtrees kept whole. Each must be listed in ALLOWLIST_JUSTIFIED. mcp_servers is the only one: it is not data
# only, so every server, and every setting a server carries beyond the usual ones, is shown before the confirm.
SAFE_SUBTREES = frozenset({"mcp_servers"})


def _ref_or_empty(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, str) and _is_reference(v))


def _env_name_value(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, str) and _ENV_NAME_RE.fullmatch(v) is not None)


# Leaves whose VALUE is constrained too. A value that fails its rule is dropped and listed like an unknown setting.
SAFE_VALUE_RULES: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    # Hermes imports plugins/context_engine/<name>/__init__.py with no sanitising, so only the built-in is allowed.
    ("context.engine", lambda v: v == "compressor"),
    # Maps keyed by a name the user picks: only the value type Hermes reads is kept.
    ("compression.model_thresholds.*", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)),
    ("agent.reasoning_overrides.*", lambda v: isinstance(v, str)),
    *((f"{root}.capabilities.*", lambda v: isinstance(v, bool))
      for root in ("providers.*", "custom_providers.*", "fallback_providers.*")),
    *((f"{root}.{leaf}", _ref_or_empty) for root in ("model", "providers.*", "custom_providers.*",
                                                      "fallback_providers.*") for leaf in ("api_key",)),
    *((f"{root}.{leaf}", _env_name_value) for root in ("model", "providers.*", "custom_providers.*",
                                                        "fallback_providers.*") for leaf in ("key_env", "api_key_env")),
)
# Allowlisted names that hold a risky word, and kept subtrees, each with the reason it is safe to bring in.
ALLOWLIST_JUSTIFIED: dict[str, str] = {
    "mcp_servers": "every server's command, address, folder, env references and extra settings are shown before "
                   "the default-No confirm",
    "model.base_url": "shown as an address that will receive the provider's API key",
    "model.api_key": "kept only as a ${NAME} reference, and shown as sent to the model's address",
    "model.key_env": "kept only as a plain variable name, and shown as sent to the model's address",
    "model.api_key_env": "kept only as a plain variable name, and shown as sent to the model's address",
    "display.runtime_footer": "which fields the status footer shows (model, context percent, folder)",
    "providers": "provider addresses are shown as receiving the provider's API key, with every key reference",
    "custom_providers": "provider addresses are shown as receiving the provider's API key, with every key reference",
    "fallback_providers": "provider addresses are shown as receiving the provider's API key, with every key reference",
    "context.engine": "kept only when the value is exactly compressor (the built-in engine)",
    "context_file_max_chars": "a size limit, not a file path",
    "file_read_max_chars": "a size limit, not a file path",
    "display.file_mutation_verifier": "a display toggle for showing file edit checks",
    "display.tui_auto_resume_recent": "a display toggle for resuming the last chat view",
    "display.tool_progress_grouping": "a display toggle",
    "display.inline_diffs": "a display toggle for showing diffs inline",
    "display.inline_images": "a display toggle for showing images inline",
    "display.background_process_notifications": "a display toggle for notices",
    "compression.codex_responses_native": "a context compression mode switch",
    "compression.codex_responses_compact_threshold": "a context compression size",
    "compression.codex_gpt55_autoraise": "a context compression switch",
    "compression.codex_gpt55_autoraise_notice": "a context compression notice switch",
    "compression.checkpoint_required": "a context compression switch",
    "tool_loop_guardrails.non_interactive_hard_stop_enabled": "a stop limit, it can only make the agent stop sooner",
}
_SAFE_SPECS = tuple(tuple(p.split(".")) for p in sorted(KNOWN_SAFE_CONFIG))
_RULE_SPECS = tuple((tuple(p.split(".")), rule) for p, rule in SAFE_VALUE_RULES)


def _path_match(pattern: str, path: tuple[str, ...]) -> bool:
    """PATTERN (dotted, "*" = any one key) is a prefix of PATH."""
    parts = pattern.split(".")
    return len(parts) <= len(path) and all(a == "*" or a == b for a, b in zip(parts, path))


def _spec_is(spec: tuple[str, ...], path: tuple[str, ...]) -> bool:
    return len(spec) == len(path) and all(a == "*" or a == b for a, b in zip(spec, path))


def _spec_below(spec: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """SPEC reaches below PATH (PATH is a strict prefix of it)."""
    return len(spec) > len(path) and all(a == "*" or a == b for a, b in zip(spec, path))


def _justified(dotted: str) -> bool:
    path = tuple(dotted.split("."))
    return any(_path_match(p, path) for p in ALLOWLIST_JUSTIFIED)


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, bool, int, float))


def _plain(v: Any) -> bool:
    return _scalar(v) or (isinstance(v, list) and all(_scalar(x) for x in v))


def _leaf_ok(path: tuple[str, ...], v: Any) -> bool:
    return _plain(v) and all(rule(v) for spec, rule in _RULE_SPECS if _spec_is(spec, path))


def _filter_config(cfg: Any) -> tuple[Any, list[str]]:
    """(the settings an import brings in, dotted paths it leaves out). Key and list order are kept."""
    if not isinstance(cfg, dict):
        return cfg, []
    dropped: list[str] = []

    def rec(node: Any, path: tuple[str, ...]) -> Any:
        items = list(node.items()) if isinstance(node, dict) else list(enumerate(node))
        out: Any = {} if isinstance(node, dict) else []
        for k, v in items:
            p = (*path, str(k))
            if any(_path_match(t, p) for t in SAFE_SUBTREES):
                kept = v
            elif any(_spec_is(s, p) for s in _SAFE_SPECS) and _leaf_ok(p, v):
                kept = v
            elif isinstance(v, (dict, list)) and v and any(_spec_below(s, p) for s in _SAFE_SPECS):
                kept = rec(v, p)
                if not kept:
                    continue                      # everything under it was dropped and is listed by its own path
            elif isinstance(v, (dict, list)) and not v and any(_spec_below(s, p) for s in _SAFE_SPECS):
                kept = v                          # an empty section such as providers: {}
            else:
                dropped.append(".".join(p))
                continue
            if isinstance(out, dict):
                out[k] = kept
            else:
                out.append(kept)
        return out

    return rec(cfg, ()), dropped


def _not_imported_line(dropped: list[str]) -> str:
    return f"{NOT_IMPORTED_HEAD} {', '.join(_clip(d) for d in dropped)}. {NOT_IMPORTED_TAIL}"


def _refused_setting(cfg: Any) -> str | None:
    if not isinstance(cfg, dict):
        return None
    for path, bad in REFUSED_SETTINGS:
        v = _get(cfg, path)
        if v is not _MISSING and bad(v):
            return path
    return None


def _address_key(key: str) -> bool:
    k = key.lower()
    return k in SHOWN_ADDRESS_KEYS or k.endswith(("_url", "_endpoint", "_proxy"))


def _flat(v: Any) -> str:
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(str(k) for k in v)
    return str(v)


def _refs_in(node: Any, budget: list[int]) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack and budget[0] > 0:
        n = stack.pop()
        budget[0] -= 1
        if isinstance(n, dict):
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)
        elif isinstance(n, str):
            out.update(_ANY_REF.findall(n))
    return out


def _capped(lines: list[str], what: str) -> list[str]:
    if len(lines) <= MAX_SHOWN:
        return lines
    return [*lines[:MAX_SHOWN], f"and {len(lines) - MAX_SHOWN} more {what}"]


_PROVIDER_ROOTS = ("model", "providers", "custom_providers", "fallback_providers")
_PROVIDER_ADDRESS_KEYS = ("base_url", "url", "api", "endpoint")
_PROGRAM_LAST_WORDS = frozenset({"cmd", "command", "exec", "bin", "binary", "script", "launcher", "program", "shell"})
# Settings that change how HTTPS connections are checked (Hermes reads ssl_verify and ssl_ca_cert on providers and
# MCP servers, client_cert and client_key on MCP servers).
_TLS_KEYS = frozenset({"ssl_verify", "verify_ssl", "tls_verify", "verify", "insecure", "ssl_ca_cert", "ca_cert",
                       "ca_bundle", "client_cert", "client_key", "ssl_cert", "ssl_key"})
_MCP_USUAL = frozenset({"command", "args", "env", "url", "headers", "cwd", "timeout", "connect_timeout", "enabled",
                        "description", "transport", "type"})
_REF_NAMES = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _program_key(key: str) -> bool:
    """``command``, ``key_cmd``, ``codex_bin``, ``binary_path``: a setting that names a program to run."""
    words = _key_words(key)
    return key.lower() in SHOWN_LEAF_KEYS or (bool(words) and words[-1] in _PROGRAM_LAST_WORDS)


def _send_lines(node: dict, where: str, address: str) -> list[str]:
    """Only values that ARE references (``${NAME}``, ``Bearer ${NAME}``) and ``key_env`` names: those are keys
    Hermes reads from the environment and sends. Other text is not a key."""
    out: list[str] = []
    stack: list[tuple[Any, str]] = [(node, where)]
    budget = 1000
    while stack and budget > 0:
        n, p = stack.pop()
        budget -= 1
        if isinstance(n, dict):
            for k, v in n.items():
                key, sub = str(k), f"{p}.{k}" if p else str(k)
                if key in ("key_env", "api_key_env") and isinstance(v, str) and _ENV_NAME_RE.fullmatch(v):
                    out.append(f"Would send the key in ${{{v}}} ({_clip(sub)}) to {_clip(address)}")
                elif isinstance(v, str) and _is_reference(v):
                    out += [f"Would send the key in ${{{ref}}} ({_clip(sub)}) to {_clip(address)}"
                            for ref in _REF_NAMES.findall(v)]
                elif isinstance(v, (dict, list)):
                    stack.append((v, sub))
        elif isinstance(n, list):
            stack.extend((v, f"{p}.{i}") for i, v in enumerate(n))
    return sorted(set(out))


def _provider_nodes(cfg: dict):
    model = cfg.get("model")
    if isinstance(model, dict):
        yield "model", model
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        yield from ((f"providers.{k}", v) for k, v in providers.items())
    for root in ("custom_providers", "fallback_providers"):
        items = cfg.get(root)
        if isinstance(items, list):
            yield from ((f"{root}.{i}", v) for i, v in enumerate(items))


def _provider_lines(cfg: dict) -> tuple[list[str], list[str]]:
    """Every provider address, said plainly to receive that provider's API key: Hermes sends the provider's key to
    a base_url set in config.yaml even with no key reference in sight (runtime_provider_backends.py treats
    provider: openrouter plus a base_url as a deliberate mirror). Then each key reference sent there."""
    addresses: list[str] = []
    sends: list[str] = []
    for where, node in _provider_nodes(cfg):
        if not isinstance(node, dict):
            continue
        keys = [k for k in _PROVIDER_ADDRESS_KEYS if not (where == "model" and k == "api")]
        found = [(k, node[k]) for k in keys if isinstance(node.get(k), str) and node[k].strip()]
        for k, addr in found:
            addresses.append(f"{_clip(where)}.{k}: {_clip(addr)} (this address will receive the API key for this "
                             "provider)")
        if found:
            sends += _send_lines(node, where, found[0][1])
    return addresses, sends


def _mcp_lines(servers: dict) -> list[str]:
    out: list[str] = []
    for name, entry in servers.items():
        shown = _clip(name)
        if not isinstance(entry, dict):
            out.append(f"MCP server {shown}")
            continue
        if entry.get("command") is not None:
            raw_args = entry.get("args")
            args = raw_args if isinstance(raw_args, list) else ([raw_args] if isinstance(raw_args, str) else [])
            out.append(f"MCP server {shown}: runs {_clip(' '.join(str(x) for x in [entry['command'], *args]))}")
        elif entry.get("url") is not None:
            out.append(f"MCP server {shown}: connects to {_clip(entry['url'])}")
        else:
            out.append(f"MCP server {shown}")
        if _nonempty(entry.get("cwd")):
            out.append(f"MCP server {shown}: runs in the folder (cwd) {_clip(entry['cwd'])}")
        env = entry.get("env")
        if isinstance(env, dict):
            out += [f"MCP server {shown}: sets {_clip(k)} to {_clip(v)}" for k, v in env.items()]
        if isinstance(entry.get("url"), str):
            out += _send_lines(entry, f"mcp_servers.{name}", entry["url"])
        other = [str(k) for k in entry if str(k) not in _MCP_USUAL and str(k) not in _TLS_KEYS]
        if other:
            out.append(f"MCP server {shown} also sets: {', '.join(_clip(k) for k in other)}")
    return out


def _tls_lines(cfg: Any) -> list[str]:
    out: list[str] = []
    stack: list[tuple[Any, tuple[str, ...]]] = [(cfg, ())]
    budget = MAX_CONFIG_NODES
    while stack and budget > 0:
        node, path = stack.pop()
        budget -= 1
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for k, v in items:
            p = (*path, str(k))
            if isinstance(node, dict) and str(k).lower() in _TLS_KEYS and not isinstance(v, (dict, list)):
                out.append(f"{_clip('.'.join(p))}: {_clip(v)} (changes how secure connections are checked)")
            elif isinstance(v, (dict, list)):
                stack.append((v, p))
    return sorted(out)


def _risky_lines(cfg: Any, hermes_names: set[str]) -> list[str]:
    """What an imported config.yaml can run or reach, in plain words (best effort, R47 tier 2). Tier 1 settings
    were refused before this runs."""
    if not isinstance(cfg, dict):
        return []
    lines: list[str] = []
    approvals = cfg.get("approvals")
    if isinstance(approvals, dict):
        mode = approvals.get("mode")
        if mode is not None and mode not in ("manual", "smart"):
            lines.append(f"Command approvals mode: {_clip(mode)}")
        for key in ("cron_mode", "single_query_mode", "unattended_mode"):
            if isinstance(approvals.get(key), str) and approvals[key].strip().lower() == "approve":
                lines.append(f"Command approvals: {key} is approve (risky commands run without asking)")
    servers = cfg.get("mcp_servers")
    if isinstance(servers, dict):
        lines += _capped(_mcp_lines(servers), "MCP server lines")
    hooks = cfg.get("hooks")
    if isinstance(hooks, dict):
        hk = []
        for event, items in hooks.items():
            for item in items if isinstance(items, list) else [items]:
                cmd = item.get("command") if isinstance(item, dict) else item
                hk.append(f"Hook on {_clip(event)}: runs {_clip(cmd)}")
        lines += _capped(hk, "hooks")
    quick = cfg.get("quick_commands")
    if isinstance(quick, dict):
        qc = []
        for name, item in quick.items():
            cmd = item.get("command") if isinstance(item, dict) else item
            qc.append(f"Quick command /{_clip(name)}: runs {_clip(cmd)}")
        lines += _capped(qc, "quick commands")
    secrets_cfg = cfg.get("secrets")
    if isinstance(secrets_cfg, dict):
        lines += _capped([f"secrets.{_clip(name)}: enabled (runs its helper program)"
                          for name, sec in secrets_cfg.items() if isinstance(sec, dict) and _on(sec.get("enabled"))],
                         "secret sources")
    shown_paths = []
    for path in SHOWN_PATHS:
        v = _get(cfg, path)
        if v is not _MISSING and _nonempty(v):
            shown_paths.append(f"{path}: {_clip(_flat(v))}")
    lines += _capped(shown_paths, "settings")
    # the generic walk: programs and addresses at any depth, outside the subtrees listed above
    programs: list[str] = []
    addresses: list[str] = []
    sends: list[str] = []
    budget = [MAX_CONFIG_NODES]
    stack: list[tuple[Any, tuple[str, ...]]] = [(cfg, ())]
    while stack and budget[0] > 0:
        node, path = stack.pop()
        budget[0] -= 1
        if not isinstance(node, dict):
            if isinstance(node, list):
                stack.extend((v, (*path, str(i))) for i, v in reversed(list(enumerate(node))))
            continue
        address = None
        for k, v in node.items():
            key = str(k)
            p = (*path, key)
            if not path and key in (*_SPECIAL_SUBTREES, *_PROVIDER_ROOTS):
                continue
            if _program_key(key) and not isinstance(v, dict) and _nonempty(v):
                programs.append(f"{'.'.join(p)}: {_clip(_flat(v))}")
            elif _address_key(key) and isinstance(v, str) and v:
                addresses.append(f"{'.'.join(p)}: {_clip(v)}")
                address = address or v
            if isinstance(v, (dict, list)):
                stack.append((v, p))
        if address is not None:
            sends += _send_lines(node, ".".join(path), address)
    provider_addresses, provider_sends = _provider_lines(cfg)
    lines += _capped(sorted(programs), "program settings")
    lines += _capped(provider_addresses + sorted(addresses), "addresses")
    lines += _capped(provider_sends + sends, "keys sent to addresses")
    lines += _capped(_tls_lines(cfg), "secure connection settings")
    unknown = sorted(str(k) for k in cfg if str(k) not in KNOWN_TOP_KEYS)
    lines += _capped([f"Setting this tool does not know: {_clip(k)}" for k in unknown], "unknown settings")
    lines += _capped([f"Hermes setting {_clip(n)} is passed to a program it starts" for n in sorted(hermes_names)],
                     "Hermes settings")
    return lines


def _build_profile_archive(top: str, entries) -> bytes:
    """A fresh gzip tar of the kept entries under TOP: regular files 0644 and directories 0755 only,
    no owner names, so nothing odd from the source archive survives."""
    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        def add(name: str, is_dir: bool, data: bytes) -> None:
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
            ti.mode = 0o755 if is_dir else 0o644
            ti.mtime = now
            ti.size = 0 if is_dir else len(data)
            tar.addfile(ti, None if is_dir else io.BytesIO(data))
        add(top, True, b"")
        for rel, is_dir, data in sorted(entries, key=lambda e: e[0]):
            add(f"{top}/{rel}", is_dir, data)
    return buf.getvalue()


# ---------------------------------------------------------------- the profile's own .env (in memory only)

_UNESCAPE = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _env_file(path: Path) -> tuple[list[str], list[str]]:
    """(names, values of MIN_VALUE_LEN or more) from a dotenv file. Values stay in memory for the secret scan
    and are never printed, logged or stored."""
    try:
        if path.is_symlink() or not path.is_file():
            return [], []
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return [], []
    names, values = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        key, sep, rest = s.partition("=")
        if not sep:
            continue
        key, rest = key.strip(), rest.strip()
        if _ENV_NAME_RE.fullmatch(key):
            names.append(key)
        forms = {rest}
        if rest.startswith('"'):
            out, i = [], 1
            while i < len(rest) and rest[i] != '"':
                if rest[i] == "\\" and i + 1 < len(rest):
                    out.append(_UNESCAPE.get(rest[i + 1], "\\" + rest[i + 1]))
                    i += 2
                    continue
                out.append(rest[i])
                i += 1
            forms.add("".join(out))
        elif rest.startswith("'"):
            forms.add(rest[1:].split("'", 1)[0])
        else:
            forms.add(rest.split(" #", 1)[0].strip())
        values.extend(v for v in forms if len(v) >= MIN_VALUE_LEN)
    return names, values


# ---------------------------------------------------------------- export

def _catalog_commit() -> str | None:
    try:
        from importlib import resources
        doc = json.loads(resources.files("trading_agent").joinpath("data").joinpath("catalog.json")
                         .read_text(encoding="utf-8"))
        c = doc.get("catalog_commit")
        return c if isinstance(c, str) and _COMMIT_RE.fullmatch(c) else None
    except Exception:  # noqa: BLE001 - the commit is optional metadata
        return None


def _hermes_version(hermes: HermesCli) -> str:
    try:
        v = hermes.version()
    except Exception:  # noqa: BLE001 - optional metadata
        return "unknown"
    v = _printable(str(v))[:200]
    return v if v and redact_patterns(v) == v else "unknown"


def _check_out_file(out: Path) -> None:
    if os.path.lexists(out):
        raise BundleError(OUT_EXISTS)
    if not out.parent.is_dir():
        raise BundleError(NO_PARENT)


def _check_out_dir(out: Path) -> None:
    if os.path.lexists(out):
        if out.is_symlink() or not out.is_dir():
            raise BundleError(NOT_A_FOLDER)
        if any(out.iterdir()):
            raise BundleError(DIR_NOT_EMPTY)
    elif not out.parent.is_dir():
        raise BundleError(NO_PARENT)


def _write_new_file(out: Path, data: bytes) -> None:
    """Temp file (0600) in the same folder, then linked into place so an existing file is never replaced.
    Filesystems without hard links (some USB sticks) fall back to os.replace after a fresh existence check."""
    tmp = out.parent / f".{out.name}.{_rand.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, out)
        except FileExistsError:
            raise BundleError(OUT_EXISTS) from None
        except OSError:
            if os.path.lexists(out):
                raise BundleError(OUT_EXISTS) from None
            os.replace(tmp, out)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _write_folder(out: Path, top: str, inner: bytes, manifest_bytes: bytes) -> None:
    stage = out.parent / f".{out.name}.{_rand.token_hex(8)}.tmp"
    stage.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(inner), mode="r:gz") as tar:
            tar.extractall(stage, filter="data")     # our own archive, already checked; data filter as well
        root = stage / top
        (root / BUNDLE_JSON).write_bytes(manifest_bytes)
        for p in [root, *root.rglob("*")]:
            p.chmod(0o700 if p.is_dir() else 0o600)
        try:
            os.rename(root, out)                     # replaces an EMPTY directory on POSIX
        except OSError:
            if os.path.lexists(out) and (out.is_symlink() or not out.is_dir() or any(out.iterdir())):
                raise BundleError(DIR_NOT_EMPTY) from None
            out.mkdir(mode=0o700, exist_ok=True)
            for child in root.iterdir():
                shutil.move(str(child), str(out / child.name))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _confirm_soft(soft: list[tuple[str, str]], ui: IO | None) -> None:
    rels = [_printable(redact_patterns(r)) for r, _ in soft]
    if ui is None:
        raise BundleError(SOFT_NO_ASK.format(rels=", ".join(rels[:5]) + (" and more" if len(rels) > 5 else "")))
    ui.say(SOFT_HEAD)
    for rel, (_, why) in list(zip(rels, soft))[:MAX_SHOWN]:
        ui.say(f"- {rel}: {why}")
    if len(soft) > MAX_SHOWN:
        ui.say(f"and {len(soft) - MAX_SHOWN} more")
    if not ui.confirm(SOFT_CONFIRM, default=False):
        raise ExportCancelled(EXPORT_NO)


def export(name: str, out: Path, *, hermes: HermesCli, receipt: Receipt | None, files: bool = False,
           io: IO | None = None) -> tuple[Path, BundleManifest]:
    """Export profile NAME to a bundle file OUT (or, with FILES, a readable folder OUT). Writes nothing at all
    unless every check passes. IO, when given, is used only to ask about named warnings (default No)."""
    ui = io
    validate_profile_name(name)
    out = Path(os.path.abspath(out))
    pdir = hermes.profile_dir(name)
    if pdir.is_symlink() or not pdir.is_dir():
        raise BundleError(NO_PROFILE.format(name=name))
    (_check_out_dir if files else _check_out_file)(out)
    if receipt is not None and receipt.profile_name != name:
        receipt = None
    env_names, values = _env_file(pdir / ".env")
    try:
        with tempfile.TemporaryDirectory(prefix="trading-agent-export-") as tmp:
            work = Path(tmp)
            raw = work / "hermes-export.tar.gz"
            res = hermes.profile_export(name, raw)
            if res.returncode != 0 or res.timed_out or not raw.is_file():
                detail = str(W._hermes_failure("export", res, values))
                raise BundleError(EXPORT_FAILED.format(detail=detail))
            entries = _load_profile_archive(raw, work)
            for rel, _, _ in entries:
                if _credential_named(rel):
                    raise BundleError(SECRET_FILE.format(rel=_printable(redact_patterns(rel))))
            stock = _stock_skill_files(entries, pdir)
            kept_files = [e for e in entries if not e[1] and _kept(e[0]) and e[0] not in stock
                          and not _hermes_skill_state(e[0])]
            # A category DESCRIPTION.md (skills/<category>/DESCRIPTION.md) is seeded with the stock skills; leave
            # it out unless a user skill file still lives in that category.
            kept_files = [e for e in kept_files if not _orphan_description(e[0], kept_files)]
            dirs_needed = set(KEPT_DIRS) | {r.rsplit("/", 1)[0] for r, _, _ in kept_files if "/" in r}
            for r, _, _ in kept_files:
                parts = r.split("/")
                dirs_needed.update("/".join(parts[:i]) for i in range(1, len(parts)))
            kept = [e for e in entries if e[1] and e[0] in dirs_needed] + kept_files
            soft: list[tuple[str, str]] = []
            budget = [MAX_PROFILE_EXPANDED]
            disk_config = b""
            for rel, is_dir, data in kept_files:
                shown_rel = _printable(redact_patterns(rel))
                # Hermes' export masks secret-looking text BEFORE we see the archive, so the on-disk bytes of
                # every file that goes into the bundle are authoritative for the hard checks.
                disk = _disk_bytes(pdir, rel, budget)
                why = _hard_reason(disk, values) or _hard_reason(data, values) or (
                    _assignment_reason(disk) if rel != "config.yaml" else None)  # config: key paths named first
                if why:
                    raise BundleError(SECRET_CONTENT.format(rel=shown_rel, why=why))
                if rel == "config.yaml":
                    # config.yaml gets the stricter structural checks below on its disk copy, and that copy is
                    # what ships: Hermes also masks harmless config lines (password_store: auto becomes ***).
                    disk_config = disk
                elif data != disk:
                    # R48a: any change Hermes made means it found something secret-looking (a real key, or a
                    # placeholder shaped like one). Refuse rather than ship a part of it.
                    raise BundleError(SECRET_CONTENT.format(rel=shown_rel, why=WHY_MASKED) if _hermes_redacts(rel)
                                      else DISK_CHANGED.format(rel=shown_rel))
                elif _masked_reason(data):
                    raise BundleError(SECRET_CONTENT.format(rel=shown_rel, why=WHY_MASKED))
                if redact_patterns(rel) != rel:
                    soft.append((rel, WHY_SOFT_NAME))
                elif _soft_reason(disk):
                    soft.append((rel, WHY_SOFT))
            if disk_config:
                literal, _ = _walk_config(_load_config(disk_config))
                if literal:
                    raise BundleError(CONFIG_LITERAL.format(paths=_paths_text(literal)))
                if _assignment_reason(disk_config):          # a password in a comment, say
                    raise BundleError(SECRET_CONTENT.format(rel="config.yaml", why=WHY_ASSIGNMENT))
                kept = [(r, d, disk_config if r == "config.yaml" and not d else b) for r, d, b in kept]
            config = disk_config
            ctext = config.decode("utf-8", errors="replace")
            needed = set(env_names) | set(_REF_RE.findall(ctext)) | set(_KEY_ENV_RE.findall(ctext))
            if receipt is not None:
                needed |= set(receipt.env_names_written)
            manifest = BundleManifest(
                format=FORMAT,
                created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                profile_name=name,
                hermes_version=_hermes_version(hermes),
                catalog_commit=_catalog_commit(),
                broker=_safe_broker(receipt),
                libraries=tuple(s for s in (receipt.skills if receipt else []) if _SKILL_RE.fullmatch(s))[:100],
                ai_kind=_safe_ai_kind(receipt),
                env_names_needed=tuple(sorted(n for n in needed if env_name_allowed(n)))[:MAX_ENV_NAMES],
                contents=_contents(kept),
                excludes=EXCLUDES,
            )
            manifest_bytes = (json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n").encode()
            # the catalog commit is a public 40-hex git id, which the hex-run pattern would flag
            pattern_view = json.dumps(dataclasses.replace(manifest, catalog_commit=None).to_dict()).encode()
            if _hard_reason(manifest_bytes, values) or _hard_reason(pattern_view, []) or _soft_reason(pattern_view):
                raise BundleError(SECRET_CONTENT.format(rel=BUNDLE_JSON, why=WHY_TOKEN))
            if soft:
                _confirm_soft(soft, ui)
            inner = _build_profile_archive(name, kept)
            try:
                if files:
                    _write_folder(out, name, inner, manifest_bytes)
                else:
                    buf = _iomod.BytesIO()
                    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                        for member, data in ((BUNDLE_JSON, manifest_bytes), (PROFILE_ARCHIVE, inner)):
                            ti = tarfile.TarInfo(member)
                            ti.mode, ti.size, ti.mtime = 0o600, len(data), int(time.time())
                            tar.addfile(ti, _iomod.BytesIO(data))
                    _write_new_file(out, buf.getvalue())
            except PermissionError:
                raise BundleError(NO_WRITE) from None
    finally:
        values.clear()
    return out, manifest


def _safe_broker(receipt: Receipt | None) -> dict | None:
    b = receipt.broker if receipt else None
    try:
        return _broker(dict(b)) if b else None
    except (ValueError, KeyError, TypeError):
        return None


def _safe_ai_kind(receipt: Receipt | None) -> str | None:
    kind = (receipt.ai_choice or {}).get("kind") if receipt else None
    return kind if kind in AI_KINDS else None


def export_bundle(name: str, out: Path, *, hermes: HermesCli, receipt: Receipt | None, io: IO | None = None) -> Path:
    return export(name, out, hermes=hermes, receipt=receipt, io=io)[0]


def export_files(name: str, out_dir: Path, *, hermes: HermesCli, receipt: Receipt | None,
                 io: IO | None = None) -> Path:
    return export(name, out_dir, hermes=hermes, receipt=receipt, files=True, io=io)[0]


# ---------------------------------------------------------------- import

def read_bundle(path: Path, work: Path
                ) -> tuple[BundleManifest, list[tuple[str, bool, bytes]], list[str], list[str]]:
    """Check a bundle completely, writing only into the private WORK dir. Returns the validated manifest, the
    profile entries (relative paths) and plain lines saying what its config.yaml can run. Raises BundleError
    before the caller creates anything."""
    try:
        st = os.stat(path)
    except OSError:
        raise BundleError(NOT_READABLE) from None
    if not stat.S_ISREG(st.st_mode):
        raise BundleError(NOT_READABLE)
    outer_tar = work / "outer.tar"
    _gunzip_bounded(Path(path), outer_tar, max_in=MAX_BUNDLE_BYTES,
                    max_out=MAX_PROFILE_COMPRESSED + MAX_MANIFEST_BYTES + 2**20)
    outer = _read_entries(outer_tar, max_members=2, max_member=MAX_PROFILE_COMPRESSED,
                          max_total=MAX_PROFILE_COMPRESSED + MAX_MANIFEST_BYTES)
    outer_tar.unlink()
    by_name = {name: (is_dir, data) for name, is_dir, data in outer}
    if set(by_name) != {BUNDLE_JSON, PROFILE_ARCHIVE} or any(is_dir for is_dir, _ in by_name.values()):
        raise BundleError(NOT_A_BUNDLE)
    raw_manifest = by_name[BUNDLE_JSON][1]
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise BundleError(BAD_MANIFEST)
    try:
        doc = json.loads(raw_manifest.decode("utf-8"))
    except (ValueError, RecursionError):
        raise BundleError(BAD_MANIFEST) from None
    manifest = BundleManifest.from_dict(doc)
    inner_gz = work / "profile-in.tar.gz"
    inner_gz.write_bytes(by_name[PROFILE_ARCHIVE][1])
    entries = _load_profile_archive(inner_gz, work)
    inner_gz.unlink()
    for rel, _, _ in entries:
        if _credential_named(rel) or not _kept(rel) or _hermes_skill_state(rel):
            raise BundleError(NOT_ALLOWED)
    risky: list[str] = []
    dropped: list[str] = []
    config = next((d for r, is_dir, d in entries if r == "config.yaml" and not is_dir), None)
    if config is not None:
        cfg = _load_config(config)
        refused = _refused_setting(cfg)       # tier 1 first, by key path only
        if refused:
            raise BundleError(REFUSED_SETTING.format(path=refused))
        literal, _ = _walk_config(cfg)
        if literal:
            raise BundleError(CONFIG_LITERAL_IMPORT.format(paths=_paths_text(literal)))
        if cfg is not None and not isinstance(cfg, dict):
            raise BundleError(CONFIG_NOT_MAPPING)
        kept, dropped = _filter_config(cfg)          # R49: only allowlisted leaves are brought in
        written = _dump_config(kept)                  # R50: always the checked tree, never the bundle's bytes
        entries = [(r, d, written if r == "config.yaml" and not d else b) for r, d, b in entries]
        _, hermes_names = _walk_config(kept)
        risky = _risky_lines(kept, hermes_names)
    return manifest, entries, risky, dropped


def _command_exists(hermes: HermesCli, name: str) -> bool:
    """Hermes import writes ``~/.local/bin/NAME`` and replaces a file already there (VERIFIED on 0.23.x),
    so that path, or any command of that name on PATH, makes the name unusable."""
    platform = hermes.platform
    try:
        if os.path.lexists(Path(platform.home()) / ".local" / "bin" / name):
            return True
        return platform.which(name) is not None
    except OSError:
        return True


def _ask_name(io: IO) -> str | None:
    while True:
        raw = io.ask(IMPORT_NAME_PROMPT).strip()
        if raw == BACK_INPUT:
            return None
        try:
            validate_profile_name(raw)
        except ValueError:
            io.say(S.NAME_INVALID)      # never echoes what was typed
            continue
        return raw


def _free_name(name: str, hermes: HermesCli, io: IO) -> str | None:
    while True:
        if hermes.profile_exists(name):
            prompt = S.PROFILE_TAKEN.format(name=name)
        elif _command_exists(hermes, name):
            prompt = COMMAND_TAKEN.format(name=name)
        else:
            return name
        if io.choose(prompt, [Option("rename", "Pick another name"), Option("back", "Back")]) == "back":
            return None
        picked = _ask_name(io)
        if picked is None:
            return None
        name = picked


def _ask_keys(names: tuple[str, ...], io: IO) -> dict[str, str]:
    """Ask or skip, one hidden prompt per name. Environment reuse is deliberately not offered in v1."""
    values: dict[str, str] = {}
    for env_name in names:
        while True:
            v = io.secret(S.PASTE_PROMPT.format(who=KEY_WHO, name=env_name))
            if v == "":
                break
            try:
                check_secret(env_name, v)
            except ValueError:
                io.say(S.BAD_VALUE)
                continue
            values[env_name] = v
            break
    return values


def _import_steps(name: str, archive: Path, values: dict[str, str], receipt: Receipt, hermes: HermesCli,
                  pdir: Path, store_factory: Callable[[Path], SecretStore]) -> None:
    platform = hermes.platform
    plain = list(values.values())
    def claim() -> None:
        """A half-made dir we checked absent a moment ago: mark it ours so rollback can remove it (and
        ``hermes profile delete`` removes the ~/.local/bin wrapper with it)."""
        if pdir.is_dir() and not pdir.is_symlink() and "install" not in receipt.steps_done:
            W._write_marker(pdir, receipt.run_id)
            receipt.created_paths.append(str(pdir))
            receipt.steps_done.append("install")
    try:
        res = hermes.profile_import(archive, name)
    except BaseException:
        claim()                                       # Ctrl-C or a crash while hermes was importing
        raise
    out = res.stdout or ""
    if res.returncode != 0 or res.timed_out:
        if "already exists" not in out:
            claim()
        raise W._hermes_failure("import", res, plain)
    m = _IMPORTED_AT.search(out)
    if m and os.path.realpath(m.group(1)) != os.path.realpath(pdir):
        raise W._StepError("import: hermes reported a different profile path than expected; left untouched")
    if not pdir.is_dir() or pdir.is_symlink():
        raise W._StepError("import: hermes exited 0 but the profile directory does not exist")
    W._write_marker(pdir, receipt.run_id)
    receipt.created_paths.append(str(pdir))
    receipt.steps_done.append("install")
    receipt.save(platform)
    if values:
        store = store_factory(pdir)
        for env_name, value in values.items():
            store.set(env_name, value)
            receipt.env_names_written.append(env_name)
            if str(pdir / ".env") not in receipt.created_paths:
                receipt.created_paths.append(str(pdir / ".env"))
            receipt.save(platform)
    receipt.steps_done.append("secrets")
    receipt.save(platform)


def import_bundle(path: Path, *, hermes: HermesCli, name: str | None, io: IO,
                  store_factory: Callable[[Path], SecretStore]) -> Receipt | None:
    """Import the bundle at PATH as a new profile. Returns its Receipt, or None when the user went Back or
    said no (nothing created). Raises BundleError (refused, nothing created), ValueError (bad NAME) or
    WriteError (failed midway, rolled back)."""
    if name is not None:
        validate_profile_name(name)
    with tempfile.TemporaryDirectory(prefix="trading-agent-import-") as tmp:
        work = Path(tmp)
        manifest, entries, risky, dropped = read_bundle(Path(path), work)
        for line in summary_lines(dataclasses.replace(manifest, contents=_contents(entries))):
            io.say(line)
        if dropped:
            io.say(_not_imported_line(dropped))
        if risky:
            io.say(RISKY_HEAD)
            for line in risky:
                io.say(f"- {line}")
            io.say(RUN_WARNING)
        else:
            io.say(IMPORT_TRUST)
        chosen = _free_name(name or manifest.profile_name, hermes, io)
        if chosen is None:
            io.say(IMPORT_NO)
            return None
        if not io.confirm(IMPORT_CONFIRM.format(name=chosen), default=False):
            io.say(IMPORT_NO)
            return None
        values = _ask_keys(manifest.env_names_needed, io)
        if hermes.profile_exists(chosen) or _command_exists(hermes, chosen):     # taken while answering?
            values.clear()
            raise BundleError(NAME_TAKEN_NOW.format(name=chosen))
        archive = work / "profile-out.tar.gz"
        archive.write_bytes(_build_profile_archive(chosen, entries))
        receipt = _install(chosen, manifest, archive, values, hermes, store_factory)
        values.clear()
    io.say(IMPORT_DONE.format(name=chosen))
    if dropped:
        io.say(_not_imported_line(dropped))
    skipped = [n for n in manifest.env_names_needed if n not in receipt.env_names_written]
    if skipped:
        io.say(FINISH_LATER_TITLE)
        for n in skipped:
            io.say(LATER_KEY.format(name=n))
    io.say(IMPORT_UNDO.format(name=chosen))
    return receipt


def _install(name: str, manifest: BundleManifest, archive: Path, values: dict[str, str], hermes: HermesCli,
             store_factory: Callable[[Path], SecretStore]) -> Receipt:
    pdir = hermes.profile_dir(name)
    receipt = Receipt(
        profile_name=name,
        ai_choice={"kind": manifest.ai_kind, "ref": ""} if manifest.ai_kind else None,
        broker=dict(manifest.broker) if manifest.broker else None,
        skills=list(manifest.libraries),
        run_id=_rand.token_hex(16),
    )
    plain = list(values.values())
    failure: str | None = None
    interrupt: BaseException | None = None
    try:
        _import_steps(name, archive, values, receipt, hermes, pdir, store_factory)
    except W._StepError as e:
        failure = str(e)
    except ForbiddenHermesArgs:
        failure = "internal error: a forbidden hermes argument was requested"
    except Exception as e:  # noqa: BLE001 - every failure after import must roll back
        failure = f"{type(e).__name__}: {W._tail(str(e), plain)}"
    except BaseException as e:  # Ctrl-C: roll back, then let it propagate
        interrupt = e
    if failure is None and interrupt is None:
        return receipt
    ran = copy.deepcopy(receipt)
    acted, problems = W._rollback_collect(receipt, hermes, store_factory, pdir)
    if interrupt is not None:
        raise interrupt
    step = ran.steps_done[-1] if ran.steps_done else "start"
    text = f"import failed after step '{step}': {failure}"
    if "install" in ran.steps_done:
        text += "; rolled back" if acted and not problems else (
            f"; rollback incomplete ({'; '.join(problems)})" if problems else "; nothing rolled back (ownership not proven)")
    raise W.WriteError(W._report(text, plain), ran) from None
