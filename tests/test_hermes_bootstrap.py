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
