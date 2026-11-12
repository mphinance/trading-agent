import pytest

from fakes import FakePlatform
from trading_agent.platform import LinuxPlatform, RunResult


def test_login_shell_has_var_returns_bool_only(tmp_path):
    p = FakePlatform(tmp_path, environ={"SHELL": "/bin/bash"},
                     run_results=[RunResult(0, "1\n", "", False)])
    assert p.login_shell_has_var("OPENAI_API_KEY") is True
    assert len(p.run_calls) == 1
    argv = p.run_calls[0]
    assert argv[:2] == ["/bin/bash", "-lc"]
    assert "OPENAI_API_KEY" in argv[2]
    assert "printenv" not in argv[2] and "env" not in argv[2].split()


def test_login_shell_has_var_false_when_empty(tmp_path):
    p = FakePlatform(tmp_path, run_results=[RunResult(1, "", "", False)])
    assert p.login_shell_has_var("FOO") is False


def test_login_shell_has_var_rejects_bad_name(tmp_path):
    p = FakePlatform(tmp_path)
    with pytest.raises(ValueError):
        p.login_shell_has_var("x; env")
    assert p.run_calls == []


def test_run_times_out():
    assert LinuxPlatform().run(["sleep", "5"], timeout=0.2).timed_out is True


def test_run_captures_output():
    r = LinuxPlatform().run(["echo", "hi"], timeout=5)
    assert r.returncode == 0 and r.stdout == "hi\n" and not r.timed_out


def test_login_shell_has_var_rejects_trailing_newline(tmp_path):
    p = FakePlatform(tmp_path)
    with pytest.raises(ValueError):
        p.login_shell_has_var("FOO\n")
    with pytest.raises(ValueError):
        p.login_shell_vars(["OK", "FOO\n"])
    assert p.run_calls == []


def test_login_shell_vars_script_shape_and_result(tmp_path):
    p = FakePlatform(tmp_path, environ={"SHELL": "/bin/bash"},
                     run_results=[RunResult(0, "A\nC\n", "", False)])
    assert p.login_shell_vars(["A", "B", "C"]) == frozenset({"A", "C"})
    argv = p.run_calls[0]
    assert argv[:2] == ["/bin/bash", "-lc"]
    assert argv[2] == '[ -n "${A}" ] && echo A; [ -n "${B}" ] && echo B; [ -n "${C}" ] && echo C'


def test_login_shell_vars_failure_modes_are_empty(tmp_path):
    for r in (RunResult(1, "A\n", "", False), RunResult(127, "", "boom", False),
              RunResult(-1, "A\n", "", True)):
        assert FakePlatform(tmp_path, run_results=[r]).login_shell_vars(["A"]) == frozenset()


def test_login_shell_vars_empty_input_spawns_nothing(tmp_path):
    p = FakePlatform(tmp_path)
    assert p.login_shell_vars([]) == frozenset()
    assert p.run_calls == []


def test_linux_login_shell_vars_real_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    got = LinuxPlatform().login_shell_vars(["PATH", "ZZ_NOPE_NOT_SET"])
    assert "ZZ_NOPE_NOT_SET" not in got
