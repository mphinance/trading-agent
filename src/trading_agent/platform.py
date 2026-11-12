from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

_VAR_RE = re.compile(r"[A-Z][A-Z0-9_]*")


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class Platform(Protocol):
    def home(self) -> Path: ...
    def config_home(self) -> Path: ...
    def environ(self) -> Mapping[str, str]: ...
    def which(self, name: str) -> Path | None: ...
    def read_text(self, path: Path) -> str: ...
    def path_exists(self, path: Path) -> bool: ...
    def list_dir(self, path: Path) -> list[str]: ...
    def login_shell_has_var(self, name: str) -> bool: ...
    def login_shell_vars(self, names: Sequence[str]) -> frozenset[str]: ...
    def run(self, argv: Sequence[str], *, timeout: float,
            env: Mapping[str, str] | None = None,
            stdin: str | None = None) -> RunResult: ...


def shell_has_var(platform: Platform, name: str) -> bool:
    """True if a login shell has NAME set and non-empty. Never reads the value."""
    if not _VAR_RE.fullmatch(name):
        raise ValueError(f"invalid variable name: {name!r}")
    shell = platform.environ().get("SHELL") or "/bin/sh"
    script = f'[ -n "${{{name}+x}}" ] && [ -n "${{{name}}}" ] && echo 1'
    result = platform.run([shell, "-lc", script], timeout=10)
    return result.returncode == 0 and result.stdout.strip() == "1"


def shell_vars(platform: Platform, names: Sequence[str]) -> frozenset[str]:
    """Names (of those asked) that a login shell has set and non-empty.

    One shell spawn, one 5 s timeout. Names are validated, then written literally
    into the script, so only names can be printed, never values. A timeout,
    or OSError yields an empty set (a broken shell reads as absent). A nonzero exit
    does not discard names already printed: stdout is filtered to requested names.
    """
    wanted = list(dict.fromkeys(names))
    for name in wanted:
        if not _VAR_RE.fullmatch(name):
            raise ValueError(f"invalid variable name: {name!r}")
    if not wanted:
        return frozenset()
    shell = platform.environ().get("SHELL") or "/bin/sh"
    # Each probe is an if, and the script ends with `true`, so the exit status is 0
    # no matter which name is last. Names are validated above and written literally.
    script = "; ".join(f'if [ -n "${{{n}}}" ]; then echo {n}; fi' for n in wanted)
    result = platform.run([shell, "-lc", script + "; true"], timeout=5)
    if result.timed_out:
        return frozenset()
    seen = {line.strip() for line in result.stdout.splitlines()}
    return frozenset(seen & set(wanted))


class LinuxPlatform:
    def home(self) -> Path:
        return Path.home()

    def config_home(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return Path(xdg) if xdg else Path.home() / ".config"

    def environ(self) -> Mapping[str, str]:
        return os.environ

    def which(self, name: str) -> Path | None:
        # An empty or relative PATH entry means "the current directory": a planted binary there must
        # never be found, so only absolute entries are searched.
        entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e and os.path.isabs(e)]
        found = shutil.which(name, path=os.pathsep.join(entries)) if entries else None
        return Path(found) if found else None

    def read_text(self, path: Path) -> str:
        """Read a file as UTF-8. Raises OSError subclasses (FileNotFoundError, PermissionError)."""
        return Path(path).read_text(encoding="utf-8")

    def path_exists(self, path: Path) -> bool:
        return Path(path).exists()

    def list_dir(self, path: Path) -> list[str]:
        """Entry names (not paths) of a directory. Raises OSError subclasses."""
        return [e.name for e in Path(path).iterdir()]

    def login_shell_has_var(self, name: str) -> bool:
        return shell_has_var(self, name)

    def login_shell_vars(self, names: Sequence[str]) -> frozenset[str]:
        return shell_vars(self, names)

    def run(self, argv: Sequence[str], *, timeout: float,
            env: Mapping[str, str] | None = None,
            stdin: str | None = None) -> RunResult:
        try:
            p = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout,
                env=dict(env) if env is not None else None, input=stdin,
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return RunResult(-1, out, err, True)
        except OSError as e:
            return RunResult(127, "", str(e), False)
        return RunResult(p.returncode, p.stdout, p.stderr, False)
