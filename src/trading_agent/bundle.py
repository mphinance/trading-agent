"""Export a profile to one bundle file (or a readable folder) and import a bundle as a new profile.

A bundle is a ``.tgz`` whose members are exactly ``bundle.json`` (names and labels, never a value) and
``profile.tar.gz`` (a filtered copy of Hermes' own ``profile export``). It never holds a key, ``.env``,
``auth.json``, a key or token file, chat history, logs or the ``.trading-agent-run`` ownership marker.
It is NOT encrypted.

Export (security first): Hermes drops ``.env`` and ``auth.json``; this module then refuses the whole export,
writing nothing, if any member is named like a credential (``.env*``, ``auth.json``, ``*.pem``, ``*.key``,
``token*``), keeps only ``SOUL.md``, ``config.yaml``, ``memories/`` and ``skills/``, and refuses again if any
kept byte looks like a secret (``redact_patterns``) or holds a value from the profile's own ``.env`` (values
of 8 or more characters, read in memory for this comparison only, never printed or stored).

Import (hostile input): every member of both archives is checked by name and type BEFORE anything is
written, under fixed caps; ``bundle.json`` is validated field by field and never trusted for a path; the
profile is created through ``hermes profile import`` with a receipt and the ownership marker made here, so
``trading-agent undo`` removes it and a failure midway rolls back. Keys are asked for by name, never reused
from the environment.
"""
from __future__ import annotations

import copy
import dataclasses
import gzip
import io
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
from trading_agent.secrets import SecretStore, check_secret, redact_patterns, scrub
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
                         "HERMES_HOME", "NODE_OPTIONS", "PERL5OPT", "RUBYOPT", "SSL_CERT_FILE", "SSL_CERT_DIR",
                         "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                         "NO_PROXY", "LANG"})
_DENY_PREFIXES = ("LD_", "DYLD_", "PYTHON", "XDG_", "GIT_", "UV_", "PIP_", "NPM_CONFIG", "LC_")

# ---------------------------------------------------------------- copy (no em or en dashes)

CONTAINS = "This contains: {items}."
CONTAINS_NOTHING = "This contains: an empty profile."
NOT_CONTAINED = "It does not contain: keys, sign-ins."
LEFT_HERE = "Chat history, logs and workspace files stay on this computer. The bundle is not encrypted."
IMPORT_TRUST = ("A bundle's config.yaml can start programs (MCP servers) when the agent runs. "
                "Import only a bundle you made or trust.")
IMPORT_CONFIRM = "Import it as a new profile called {name}?"
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
NO_PROFILE = "I found no profile called {name}."
EXPORT_FAILED = "Hermes could not export the profile ({detail}). Nothing was written."
OUT_EXISTS = "That file already exists. I never overwrite; pick a new name."
DIR_NOT_EMPTY = "That folder is not empty. I never overwrite; pick a new or empty folder."
NO_PARENT = "The folder to write into does not exist."


class BundleError(Exception):
    """One plain, value-free line. Nothing was written (export) or nothing was created (import)."""


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


def _looks_secret(data: bytes, values: list[str]) -> bool:
    text = data.decode("utf-8", errors="replace")
    if redact_patterns(text) != text:
        return True
    return bool(values) and scrub(text, values) != text


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
        if out.is_symlink() or not out.is_dir() or any(out.iterdir()):
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


def export(name: str, out: Path, *, hermes: HermesCli, receipt: Receipt | None, files: bool = False
           ) -> tuple[Path, BundleManifest]:
    """Export profile NAME to a bundle file OUT (or, with FILES, a readable folder OUT). Writes nothing at all
    unless every check passes."""
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
            kept = [e for e in entries if _kept(e[0])]
            for rel, is_dir, data in kept:
                if redact_patterns(rel) != rel or (not is_dir and _looks_secret(data, values)):
                    raise BundleError(SECRET_FILE.format(rel=_printable(redact_patterns(rel))))
            config = next((d for r, is_dir, d in kept if r == "config.yaml" and not is_dir), b"")
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
            if _looks_secret(pattern_view, []) or (values and scrub(manifest_bytes.decode(), values)
                                                    != manifest_bytes.decode()):
                raise BundleError(SECRET_FILE.format(rel=BUNDLE_JSON))
            inner = _build_profile_archive(name, kept)
            if files:
                _write_folder(out, name, inner, manifest_bytes)
            else:
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                    for member, data in ((BUNDLE_JSON, manifest_bytes), (PROFILE_ARCHIVE, inner)):
                        ti = tarfile.TarInfo(member)
                        ti.mode, ti.size, ti.mtime = 0o600, len(data), int(time.time())
                        tar.addfile(ti, io.BytesIO(data))
                _write_new_file(out, buf.getvalue())
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


def export_bundle(name: str, out: Path, *, hermes: HermesCli, receipt: Receipt | None) -> Path:
    return export(name, out, hermes=hermes, receipt=receipt)[0]


def export_files(name: str, out_dir: Path, *, hermes: HermesCli, receipt: Receipt | None) -> Path:
    return export(name, out_dir, hermes=hermes, receipt=receipt, files=True)[0]


# ---------------------------------------------------------------- import

def read_bundle(path: Path, work: Path) -> tuple[BundleManifest, list[tuple[str, bool, bytes]]]:
    """Check a bundle completely, writing only into the private WORK dir. Returns the validated manifest and
    the profile entries (relative paths). Raises BundleError before the caller creates anything."""
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
        if _credential_named(rel) or not _kept(rel):
            raise BundleError(NOT_ALLOWED)
    return manifest, entries


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
    res = hermes.profile_import(archive, name)
    if res.returncode != 0 or res.timed_out:
        if "already exists" not in (res.stdout or "") and pdir.is_dir() and not pdir.is_symlink():
            W._write_marker(pdir, receipt.run_id)     # a half-made dir we checked absent: claim it to roll back
            receipt.created_paths.append(str(pdir))
            receipt.steps_done.append("install")
        raise W._hermes_failure("import", res, plain)
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
        manifest, entries = read_bundle(Path(path), work)
        for line in summary_lines(dataclasses.replace(manifest, contents=_contents(entries))):
            io.say(line)
        io.say(IMPORT_TRUST)
        chosen = _free_name(name or manifest.profile_name, hermes, io)
        if chosen is None:
            io.say(IMPORT_NO)
            return None
        if not io.confirm(IMPORT_CONFIRM.format(name=chosen)):
            io.say(IMPORT_NO)
            return None
        values = _ask_keys(manifest.env_names_needed, io)
        archive = work / "profile-out.tar.gz"
        archive.write_bytes(_build_profile_archive(chosen, entries))
        receipt = _install(chosen, manifest, archive, values, hermes, store_factory)
        values.clear()
    io.say(IMPORT_DONE.format(name=chosen))
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
