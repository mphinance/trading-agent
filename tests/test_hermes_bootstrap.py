from __future__ import annotations

import pytest

from fakes import FakePlatform
from trading_agent.platform import RunResult
from trading_agent.profile.hermes_cli import (
    HermesCli, HermesNotBootstrapped, HermesUnavailable, parse_hermes_version,
)

T0 = "no dependency environment is committed for this install; run `hermes pm repair`"


def cli(tmp_path, *results):
    fp = FakePlatform(tmp_path, which_map={"hermes": "/opt/hermes"}, run_results=list(results))
    return HermesCli(fp)


@pytest.mark.parametrize("res", [RunResult(1, "", T0, False), RunResult(2, T0 + "\n", "", False)])
def test_not_bootstrapped_only_on_the_exact_t0_signature_with_a_nonzero_exit(tmp_path, res):
    with pytest.raises(HermesNotBootstrapped):
        cli(tmp_path, res).version()


@pytest.mark.parametrize("res", [
    RunResult(1, "", "run hermes pm repair", False),                       # bare substring
    RunResult(1, "", "Error: corrupted venv, run `hermes pm repair`", False),
    RunResult(1, "", T0 + " (corrupt)", False),
    RunResult(1, "", "boom", False),
    RunResult(0, "", "", False),                                           # empty output
    RunResult(-1, "", T0, True),                                           # timeout, even with the text
])
def test_everything_else_is_a_plain_failure(tmp_path, res):
    with pytest.raises(HermesUnavailable) as e:
        cli(tmp_path, res).version()
    assert not isinstance(e.value, HermesNotBootstrapped)


def test_normal_version_returned_and_the_hermes_line_is_preferred(tmp_path):
    assert cli(tmp_path, RunResult(0, "Hermes Agent v0.23.1\nInstall method: git\n", "", False)).version() \
        == "Hermes Agent v0.23.1"
    assert cli(tmp_path, RunResult(0, "Python 3.12.4\nHermes Agent v0.21.7\n", "", False)).version() \
        == "Hermes Agent v0.21.7"


@pytest.mark.parametrize("text,expected", [
    ("Hermes Agent v0.23.1 (2026.9.24) · stable", ((0, 23, 1), False)),
    ("Hermes Agent 0.23.0", ((0, 23, 0), False)),
    ("Python 3.12.4; hermes 0.21.0", ((0, 21, 0), False)),         # not the first x.y.z
    ("hermes v0.23.0+canary.20261012", ((0, 23, 0), False)),
    ("Hermes Agent v0.23.0rc1", ((0, 23, 0), True)),
    ("Hermes Agent v0.23.0-rc1", ((0, 23, 0), True)),
    ("v0.24.2", ((0, 24, 2), False)),
    ("Hermes Agent (dev build)", None),
    ("garbage 1.2.3", None),
    ("", None),
])
def test_parse_hermes_version(text, expected):
    assert parse_hermes_version(text) == expected


def test_exit_zero_with_the_signature_text_is_just_unreadable_output_not_bootstrap(tmp_path):
    line = cli(tmp_path, RunResult(0, T0 + "\n", "", False)).version()
    assert parse_hermes_version(line) is None


def test_run_never_falls_back_to_a_bare_hermes_when_the_lookup_finds_none(tmp_path, monkeypatch):
    import os
    import stat
    from trading_agent.platform import LinuxPlatform
    marker = tmp_path / "PLANTED-RAN"
    planted = tmp_path / "cwd" / "hermes"
    planted.parent.mkdir()
    planted.write_text(f"#!/bin/sh\ntouch {marker}\necho 'Hermes Agent v0.23.1'\n")
    planted.chmod(planted.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(planted.parent)
    monkeypatch.setenv("PATH", ":/usr/bin:/bin")
    cli = HermesCli(LinuxPlatform())
    for call in (cli.version, lambda: cli.profile_exists("trader"), lambda: cli.profile_install(tmp_path, "trader"),
                 lambda: cli.config_set("trader", "a.b", "c")):
        with pytest.raises(HermesUnavailable):
            call()
    assert not marker.exists() and not os.path.exists(marker)


def test_resume_at_review_then_write_never_runs_a_planted_hermes(tmp_path, monkeypatch):
    """PRECHECK is skipped when resuming at REVIEW, so the choke point in run() is what protects WRITE."""
    import stat
    from fakes import ScriptedIO
    from trading_agent.platform import LinuxPlatform
    from trading_agent.wizard.flow import Deps, Detectors, run_wizard
    from trading_agent.wizard.state import State, WizardState
    marker = tmp_path / "PLANTED-RAN"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "hermes").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (cwd / "hermes").chmod(0o755)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", ":/usr/bin:/bin")

    class P(LinuxPlatform):
        def config_home(self):
            return tmp_path / "cfg"

        def home(self):
            return tmp_path / "home"
    plat = P()
    WizardState(State.REVIEW, {"profile_name": "trader", "ai.kind": "none", "skills.done": "1"}).save(plat)
    deps = Deps(platform=plat, hermes=HermesCli(plat), detectors=Detectors(lambda: [], lambda e: [], lambda: [], lambda: []),
                catalog=[], libraries=[], store_factory=lambda d: None, source_dir=tmp_path)
    io = ScriptedIO(["do", "stop"])
    assert run_wizard(io, deps) is None
    assert not marker.exists()
    assert "Setup did not finish" in "\n".join(io.said)


def test_multi_line_output_prefers_the_token_next_to_hermes(tmp_path):
    line = cli(tmp_path, RunResult(0, "Built with toolchain v9.9.9\nHermes Agent v0.21.0\n", "", False)).version()
    assert parse_hermes_version(line) == ((0, 21, 0), False)


def test_a_printed_version_beats_the_bootstrap_signature(tmp_path):
    out = T0 + "\nHermes Agent v0.10.0\n"
    line = cli(tmp_path, RunResult(1, out, "", False)).version()      # not "ok, continue": the pin gets enforced
    assert parse_hermes_version(line) == ((0, 10, 0), False)


@pytest.mark.parametrize("text", ["no dependency environment is committed; run hermes pm repair",
                                  "no dependency environment is committed for this install"])
def test_only_the_exact_t0_message_counts_as_not_bootstrapped(tmp_path, text):
    with pytest.raises(HermesUnavailable) as e:
        cli(tmp_path, RunResult(1, "", text, False)).version()
    assert not isinstance(e.value, HermesNotBootstrapped)
