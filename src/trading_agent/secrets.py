"""Secret storage for the new Hermes profile, and output scrubbing.

Hermes reads the profile ``.env`` with python-dotenv's ``DotEnv(...).parse()``
(``interpolate=False``) and then expands ``${VAR}`` / ``${VAR:-default}`` itself
(hermes_cli/env_loader.py, ``_parse_dotenv_file`` and ``_resolve_dotenv_assignments``).
So this module writes lines that parser reads back byte for byte:

* a value made only of ``_SAFE_BARE`` characters is written bare;
* anything else is double quoted, with ``\\``, ``"``, newline, CR, tab, BEL,
  backspace, form feed and vertical tab written as the escapes dotenv decodes.
  Multi-line values therefore stay on ONE physical line;
* a value containing ``${...}`` is rejected: the loader's interpolation has no
  escape, so it could never round-trip.

KeyringStore is deliberately not implemented in v1: Hermes core does not read
provider keys from the OS keyring, so a secret stored there would silently break
the broker. Keyring support is a documented follow-up behind the same
SecretStore Protocol.

No value is ever logged, printed, stored on an object, or put in an exception.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _stdlib_secrets
from pathlib import Path
from typing import Iterable, Protocol

from trading_agent.platform import Platform

HIDDEN = "[hidden]"

_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SAFE_BARE = re.compile(r"[A-Za-z0-9_./:@+,%^~-]+")
# Same shape python-dotenv's variables._posix_variable expands (superset: any ${...}).
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\v": "\\v",
}


class SecretStore(Protocol):
    def set(self, name: str, value: str) -> None: ...
    def exists(self, name: str) -> bool: ...
    def remove(self, name: str) -> None: ...


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("invalid secret name (expected [A-Z_][A-Z0-9_]*)")


def _check_value(name: str, value: str) -> None:
    """Raises ValueError naming only NAME, never the value."""
    if not isinstance(value, str):
        raise ValueError(f"secret {name} must be a string")
    if "\x00" in value:
        raise ValueError(f"secret {name} contains a NUL byte")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"secret {name} is not valid UTF-8 text") from None
    if _INTERPOLATION.search(value):
        raise ValueError(f"secret {name} contains '${{...}}', which the Hermes .env loader would expand")


def _escape(value: str) -> str:
    return "".join(_ESCAPES.get(c, c) for c in value)


def encode_value(value: str) -> str:
    """Dotenv text for VALUE (bare or double quoted), always one physical line."""
    if value and _SAFE_BARE.fullmatch(value):
        return value
    return f'"{_escape(value)}"'


def _line_name(line: str) -> str | None:
    """Name a line assigns, matching Hermes' ``_env_line_defines_key`` shapes:
    ``NAME=v``, ``export NAME=v``, ``NAME = v``. Comments and blanks give None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    key, sep, _ = stripped.partition("=")
    if not sep:
        return None
    return key.strip()


class EnvFileStore:
    """Dotenv file store. Holds only the path; every operation re-reads the file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def __repr__(self) -> str:
        return f"EnvFileStore({str(self._path)!r})"

    __str__ = __repr__

    def _read_lines(self) -> list[str] | None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        text = raw.decode("utf-8", errors="surrogateescape")
        lines = text.split("\n")
        # split leaves a trailing "" when the file ends with a newline
        tail = lines.pop()
        out = [line + "\n" for line in lines]
        if tail:
            out.append(tail)
        return out

    def _write_lines(self, lines: list[str]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = directory / f".{self._path.name}.{_stdlib_secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write("".join(lines).encode("utf-8", errors="surrogateescape"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def set(self, name: str, value: str) -> None:
        _check_name(name)
        _check_value(name, value)
        new_line = f"{name}={encode_value(value)}\n"
        lines = self._read_lines() or []
        out: list[str] = []
        placed = False
        for line in lines:
            if _line_name(line) == name:
                # dotenv: last duplicate wins, so keep exactly one, in the first slot
                if not placed:
                    out.append(new_line)
                    placed = True
                continue
            out.append(line)
        if not placed:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(new_line)
        self._write_lines(out)

    def exists(self, name: str) -> bool:
        _check_name(name)
        lines = self._read_lines()
        if lines is None:
            return False
        return any(_line_name(line) == name for line in lines)

    def remove(self, name: str) -> None:
        _check_name(name)
        lines = self._read_lines()
        if lines is None:
            return
        kept = [line for line in lines if _line_name(line) != name]
        if len(kept) != len(lines):
            self._write_lines(kept)


def choose_store(platform: Platform, profile_dir: Path) -> SecretStore:
    """The store for a new profile: its ``.env``.

    A KeyringStore is a documented follow-up behind the same SecretStore Protocol; it is
    not offered in v1 because Hermes core does not read provider keys from the OS
    keyring (injection unverified), so a keyed-in secret would silently break the broker.
    """
    return EnvFileStore(Path(profile_dir) / ".env")


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret, and its dotenv, Python-repr and JSON escaped forms, with ``[hidden]``.

    One pass over TEXT with longest-first alternation: overlapping and substring secrets
    resolve to the longest match, secrets are escaped (never used as patterns), and
    replacement output is never rescanned (a secret like "hid" cannot eat "[hidden]").
    """
    forms: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        escaped = _escape(secret)
        forms.update((secret, escaped, f'"{escaped}"'))
        # forms a Python or JSON dump of the value would show in a traceback or log line
        forms.add(repr(secret)[1:-1])
        forms.add(json.dumps(secret)[1:-1])
        forms.add(json.dumps(secret, ensure_ascii=False)[1:-1])
        forms.discard("")
    if not forms:
        return text
    pattern = re.compile("|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)))
    return pattern.sub(HIDDEN, text)


def check_secret(name: str, value: str) -> None:
    """Validate a NAME and VALUE pair the way EnvFileStore.set does, without writing.

    Raises ValueError carrying the NAME only, never the value."""
    _check_name(name)
    _check_value(name, value)


# ---- pattern based redaction -------------------------------------------------
# scrub() removes secrets we KNOW. redact_patterns() is the second net for text that
# may carry a secret we do not know (a tool echoing a key the user typed elsewhere).
# Deliberately small and biased toward over-redacting in error reports.

_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{7,}")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{8,}")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@]+@")
_URL_SENSITIVE_PARAM = re.compile(
    r"(?i)([?&][A-Za-z0-9_.\-]*(?:token|key|secret|sig|signature|auth|passw|pwd|credential|access)"
    r"[A-Za-z0-9_.\-]*=)[^&\s#\"']+")
_URL_LONG_VALUE = re.compile(r"([?&][A-Za-z0-9_.\-]+=)([A-Za-z0-9_.~+/\-]{16,})(?=$|[&\s#\"'])")
_HEX_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])")
# '/' is not in this alphabet, so file paths never form one long run. A real base64 secret
# containing '/' is cut into pieces; a piece of 32+ chars is still redacted.
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+_\-=])[A-Za-z0-9+_\-]{32,}={0,2}(?![A-Za-z0-9+_\-=])")


def _b64_sub(m: re.Match) -> str:
    run = m.group(0)
    # needs a digit and a letter: long plain words and snake_case identifiers stay readable
    if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
        return HIDDEN
    return run


def redact_patterns(text: str) -> str:
    """Mask things shaped like credentials: sk-style keys, ``Bearer <token>``, hex or
    base64 runs of 32+ chars, URL userinfo, and token-looking URL query values."""
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{HIDDEN}@", text)
    text = _URL_SENSITIVE_PARAM.sub(lambda m: f"{m.group(1)}{HIDDEN}", text)
    text = _URL_LONG_VALUE.sub(lambda m: f"{m.group(1)}{HIDDEN}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {HIDDEN}", text)
    text = _SK_KEY.sub(HIDDEN, text)
    text = _HEX_RUN.sub(HIDDEN, text)
    text = _B64_RUN.sub(_b64_sub, text)
    return text
