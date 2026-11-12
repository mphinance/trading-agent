"""The only place a ``hermes`` process is spawned.

Rules enforced here (FIRST_RUN.md 5.3): never ``--clone``, ``--clone-all``, ``--clone-from``,
never ``--env``, never ``auth list`` / ``auth status``, never ``import-agent``. The child gets an
allow-listed environment (no provider keys), ``HERMES_HOME`` only when the platform has it, and a
timeout on every call. One process at a time (calls are synchronous).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from trading_agent.platform import Platform, RunResult

RESERVED_NAMES = frozenset({"hermes", "default", "test", "tmp", "root", "sudo"})
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")

INSTALL_TIMEOUT = 120
CALL_TIMEOUT = 30

_FORBIDDEN_FLAGS = ("--clone", "--clone-all", "--clone-from", "--env")
_FORBIDDEN_SEQUENCES = (("auth", "list"), ("auth", "status"))
_FORBIDDEN_WORDS = ("import-agent",)
# What a hermes child may inherit. Provider keys and the rest of the environment are not forwarded.
_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
              "XDG_CACHE_HOME", "HERMES_HOME")


class ForbiddenHermesArgs(Exception):
    """The argv contains something the product never does. Carries no argument text."""


class HermesUnavailable(RuntimeError):
    """``hermes --version`` did not work. Value-free."""


class HermesNotBootstrapped(HermesUnavailable):
    """Hermes is installed but its data root has no dependency environment yet (T0: "run
    ``hermes pm repair``"). The first ``profile install`` bootstraps it, so this is not a failure."""


_NOT_BOOTSTRAPPED_MARKERS = ("pm repair", "no dependency environment")


def validate_profile_name(name: str) -> None:
    """Raises a value-free ValueError (the name is not echoed) unless NAME is safe and not reserved."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("invalid profile name (lowercase letters, digits, hyphen, underscore; "
                         "1 to 32 chars; must start with a letter)")
    if name in RESERVED_NAMES:
        raise ValueError("reserved profile name; pick another")


def check_args(args: Sequence[str]) -> None:
    tokens = [str(a) for a in args]
    for tok in tokens:
        if tok in _FORBIDDEN_FLAGS or tok.startswith(("--clone=", "--clone-all=", "--clone-from=", "--env=")):
            raise ForbiddenHermesArgs("forbidden hermes flag")
        if tok in _FORBIDDEN_WORDS:
            raise ForbiddenHermesArgs("forbidden hermes subcommand")
    for a, b in zip(tokens, tokens[1:]):
        if (a, b) in _FORBIDDEN_SEQUENCES:
            raise ForbiddenHermesArgs("forbidden hermes subcommand")


class HermesCli:
    def __init__(self, platform: Platform, binary: str = "hermes") -> None:
        self.platform = platform
        self._binary = binary

    # ---- the single spawn point

    def run(self, args: Sequence[str], *, timeout: float) -> RunResult:
        """Run ``hermes ARGS``. ARGS excludes the binary (a leading binary name is tolerated for checking)."""
        args = [str(a) for a in args]
        check_args(args)
        exe = self.platform.which(self._binary)
        argv = [str(exe) if exe else self._binary, *args]
        environ = self.platform.environ()
        env = {k: environ[k] for k in _ENV_ALLOW if k in environ}
        return self.platform.run(argv, timeout=timeout, env=env)

    # ---- locations

    def profile_dir(self, name: str) -> Path:
        validate_profile_name(name)
        root = self.platform.environ().get("HERMES_HOME")
        base = Path(root) if root else self.platform.home() / ".hermes"
        return base / "profiles" / name

    # ---- queries

    def version(self) -> str:
        res = self.run(["--version"], timeout=CALL_TIMEOUT)
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        if res.timed_out:
            raise HermesUnavailable("hermes --version failed")
        text = f"{res.stdout}\n{res.stderr}".lower()
        if any(m in text for m in _NOT_BOOTSTRAPPED_MARKERS):
            raise HermesNotBootstrapped("hermes found, data root not initialized yet")
        if res.returncode != 0 or not lines:
            raise HermesUnavailable("hermes --version failed")
        return lines[0]

    def profile_exists(self, name: str) -> bool:
        """True if the profile directory exists OR ``hermes profile list`` names it.

        Both signals are consulted and either one refuses, so a naming collision is never overwritten."""
        if self.profile_dir(name).exists():
            return True
        res = self.run(["profile", "list"], timeout=CALL_TIMEOUT)
        if res.returncode != 0 or res.timed_out:
            return False
        for line in res.stdout.splitlines():
            token = line.lstrip(" \t◆*✓>").split(None, 1)
            if token and token[0] == name:
                return True
        return False

    # ---- mutations (each is one hermes process)

    def profile_install(self, source: Path, name: str) -> RunResult:
        validate_profile_name(name)
        return self.run(["profile", "install", str(source), "--name", name, "-y"], timeout=INSTALL_TIMEOUT)

    def profile_update(self, name: str) -> RunResult:
        validate_profile_name(name)
        return self.run(["profile", "update", name, "-y"], timeout=CALL_TIMEOUT)

    def profile_delete(self, name: str) -> RunResult:
        validate_profile_name(name)
        return self.run(["profile", "delete", name, "-y"], timeout=CALL_TIMEOUT)

    def config_set(self, profile: str, key: str, value: str) -> RunResult:
        """``hermes -p PROFILE config set KEY VALUE``. VALUE goes on the command line: never a secret."""
        validate_profile_name(profile)
        if not re.fullmatch(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*", key):
            raise ValueError("invalid config key")
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("invalid config value")
        return self.run(["-p", profile, "config", "set", key, value], timeout=CALL_TIMEOUT)
