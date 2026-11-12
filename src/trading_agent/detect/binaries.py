from __future__ import annotations

import re
from pathlib import Path

from trading_agent.findings import Finding
from trading_agent.platform import Platform

# Fixed allow-list. Names are written into a shell script, so nothing else is ever accepted.
AI_CLIS: tuple[str, ...] = ("claude", "codex", "gemini", "agy")

_VERSION_TIMEOUT = 3
_LOOKUP_TIMEOUT = 5
_VERSION_MAX = 40
_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def known_binary_dirs(platform: Platform) -> list[Path]:
    """Plain (non-glob) directories where user-installed CLIs usually live.

    FOLLOW-UP: Windows locations (npm global, %LOCALAPPDATA% shims, .exe/.cmd suffixes)
    are omitted in v1. Linux first; everything root-relative goes through platform.home().
    """
    home = platform.home()
    return [
        home / ".local" / "bin",
        home / ".volta" / "bin",
        home / ".asdf" / "shims",
        Path("/opt/homebrew/bin"),
    ]


def _nvm_bin_dirs(platform: Platform) -> list[Path]:
    """Expand ~/.nvm/versions/node/*/bin. A listing failure means no nvm, not an error."""
    root = platform.home() / ".nvm" / "versions" / "node"
    try:
        names = sorted(platform.list_dir(root))
    except OSError:
        return []
    return [root / n / "bin" for n in names]


def _search_dirs(platform: Platform) -> list[Path]:
    dirs = known_binary_dirs(platform)
    # Keep the documented order: .local/bin, nvm globs, volta, asdf, homebrew.
    return dirs[:1] + _nvm_bin_dirs(platform) + dirs[1:]


def _exists(platform: Platform, path: Path) -> bool:
    try:
        return platform.path_exists(path)
    except OSError:
        return False


def _login_shell_lookup(platform: Platform, name: str) -> Path | None:
    """`command -v NAME` in a login shell. Its exit status is ignored (nonzero when missing);
    stdout is filtered to a single absolute path."""
    if name not in AI_CLIS:
        raise ValueError(f"not an allowed CLI name: {name!r}")
    try:
        shell = platform.environ().get("SHELL") or "/bin/sh"
        result = platform.run([shell, "-lc", f"command -v {name}"], timeout=_LOOKUP_TIMEOUT)
    except OSError:
        return None
    if result.timed_out:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("/") and "\x00" not in line:
            return Path(line)
    return None


def _locate(platform: Platform, name: str, dirs: list[Path]) -> Path | None:
    found = _login_shell_lookup(platform, name)
    if found is not None:
        return found
    try:
        on_path = platform.which(name)
    except OSError:
        on_path = None
    if on_path is not None:
        return on_path
    for d in dirs:
        candidate = d / name
        if _exists(platform, candidate):
            return candidate
    return None


def _version(platform: Platform, path: Path) -> str | None:
    """First line of `--version`, printable ASCII only, capped. Empty environment, 3 s."""
    try:
        result = platform.run([str(path), "--version"], timeout=_VERSION_TIMEOUT, env={})
    except OSError:
        return None
    if result.timed_out or result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    first = _PRINTABLE.sub("", lines[0]).strip() if lines else ""
    return first[:_VERSION_MAX] or None


def detect_ai_clis(platform: Platform) -> list[Finding]:
    dirs = _search_dirs(platform)
    findings: list[Finding] = []
    for name in AI_CLIS:
        try:
            path = _locate(platform, name, dirs)
            version = _version(platform, path) if path is not None else None
        except Exception:  # one CLI failing never aborts the rest
            path, version = None, None
        if path is None:
            looked = ", ".join(["login shell", "PATH", *(str(d) for d in dirs)])
            findings.append(Finding(
                id=f"cli:{name}", kind="ai-cli", what=f"{name} CLI (not found)",
                where=f"Looked in: {looked}", state="absent",
                names=(name,), reusable="reference-only",
            ))
            continue
        suffix = f" {version}" if version else ""
        findings.append(Finding(
            id=f"cli:{name}", kind="ai-cli", what=f"{name} CLI{suffix} (found, not tested)",
            where=str(path), state="present", names=(name,), reusable="reference-only",
        ))
    return findings
