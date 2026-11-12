from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fakes import FakePlatform
from trading_agent.detect import mcp_files
from trading_agent.detect.mcp_files import McpParseError, detect_mcp_entries, parse_mcp_config

FIX = Path(__file__).parent / "fixtures" / "mcp"
ALLOWED = {"read_text", "path_exists", "home", "config_home", "environ"}


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def plat(tmp_path, **files):
    paths = {
        "code": tmp_path / ".claude.json",
        "desktop": tmp_path / ".config" / "Claude" / "claude_desktop_config.json",
    }
    return FakePlatform(tmp_path, files={paths[k]: v for k, v in files.items()})


def test_values_dropped_at_parse_boundary():
    entries = parse_mcp_config(fx("claude_code.json"))
    assert {e.server for e in entries} == {"alpaca", "remote", "projlocal"}
    for e in entries:
        assert "sk-LEAK" not in repr(e)
        assert "sk-LEAK" not in str(e)
        for v in vars(e).values():
            assert "sk-LEAK" not in repr(v)
    by = {e.server: e for e in entries}
    assert by["alpaca"].env_names == ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    assert by["alpaca"].command == "uvx"
    assert by["alpaca"].transport == "stdio"
    assert by["remote"].header_names == ("Authorization",)
    assert by["projlocal"].env_names == ("PROJ_TOKEN",)


def test_url_host_only():
    e = {x.server: x for x in parse_mcp_config(fx("claude_code.json"))}["remote"]
    assert e.url_host == "mcp.example.invalid"
    assert e.transport == "http"


def test_unrelated_account_data_ignored():
    text = fx("claude_code.json")
    assert "someone@example.invalid" in text
    for e in parse_mcp_config(text):
        assert "someone" not in repr(e) and "secret-project-path" not in repr(e)


def test_findings_hold_no_values(tmp_path):
    p = plat(tmp_path, code=fx("claude_code.json"), desktop=fx("claude_desktop.json"))
    findings = detect_mcp_entries(p)
    present = {f.id: f for f in findings if f.state == "present"}
    assert "mcp:claude-code:alpaca" in present
    assert present["mcp:claude-code:alpaca"].names == (
        "alpaca", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    assert "mcp:claude-desktop:tradier" in present
    for f in findings:
        assert "sk-LEAK" not in repr(f)
        assert "secret-project-path" not in repr(f)
        assert f.kind == "mcp-entry"


def test_malformed_file_is_unreadable_not_an_error(tmp_path):
    p = plat(tmp_path, code=fx("bad.json"))
    findings = detect_mcp_entries(p)
    f = next(x for x in findings if x.id == "mcp:claude-code")
    assert f.state == "unreadable"
    assert "alpaca" not in repr(f) and "ALPACA" not in repr(f) and "sk-LEAK" not in repr(f)


def test_error_messages_contain_no_values(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    bad = '{"mcpServers": {"x": {"env": {"K": "sk-LEAK"}}'  # truncated
    with pytest.raises(McpParseError) as ei:
        parse_mcp_config(bad)
    assert "sk-LEAK" not in str(ei.value) and "sk-LEAK" not in repr(ei.value)
    # no chained exception that could carry the document
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    findings = detect_mcp_entries(plat(tmp_path, code=bad, desktop="sk-LEAK not json"))
    assert all("sk-LEAK" not in str(f) for f in findings)
    assert "sk-LEAK" not in caplog.text
    assert all("sk-LEAK" not in r.getMessage() and r.exc_info is None for r in caplog.records)


def test_deeply_nested_json_is_unreadable(tmp_path):
    p = plat(tmp_path, code="[" * 100000)
    f = next(x for x in detect_mcp_entries(p) if x.id == "mcp:claude-code")
    assert f.state == "unreadable"


def test_unreadable_when_read_fails(tmp_path):
    class Denied(FakePlatform):
        def read_text(self, path):
            self.calls.append("read_text")
            raise PermissionError("sk-LEAK denied")

    p = Denied(tmp_path, files={tmp_path / ".claude.json": "{}"})
    f = next(x for x in detect_mcp_entries(p) if x.id == "mcp:claude-code")
    assert f.state == "unreadable" and "sk-LEAK" not in repr(f)


def test_toml_reported_unreadable_not_supported(tmp_path, monkeypatch):
    src = mcp_files._Source("codex", "Codex CLI", lambda p: p.home() / "c.toml", "toml", True)
    monkeypatch.setattr(mcp_files, "SOURCES", (src,))
    p = FakePlatform(tmp_path, files={tmp_path / "c.toml": "[mcp_servers.x]\nenv={K='sk-LEAK'}"})
    (f,) = detect_mcp_entries(p)
    assert f.state == "unreadable" and "not supported yet" in f.what
    assert "read_text" not in p.calls


def test_missing_files_are_absent(tmp_path):
    findings = detect_mcp_entries(FakePlatform(tmp_path))
    assert findings and all(f.state == "absent" for f in findings)


def test_unverified_locations_absent_and_untouched(tmp_path):
    cursor = tmp_path / ".cursor" / "mcp.json"
    p = FakePlatform(tmp_path, files={cursor: fx("cursor.json")})
    findings = detect_mcp_entries(p)
    f = next(x for x in findings if x.id == "mcp:cursor")
    assert f.state == "absent"
    assert not any(x.id.startswith("mcp:cursor:") for x in findings)
    assert "cursorsrv" not in repr(findings)


def test_disabled_scan_returns_empty(tmp_path):
    p = plat(tmp_path, code=fx("claude_code.json"))
    assert detect_mcp_entries(p, enabled=False) == []
    assert "read_text" not in p.calls and "path_exists" not in p.calls
    assert p.calls == []


def test_never_writes(tmp_path):
    p = plat(tmp_path, code=fx("claude_code.json"), desktop=fx("bad.json"))
    before = dict(p.files)
    detect_mcp_entries(p)
    assert set(p.calls) <= ALLOWED
    assert p.run_calls == []
    assert p.files == before
    assert not any(tmp_path.iterdir())  # nothing created on disk
