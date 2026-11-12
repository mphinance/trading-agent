from __future__ import annotations

import socket
from typing import Callable, Mapping

from trading_agent.findings import Finding

LOOPBACK = "127.0.0.1"
DEFAULT_PORTS: dict[str, int] = {
    "ollama": 11434,
    "lm-studio": 1234,
    "ibkr-gateway": 7497,
    "moomoo-opend": 11111,
    "ninjatrader": 7890,
}
_AI_LOCAL = frozenset({"ollama", "lm-studio"})
_TIMEOUT = 0.5


def tcp_connect(host: str, port: int, timeout: float) -> bool:
    """True if something accepts a TCP connection. Loopback only, sends nothing."""
    if host != LOOPBACK:
        return False
    try:
        with socket.create_connection((LOOPBACK, port), timeout=timeout):
            return True
    except (OSError, OverflowError, ValueError):
        return False


def probe_ports(ports: Mapping[str, int] = DEFAULT_PORTS,
                connect: Callable[[str, int, float], bool] = tcp_connect) -> list[Finding]:
    findings: list[Finding] = []
    for name, port in ports.items():
        kind = "ai-local" if name in _AI_LOCAL else "gateway"
        try:
            valid = isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536
            is_open = bool(connect(LOOPBACK, port, _TIMEOUT)) if valid else False
        except Exception:  # a failing probe reads as closed, never aborts the rest
            is_open = False
        where = f"{LOOPBACK}:{port}"
        findings.append(Finding(
            id=f"{kind}:{name}", kind=kind,
            what=f"Something is listening on the usual {name} port" if is_open
            else f"Nothing listening on the usual {name} port",
            where=where, state="present" if is_open else "absent",
            names=(name,), reusable="none",
        ))
    return findings
