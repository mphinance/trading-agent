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
