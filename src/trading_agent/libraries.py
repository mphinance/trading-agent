"""Library catalog: pointer entries for optional skill and tool libraries.

Pointers only; nothing is vendored. Data lives in package data under data/libraries/.
Errors name the library id and field, never file content.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml

_FILES = ("skills.yaml", "tools.yaml")
_KINDS = ("skills", "tool")
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_REQUIRED = ("id", "kind", "name", "source_url", "license", "counts", "needs_keys", "attribution", "listed")
_OPTIONAL = ("unresolved", "unverified")


class LibraryError(Exception):
    """Invalid library data. The message names the library id and field only."""


@dataclass(frozen=True)
class Library:
    id: str
    kind: Literal["skills", "tool"]
    name: str
    source_url: str
    license: str
    counts: Mapping[str, int]
    needs_keys: tuple[str, ...]
    attribution: str
    listed: bool
    unresolved: str = ""
    unverified: tuple[str, ...] = field(default_factory=tuple)


def _fail(lib_id: str, fname: str, what: str) -> LibraryError:
    return LibraryError(f"library {lib_id!r}: field {fname!r}: {what}")


def _str(lib_id: str, fname: str, v: object) -> str:
    if not isinstance(v, str):
        raise _fail(lib_id, fname, "must be a string")
    return v


def _build(raw: object, where: str) -> Library:
    if not isinstance(raw, dict):
        raise LibraryError(f"{where}: entry must be a mapping")
    lib_id = raw.get("id")
    if not isinstance(lib_id, str) or not _ID.fullmatch(lib_id):
        shown = repr(lib_id)[:40] if isinstance(lib_id, str) else type(lib_id).__name__
        raise LibraryError(f"{where}: field 'id': invalid ({shown})")
    for k in raw:
        if not isinstance(k, str) or k not in _REQUIRED + _OPTIONAL:
            raise _fail(lib_id, str(k) if isinstance(k, str) else "<non-string>", "unknown field")
    for k in _REQUIRED:
        if k not in raw:
            raise _fail(lib_id, k, "missing")
    kind = _str(lib_id, "kind", raw["kind"])
    if kind not in _KINDS:
        raise _fail(lib_id, "kind", "unknown kind")
    name = _str(lib_id, "name", raw["name"])
    url = _str(lib_id, "source_url", raw["source_url"])
    lic = _str(lib_id, "license", raw["license"])
    attribution = _str(lib_id, "attribution", raw["attribution"])
    listed = raw["listed"]
    if not isinstance(listed, bool):
        raise _fail(lib_id, "listed", "must be a boolean")
    counts = raw["counts"]
    if not isinstance(counts, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in counts.items()
    ):
        raise _fail(lib_id, "counts", "must be a mapping of names to integers >= 0")
    keys = raw["needs_keys"]
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise _fail(lib_id, "needs_keys", "must be a list of strings")
    if not all(_ENV_NAME.fullmatch(k) for k in keys):
        raise _fail(lib_id, "needs_keys", "entries must be env var names")
    unresolved = _str(lib_id, "unresolved", raw.get("unresolved", ""))
    unv = raw.get("unverified", [])
    if not isinstance(unv, list) or not all(isinstance(u, str) for u in unv):
        raise _fail(lib_id, "unverified", "must be a list of strings")
    if listed and not lic.strip():
        raise _fail(lib_id, "license", "a listed library needs a license")
    if listed and not url.strip():
        raise _fail(lib_id, "source_url", "a listed library needs a source_url")
    if listed:
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or not parts.netloc:
            raise _fail(lib_id, "source_url", "a listed library needs an https URL")
    return Library(lib_id, kind, name, url, lic, dict(counts), tuple(keys), attribution, listed,  # type: ignore[arg-type]
                   unresolved, tuple(unv))


def _read(name: str, root: Path | None) -> str:
    try:
        if root is None:
            return (resources.files("trading_agent") / "data" / "libraries" / name).read_text(encoding="utf-8")
        return (root / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise LibraryError(f"data file {name}: unreadable ({type(e).__name__})") from None


def load_libraries(root: Path | None = None) -> list[Library]:
    """Load and strictly validate every library entry. root=None reads the packaged data."""
    out: list[Library] = []
    seen: set[str] = set()
    for fname in _FILES:
        text = _read(fname, root)
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            raise LibraryError(f"data file {fname}: not valid YAML") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("libraries"), list):
            raise LibraryError(f"data file {fname}: expected a 'libraries' list")
        for raw in doc["libraries"]:
            lib = _build(raw, fname)
            if lib.id in seen:
                raise LibraryError(f"library {lib.id!r}: field 'id': duplicate")
            seen.add(lib.id)
            out.append(lib)
    return out


def listed_only(libs: Sequence[Library]) -> list[Library]:
    """Libraries the wizard may offer: unlisted entries are excluded."""
    return [lib for lib in libs if lib.listed]


def keys_needed(selected: Sequence[Library]) -> tuple[str, ...]:
    """Env var names the picked libraries need: sorted, de-duplicated, names only."""
    return tuple(sorted({k for lib in selected for k in lib.needs_keys}))
