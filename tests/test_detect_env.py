from __future__ import annotations

from fakes import FakePlatform
from trading_agent.detect.env import (
    CredentialSpec, detect_env, load_credential_specs, load_info_names,
)
from trading_agent.platform import RunResult

ALPACA = CredentialSpec("env", "broker-key", "Alpaca API key",
                        ("ALPACA_API_KEY",), "copy-with-consent")


def one(findings, fid):
    hits = [f for f in findings if f.id == fid]
    assert len(hits) == 1
    return hits[0]


def test_present_when_set(tmp_path):
    p = FakePlatform(tmp_path, environ={"ALPACA_API_KEY": "x"})
    f = one(detect_env(p, [ALPACA]), "env:ALPACA_API_KEY")
    assert f.state == "present"
    assert f.names == ("ALPACA_API_KEY",)
    assert f.reusable == "copy-with-consent"
    assert f.kind == "broker-key"


def test_empty_value_counts_as_absent(tmp_path):
    p = FakePlatform(tmp_path, environ={"ALPACA_API_KEY": ""})
    f = one(detect_env(p, [ALPACA]), "env:ALPACA_API_KEY")
    assert f.state == "absent"
    assert p.run_calls  # fell through to the login shell probe, nothing else


def test_absent_when_missing(tmp_path):
    p = FakePlatform(tmp_path)
    assert one(detect_env(p, [ALPACA]), "env:ALPACA_API_KEY").state == "absent"


def test_value_never_in_any_field(tmp_path):
    p = FakePlatform(tmp_path, environ={"ALPACA_API_KEY": "sk-SECRET123"})
    findings = detect_env(p, load_credential_specs())
    assert findings
    for f in findings:
        assert "SECRET123" not in repr(f)


def test_login_shell_fallback(tmp_path):
    p = FakePlatform(tmp_path, run_results=[RunResult(0, "ALPACA_API_KEY\n", "", False)])
    f = one(detect_env(p, [ALPACA]), "env:ALPACA_API_KEY")
    assert f.state == "present"
    assert f.where == "login shell environment"


def test_process_env_wins_over_login_shell(tmp_path):
    p = FakePlatform(tmp_path, environ={"ALPACA_API_KEY": "x"})
    f = one(detect_env(p, [ALPACA]), "env:ALPACA_API_KEY")
    assert f.where == "environment variable"
    assert p.run_calls == []


def test_reference_only_rows_never_copy_with_consent():
    by_name = {n: s.reusable for s in load_credential_specs() for n in s.names}
    for name in ("POLYMARKET_PRIVATE_KEY", "TASTYTRADE_REFRESH_TOKEN", "TR_PIN",
                 "DEGIRO_PASSWORD", "DEGIRO_TOTP_SECRET", "MOOMOO_TRADE_PASSWORD",
                 "TIGEROPEN_TOKEN"):
        assert by_name[name] == "reference-only", name
    assert by_name["ALPACA_API_KEY"] == "copy-with-consent"


def test_ai_keys_present_and_info_names_separate():
    by_name = {n: s for s in load_credential_specs() for n in s.names}
    for name in ("OPENROUTER_API_KEY", "VENICE_API_KEY", "OPENAI_API_KEY",
                 "ANTHROPIC_API_KEY"):
        assert by_name[name].kind == "ai-key"
        assert by_name[name].reusable == "copy-with-consent"
    info = load_info_names()
    assert "ALPACA_PAPER_TRADE" in info
    assert not set(info) & set(by_name)


def test_all_specs_valid_and_unique():
    specs = load_credential_specs()
    names = [n for s in specs for n in s.names]
    assert len(names) == len(set(names))
    for s in specs:
        assert s.kind in {"broker-key", "data-key", "ai-key"}
        assert s.reusable in {"reference-only", "copy-with-consent"}


def test_one_shell_spawn_for_many_absent_names(tmp_path):
    names = tuple(f"VAR_{i}" for i in range(50))
    spec = CredentialSpec("env", "broker-key", "x", names, "copy-with-consent")
    p = FakePlatform(tmp_path)
    findings = detect_env(p, [spec])
    assert len(p.run_calls) == 1
    assert len(findings) == 50 and all(f.state == "absent" for f in findings)


def test_no_shell_spawn_when_nothing_missing(tmp_path):
    p = FakePlatform(tmp_path, environ={"ALPACA_API_KEY": "x"})
    detect_env(p, [ALPACA])
    assert p.run_calls == []


def test_shell_timeout_means_absent_and_does_not_raise(tmp_path):
    p = FakePlatform(tmp_path, run_results=[RunResult(-1, "ALPACA_API_KEY\n", "", True)])
    f = one(detect_env(p, [ALPACA]), "env:ALPACA_API_KEY")
    assert f.state == "absent"


def test_shell_stdout_filtered_to_requested_names(tmp_path):
    p = FakePlatform(tmp_path, run_results=[
        RunResult(0, "ALPACA_API_KEY\nSOMETHING_ELSE\nsk-SECRET123\n", "", False)])
    assert p.login_shell_vars(["ALPACA_API_KEY"]) == frozenset({"ALPACA_API_KEY"})


def test_argv_contains_names_never_values(tmp_path):
    p = FakePlatform(tmp_path, environ={"OPENAI_API_KEY": "sk-SECRET123"},
                     run_results=[RunResult(0, "ALPACA_API_KEY\n", "", False)])
    findings = detect_env(p, load_credential_specs())
    script = p.run_calls[0][2]
    assert "ALPACA_API_KEY" in script
    assert "SECRET123" not in script
    assert "OPENAI_API_KEY" not in script  # set in process env, never probed
    assert all("SECRET123" not in repr(f) for f in findings)
