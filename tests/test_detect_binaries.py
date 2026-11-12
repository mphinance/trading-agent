from __future__ import annotations

from fakes import FakePlatform
from trading_agent.detect.binaries import AI_CLIS, detect_ai_clis
from trading_agent.platform import RunResult


def by_name(findings, name):
    hits = [f for f in findings if f.names == (name,)]
    assert len(hits) == 1
    return hits[0]


def test_cli_found_via_known_path_not_on_path(tmp_path):
    exe = tmp_path / ".local" / "bin" / "claude"
    p = FakePlatform(tmp_path, files={exe: ""})
    f = by_name(detect_ai_clis(p), "claude")
    assert f.state == "present"
    assert f.where == str(exe)
    assert f.what.endswith("(found, not tested)")


def test_cli_missing_lists_looked_in(tmp_path):
    f = by_name(detect_ai_clis(FakePlatform(tmp_path)), "codex")
    assert f.state == "absent"
    assert f.where.startswith("Looked in:")
    for frag in (".local/bin", ".volta/bin", ".asdf/shims", "/opt/homebrew/bin", "PATH"):
        assert frag in f.where


def test_version_run_uses_empty_env(tmp_path):
    calls = []

    class P(FakePlatform):
        def run(self, argv, *, timeout, env=None, stdin=None):
            calls.append((list(argv), timeout, env))
            if "--version" in argv:
                return RunResult(0, "claude 1.2.3\nextra\n", "", False)
            return RunResult(1, "", "", False)

    p = P(tmp_path, which_map={"claude": "/usr/bin/claude"})
    f = by_name(detect_ai_clis(p), "claude")
    ver = [c for c in calls if "--version" in c[0]]
    assert len(ver) == 1
    assert ver[0][2] == {} and ver[0][1] == 3
    assert "claude 1.2.3" in f.what and "extra" not in f.what


def test_login_shell_lookup_nonzero_exit_keeps_path(tmp_path):
    p = FakePlatform(tmp_path, run_results=[RunResult(1, "/opt/x/bin/claude\n", "", False)])
    f = by_name(detect_ai_clis(p), "claude")
    assert f.state == "present" and f.where == "/opt/x/bin/claude"


def test_login_shell_noise_is_filtered(tmp_path):
    p = FakePlatform(tmp_path, run_results=[RunResult(0, "alias claude='x'\nwelcome\n", "", False)])
    assert by_name(detect_ai_clis(p), "claude").state == "absent"


def test_nvm_glob_found(tmp_path):
    root = tmp_path / ".nvm" / "versions" / "node"
    exe = root / "v20.1.0" / "bin" / "gemini"
    p = FakePlatform(tmp_path, files={exe: ""}, dirs={root: ["v20.1.0"]})
    assert by_name(detect_ai_clis(p), "gemini").where == str(exe)


def test_oserror_never_aborts(tmp_path):
    class P(FakePlatform):
        def list_dir(self, path):
            raise PermissionError("no")

        def path_exists(self, path):
            raise PermissionError("no")

    findings = detect_ai_clis(P(tmp_path))
    assert [f.names[0] for f in findings] == list(AI_CLIS)
    assert all(f.state == "absent" for f in findings)


def test_version_capped_and_sanitized(tmp_path):
    class P(FakePlatform):
        def run(self, argv, *, timeout, env=None, stdin=None):
            if "--version" in argv:
                return RunResult(0, "v" + "9" * 500 + "\x1b[31m", "", False)
            return RunResult(0, "", "", False)

    f = by_name(detect_ai_clis(P(tmp_path, which_map={"agy": "/bin/agy"})), "agy")
    assert len(f.what) < 100 and "\x1b" not in f.what


def test_ai_cli_findings_are_reference_only(tmp_path):
    p = FakePlatform(tmp_path, which_map={"claude": "/usr/bin/claude"})
    findings = detect_ai_clis(p)
    assert findings
    assert all(f.kind == "ai-cli" and f.reusable == "reference-only" for f in findings)
