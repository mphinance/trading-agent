from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from trading_agent.findings import Finding
from trading_agent.platform import Platform

_TRANSPORTS = {"stdio", "http", "sse", "streamable-http", "websocket", "ws"}


class McpParseError(Exception):
    """Content-free parse failure. Never carries any part of the file."""


@dataclass(frozen=True)
class McpEntry:
    """One MCP server definition with every value dropped. Names and a host only."""

    server: str
    transport: str
    command: str | None
    url_host: str | None
    env_names: tuple[str, ...]
    header_names: tuple[str, ...]


def _basename(command: object) -> str | None:
    """Executable token only: first token (quoted token if it opens with a quote),
    then its basename. Arguments, flags and anything after the first token are dropped."""
    if not isinstance(command, str):
        return None
    c = command.strip()
    if not c:
        return None
    if c[0] in "\"'":
        end = c.find(c[0], 1)
        token = c[1:end] if end != -1 else c[1:].split()[0] if c[1:].split() else ""
    else:
        token = c.split()[0]
    return token.strip().replace("\\", "/").rsplit("/", 1)[-1] or None


def _host(url: object) -> str | None:
    """Hostname only: no scheme, userinfo, port, path or query (any may hold a token)."""
    if not isinstance(url, str):
        return None
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


def _names(obj: object) -> tuple[str, ...]:
    return tuple(str(k) for k in obj) if isinstance(obj, dict) else ()


def _entry(name: str, raw: object) -> McpEntry | None:
    if not isinstance(raw, dict):
        return None
    command = _basename(raw.get("command"))
    host = _host(raw.get("url"))
    declared = raw.get("type", raw.get("transport"))
    if isinstance(declared, str) and declared.lower() in _TRANSPORTS:
        transport = declared.lower()
    elif host is not None:
        transport = "http"
    elif command is not None:
        transport = "stdio"
    else:
        transport = "unknown"
    return McpEntry(
        server=name, transport=transport, command=command, url_host=host,
        env_names=_names(raw.get("env")), header_names=_names(raw.get("headers")),
    )


def parse_mcp_config(text: str) -> list[McpEntry]:
    """Extract MCP server entries from a JSON config, keeping names only.

    Reads only top-level `mcpServers` and each `projects.<path>.mcpServers`; the rest
    of the document (accounts, history, tokens) is never copied. Raises McpParseError
    with a fixed message on bad JSON. json.JSONDecodeError carries the whole document in
    `.doc`, so it is never chained or re-raised.
    """
    failed = False
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        failed = True
        doc = None
    if failed:
        raise McpParseError("not valid JSON")
    entries: dict[str, McpEntry] = {}
    if isinstance(doc, dict):
        blocks = [doc.get("mcpServers")]
        projects = doc.get("projects")
        if isinstance(projects, dict):
            blocks += [p.get("mcpServers") for p in projects.values() if isinstance(p, dict)]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for name, raw in block.items():
                entry = _entry(str(name), raw)
                if entry is not None:
                    entries.setdefault(entry.server, entry)
    del doc  # raw parsed object (with every value) does not leave this function
    return list(entries.values())


@dataclass(frozen=True)
class _Source:
    key: str
    label: str
    path: Callable[[Platform], Path]
    fmt: str  # "json" or "toml"
    verified: bool  # False: location is UNVERIFIED, report absent, touch nothing


SOURCES: tuple[_Source, ...] = (
    _Source("claude-code", "Claude Code", lambda p: p.home() / ".claude.json", "json", True),
    _Source("claude-desktop", "Claude Desktop",
            lambda p: p.config_home() / "Claude" / "claude_desktop_config.json", "json", True),
    # UNVERIFIED locations (AUTODETECTION.md section 6): never read.
    _Source("cursor", "Cursor", lambda p: p.home() / ".cursor" / "mcp.json", "json", False),  # UNVERIFIED
    _Source("vscode", "VS Code",
            lambda p: p.config_home() / "Code" / "User" / "mcp.json", "json", False),  # UNVERIFIED
    _Source("codex", "Codex CLI", lambda p: p.home() / ".codex" / "config.toml", "toml", False),  # UNVERIFIED
)


def _status(src: _Source, what: str, state: str, fid: str | None = None,
            names: tuple[str, ...] = ()) -> Finding:
    return Finding(id=fid or f"mcp:{src.key}", kind="mcp-entry", what=what,
                   where=f"{src.label} MCP config", state=state, names=names,  # type: ignore[arg-type]
                   reusable="none")


def _scan(platform: Platform, src: _Source) -> list[Finding]:
    if not src.verified:
        return [_status(src, "config location not verified", "absent")]
    path = src.path(platform)
    probe_failed = False
    try:
        exists = platform.path_exists(path)
    except OSError:
        probe_failed, exists = True, False
    if probe_failed:
        return [_status(src, "config location could not be checked", "unreadable")]
    if not exists:
        return [_status(src, "no config file", "absent")]
    if src.fmt != "json":
        return [_status(src, "TOML config not supported yet", "unreadable")]
    read_failed = False
    text = ""
    try:
        text = platform.read_text(path)
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError (holds raw bytes)
        read_failed = True
    if read_failed:
        return [_status(src, "config file could not be read", "unreadable")]
    try:
        entries = parse_mcp_config(text)
    except McpParseError:
        return [_status(src, "config file is not valid JSON", "unreadable")]
    finally:
        del text
    if not entries:
        return [_status(src, "no MCP servers configured", "absent")]
    out = []
    for e in entries:
        where_bits = e.transport + (f" {e.url_host}" if e.url_host else "")
        out.append(_status(
            src, f"MCP server {e.server} ({where_bits})", "present",
            fid=f"mcp:{src.key}:{e.server}",
            names=(e.server, *e.env_names, *e.header_names),
        ))
    return out


def detect_mcp_entries(platform: Platform, *, enabled: bool = True) -> list[Finding]:
    """Existing MCP server entries in other tools. Read-only; names only, never values."""
    if not enabled:
        return []
    findings: list[Finding] = []
    for src in SOURCES:
        findings.extend(_scan(platform, src))
    return findings
