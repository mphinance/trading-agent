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
from urllib.parse import quote, quote_plus, unquote
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
    """Replace every secret, and its dotenv, Python-repr, JSON and percent-encoded forms, with ``[hidden]``.

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
        # percent-encoded echoes (URLs, form bodies), upper and lower case hex
        for enc in (quote(secret, safe=""), quote(secret), quote_plus(secret)):
            forms.add(enc)
            forms.add(re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), enc))
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
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})")
# NAME=value or NAME: value. The name is bounded so a long word retried from its start stays linear;
# whether the name is sensitive is decided in the callback, and only the value is hidden.
_ENV_LINE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{0,127})([ \t]*[=:][ \t]*)(\S+)")
_ENV_NAME_VALUE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")   # names another NAME (key_env: MY_KEY)
_ENV_WORDS = ("KEY", "TOKEN", "SECRET", "PASSW", "PWD", "AUTH", "CREDENTIAL")
_PROSE_SAFE_WORDS = {"key", "keys", "token", "tokens", "secret", "secrets", "password", "passwd", "pwd", "auth",
                     "credential", "credentials", "apikey"}
# Bounded scheme: an unbounded scheme class retried from every start position is quadratic.
# The userinfo class stops at the next "/" so it stays linear without a bound (a bound would fail open).
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,31}://)[^/\s@]+@")
# One bounded-name pattern for query parameters; the keyword and long-value rules run in the callback.
# (Nested unbounded classes around a keyword alternation were quadratic on "?" + "token" * n.)
_URL_PARAM = re.compile(r"([?&#;])([A-Za-z0-9_.%\-]+)=([^&;\s#\"']+)")
# Second pass, value class without "?": finds a sensitive param NESTED inside another param's value
# (redirect_uri=https://app/cb?access_token=...). Two linear passes, never recursion.
_URL_PARAM_NESTED = re.compile(r"([?&#;])([A-Za-z0-9_.%\-]+)=([^?&;\s#\"']+)")
_SENSITIVE_NAME = ("token", "key", "secret", "sig", "auth", "passw", "pwd", "credential", "access")
_TOKENISH = re.compile(r"[A-Za-z0-9_.~+/\-]{16,}")
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


def _param_sub(m: re.Match) -> str:
    sep, name, value = m.group(1), m.group(2), m.group(3)
    lowered = unquote(name).lower()
    if any(k in lowered for k in _SENSITIVE_NAME) or _TOKENISH.fullmatch(value):
        return f"{sep}{name}={HIDDEN}"
    return m.group(0)


def _bearer_sub(m: re.Match) -> str:
    scheme, value = m.group(1), m.group(2)
    # "basic" is also an English word: a plain lower-case or Title-case word after it is prose, not a credential
    if scheme.lower() == "basic" and value.isalpha() and (value.islower() or value.istitle()):
        return m.group(0)
    return f"{scheme} {HIDDEN}"


def _env_name_sensitive(name: str) -> bool:
    if name.upper() == name:
        return any(w in name.upper() for w in _ENV_WORDS)
    # mixed or lower case: an identifier with a sensitive segment (api_key, access_token), or the bare word
    segs = name.lower().split("_")
    return any(seg in _PROSE_SAFE_WORDS for seg in segs) and ("_" in name or name.lower() not in ("key", "keys"))


def _env_value_plain(value: str) -> bool:
    """Not a credential: a ${NAME} reference, an already hidden value, a number, a word like true, or short."""
    return (len(value) < 8 or value.startswith(("${", "[")) or value.isdigit() or "://" in value
            or _ENV_NAME_VALUE.fullmatch(value) is not None or value.lower() in ("true", "false", "none", "null"))


def _env_sub(m: re.Match) -> str:
    if not _env_name_sensitive(m.group(1)) or _env_value_plain(m.group(3)):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{HIDDEN}"


def redact_patterns(text: str, *, env_lines: bool = True) -> str:
    """Mask things shaped like credentials: sk-style keys, ``Bearer``/``Basic <token>``, hex or
    base64 runs of 32+ chars, URL userinfo, token-looking URL query or fragment values, and the value of
    a ``NAME=value`` or ``NAME: value`` line whose NAME looks like a key, token, secret or password.
    ``env_lines=False`` skips that last rule: the export soft check has stricter config-specific rules of its own."""
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{HIDDEN}@", text)
    text = _URL_PARAM.sub(_param_sub, text)
    text = _URL_PARAM_NESTED.sub(_param_sub, text)
    text = _BEARER.sub(_bearer_sub, text)
    if env_lines:
        text = _ENV_LINE.sub(_env_sub, text)
    text = _SK_KEY.sub(HIDDEN, text)
    text = _HEX_RUN.sub(HIDDEN, text)
    text = _B64_RUN.sub(_b64_sub, text)
    return text
