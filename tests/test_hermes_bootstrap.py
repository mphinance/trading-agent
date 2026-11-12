from __future__ import annotations

import pytest

from fakes import FakePlatform
from trading_agent.platform import RunResult
from trading_agent.profile.hermes_cli import HermesCli, HermesNotBootstrapped, HermesUnavailable


def cli(tmp_path, *results):
    fp = FakePlatform(tmp_path, which_map={"hermes": "/opt/hermes"}, run_results=list(results))
    return HermesCli(fp)


def test_not_bootstrapped_when_pm_repair_text(tmp_path):
    text = "no dependency environment is committed for this install; run `hermes pm repair`"
    with pytest.raises(HermesNotBootstrapped):
        cli(tmp_path, RunResult(1, "", text, False)).version()


def test_not_bootstrapped_when_text_is_on_stdout_with_exit_zero(tmp_path):
    text = "No dependency environment is committed for this install"
    with pytest.raises(HermesNotBootstrapped):
        cli(tmp_path, RunResult(0, text + "\n", "", False)).version()


def test_not_bootstrapped_is_distinct_from_a_plain_failure(tmp_path):
    with pytest.raises(HermesUnavailable) as e:
        cli(tmp_path, RunResult(1, "", "boom", False)).version()
    assert not isinstance(e.value, HermesNotBootstrapped)


def test_timeout_is_a_plain_failure_even_with_marker_text(tmp_path):
    with pytest.raises(HermesUnavailable) as e:
        cli(tmp_path, RunResult(-1, "", "run hermes pm repair", True)).version()
    assert not isinstance(e.value, HermesNotBootstrapped)


def test_normal_version_still_returned(tmp_path):
    assert cli(tmp_path, RunResult(0, "Hermes Agent v0.23.1\nInstall method: git\n", "", False)).version() \
        == "Hermes Agent v0.23.1"
