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
    assert argv[2] == ('if [ -n "${A}" ]; then echo A; fi; if [ -n "${B}" ]; then echo B; fi; '
                       'if [ -n "${C}" ]; then echo C; fi; true')


def test_login_shell_vars_failure_modes_are_empty(tmp_path):
    for r in (RunResult(1, "", "", False), RunResult(127, "", "boom", False),
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


# ---- real shell regression tests (batch script exit status) ----
import os

needs_sh = pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="no /bin/sh")


@pytest.fixture
def real_sh(monkeypatch, tmp_path):
    # Real POSIX shell, but without sourcing the host's profile files (a broken
    # /etc/profile.d script would abort a true login shell and make the test host-dependent).
    wrapper = tmp_path / "sh-nologin"
    wrapper.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("SHELL", str(wrapper))
    for n in ("RS_A", "RS_B", "RS_C", "RS_D", "RS_E"):
        monkeypatch.delenv(n, raising=False)
    return monkeypatch


@needs_sh
def test_real_shell_unset_name_last(real_sh):
    real_sh.setenv("RS_A", "x")
    real_sh.setenv("RS_B", "")
    real_sh.setenv("RS_D", "y")
    got = LinuxPlatform().login_shell_vars(["RS_A", "RS_B", "RS_C", "RS_D", "RS_E"])
    assert got == frozenset({"RS_A", "RS_D"})


@needs_sh
def test_real_shell_unset_first_set_last(real_sh):
    real_sh.setenv("RS_E", "z")
    got = LinuxPlatform().login_shell_vars(["RS_A", "RS_B", "RS_E"])
    assert got == frozenset({"RS_E"})


@needs_sh
def test_real_shell_all_unset(real_sh):
    assert LinuxPlatform().login_shell_vars(["RS_A", "RS_B"]) == frozenset()


@needs_sh
def test_real_shell_hostile_value_never_printed(real_sh):
    real_sh.setenv("RS_A", "it's \"q\" $(echo PWNED) `echo PWNED`")
    p = LinuxPlatform()
    got = p.login_shell_vars(["RS_A", "RS_B"])
    assert got == frozenset({"RS_A"})
    # and the raw stdout of the generated script carries only the name
    from trading_agent.platform import shell_vars

    class Spy(LinuxPlatform):
        out = ""

        def run(self, argv, **kw):
            res = super().run(argv, **kw)
            Spy.out = res.stdout
            return res

    shell_vars(Spy(), ["RS_A", "RS_B"])
    assert Spy.out.split() == ["RS_A"]
    assert "PWNED" not in Spy.out


def test_nonzero_rc_keeps_printed_names(tmp_path):
    p = FakePlatform(tmp_path, run_results=[RunResult(1, "A\nnoise\n", "", False)])
    assert p.login_shell_vars(["A", "B"]) == frozenset({"A"})


def test_timeout_and_oserror_still_empty_even_with_stdout(tmp_path):
    assert FakePlatform(tmp_path, run_results=[RunResult(-1, "A\n", "", True)]
                        ).login_shell_vars(["A"]) == frozenset()
    assert FakePlatform(tmp_path, run_results=[RunResult(127, "", "boom", False)]
                        ).login_shell_vars(["A"]) == frozenset()


def test_which_ignores_relative_and_empty_path_entries(tmp_path, monkeypatch):
    import os
    from trading_agent.platform import LinuxPlatform
    planted = tmp_path / "cwd" / "hermes"
    planted.parent.mkdir()
    planted.write_text("#!/bin/sh\necho pwned\n")
    planted.chmod(0o755)
    good = tmp_path / "bin"
    good.mkdir()
    monkeypatch.chdir(planted.parent)
    for path in (":/nonexistent", ".:/nonexistent", "/nonexistent::", "rel/dir:."):
        monkeypatch.setenv("PATH", path)
        assert LinuxPlatform().which("hermes") is None
    ok = good / "hermes"
    ok.write_text("#!/bin/sh\n")
    ok.chmod(0o755)
    monkeypatch.setenv("PATH", f".:{good}")
    assert LinuxPlatform().which("hermes") == ok
    monkeypatch.setenv("PATH", "")
    assert LinuxPlatform().which("hermes") is None
