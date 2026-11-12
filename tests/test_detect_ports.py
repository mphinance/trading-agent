from __future__ import annotations

from trading_agent.detect.ports import DEFAULT_PORTS, probe_ports, tcp_connect


def test_ports_connect_only_localhost():
    hosts = []

    def connect(host, port, timeout):
        hosts.append(host)
        return False

    probe_ports(DEFAULT_PORTS, connect)
    assert len(hosts) == len(DEFAULT_PORTS)
    assert set(hosts) == {"127.0.0.1"}


def test_closed_port_is_absent_and_open_is_present():
    findings = probe_ports(DEFAULT_PORTS, lambda h, p, t: p == 11434)
    by = {f.names[0]: f for f in findings}
    assert by["ollama"].state == "present"
    assert all(f.state == "absent" for n, f in by.items() if n != "ollama")


def test_kinds_and_defaults():
    by = {f.names[0]: f for f in probe_ports(DEFAULT_PORTS, lambda h, p, t: False)}
    assert DEFAULT_PORTS == {"ollama": 11434, "lm-studio": 1234, "ibkr-gateway": 7497,
                             "moomoo-opend": 11111, "ninjatrader": 7890}
    assert by["ollama"].kind == by["lm-studio"].kind == "ai-local"
    assert {by[n].kind for n in ("ibkr-gateway", "moomoo-opend", "ninjatrader")} == {"gateway"}


def test_failing_connect_is_isolated():
    def connect(host, port, timeout):
        if port == 1234:
            raise RuntimeError("boom")
        return True

    by = {f.names[0]: f for f in probe_ports(DEFAULT_PORTS, connect)}
    assert by["lm-studio"].state == "absent"
    assert by["ollama"].state == "present"


def test_tcp_connect_refuses_other_hosts():
    assert tcp_connect("192.0.2.1", 80, 0.1) is False
