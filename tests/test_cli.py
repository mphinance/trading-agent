"""The trading-agent command, end to end against the shim hermes with a real temp HERMES_HOME.

Nothing here touches the real home: os.environ is replaced with a clean dict for in-process runs and
subprocess runs get an explicit minimal environment."""
from __future__ import annotations

import io
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from test_profile_writer import Env, dotenv_read
from trading_agent import cli, paths
from trading_agent.profile.receipt import Receipt
from trading_agent.wizard.screens import INSTALL_CMD

ROOT = Path(__file__).parents[1]
FIXTURE_CATALOG = Path(__file__).parent / "fixtures" / "catalog"
KEY_ID, SECRET_KEY = "KEYID-0123456789", "SECRETKEY-9876543210"
# Pasted through the wizard: choose route, paste both keys, mode switch, then finish.
HAPPY = ["start", "", "scan", "skip", "continue", "pick:alpaca", "route:local", "continue",
         "paste", KEY_ID, "paste", SECRET_KEY, "true", "done", "do"]


def random_secret() -> str:
    """24 chars; every 4-character window holds symbols, so a window can never match ordinary output by chance."""
    rnd = random.SystemRandom()
    return "".join(rnd.choice(string.ascii_letters + string.digits) if i % 2 == 0 else rnd.choice("!%^*+=~@")
                   for i in range(24))


def no_window(text: str, secret: str) -> bool:
    return not any(secret[i:i + 4] in text for i in range(len(secret) - 3))


class Stdin:
    """A non-tty stdin: scripted lines, then either end of input or a Ctrl-C."""

    def __init__(self, lines, then="eof"):
        self.lines, self.then = [str(x) + "\n" for x in lines], then

    def readline(self, *_):
        if self.lines:
            return self.lines.pop(0)
        if self.then == "interrupt":
            raise KeyboardInterrupt
        return ""

    def isatty(self):
        return False


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = Env(tmp_path)
    (tmp_path / "world" / "home").mkdir(parents=True, exist_ok=True)
    clean = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path / "world" / "home"),
             "XDG_CONFIG_HOME": str(w.config_home), "HERMES_HOME": str(w.hermes_home)}
    monkeypatch.setattr(os, "environ", clean)
    w.environ = clean
    return w


def run_cli(monkeypatch, capsys, argv, lines=(), then="eof"):
    monkeypatch.setattr(sys, "stdin", Stdin(lines, then))
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def setup(world, monkeypatch, capsys, lines=HAPPY):
    return run_cli(monkeypatch, capsys, ["setup", "--distribution", str(world.dist)], lines)


# ---------------------------------------------------------------- setup and undo

def test_setup_end_to_end_with_shim(world, monkeypatch, capsys):
    rc, out, err = setup(world, monkeypatch, capsys)
    assert rc == 0 and "You are set up." in out
    assert "trading-agent undo --profile trader" in out
    assert dotenv_read(world.pdir() / ".env") == {
        "ALPACA_API_KEY": KEY_ID, "ALPACA_SECRET_KEY": SECRET_KEY, "ALPACA_PAPER_TRADE": "true"}
    cfg = yaml.safe_load((world.pdir() / "config.yaml").read_text())
    assert cfg["mcp_servers"]["alpaca"]["env"] == {
        "ALPACA_API_KEY": "${ALPACA_API_KEY}", "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}",
        "ALPACA_PAPER_TRADE": "${ALPACA_PAPER_TRADE}"}
    assert KEY_ID not in out + err and SECRET_KEY not in out + err
    assert Receipt.load(world.platform, "trader").profile_name == "trader"


def test_undo_removes_everything_setup_created(world, monkeypatch, capsys):
    before = {p for p in world.root.rglob("*") if not p.is_dir()}
    assert setup(world, monkeypatch, capsys)[0] == 0
    rc, out, err = run_cli(monkeypatch, capsys, ["undo"], ["y"])    # the only receipt is picked without asking
    assert rc == 0 and "Removed the profile trader" in out
    assert "ALPACA_API_KEY" in out and KEY_ID not in out + err and SECRET_KEY not in out + err
    assert not world.pdir().exists() and not world.receipt_path().exists()
    after = {p for p in world.root.rglob("*") if not p.is_dir()}
    # only the wizard's own progress file is left behind; it is not in the receipt, so undo leaves it
    assert {p.name for p in after - before} <= {"state.json", "calls.jsonl"}
    assert Receipt.list_receipts(world.platform) == []


@pytest.mark.parametrize("answer", [[""], ["n"], ["no"]])
def test_undo_default_is_no(world, monkeypatch, capsys, answer):
    setup(world, monkeypatch, capsys)
    rc, out, _ = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], answer)
    assert rc == 0 and "Nothing was removed." in out
    assert (world.pdir() / ".env").is_file() and world.receipt_path().is_file()


def test_undo_reasks_on_junk_and_never_echoes_it(world, monkeypatch, capsys):
    setup(world, monkeypatch, capsys)
    junk = random_secret()
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], [junk, "n"])
    assert rc == 0 and "Type y or n." in out and no_window(out + err, junk)


def test_undo_with_no_receipt_prints_one_plain_line_and_exits_1(world, monkeypatch, capsys):
    rc, out, err = run_cli(monkeypatch, capsys, ["undo"])
    assert rc == 1 and out.strip() == cli.NO_RECEIPT_ANY and err == ""
    rc, out, _ = run_cli(monkeypatch, capsys, ["undo", "--profile", "nobody"])
    assert rc == 1 and "no setup receipt for nobody" in out


def test_undo_rejects_a_bad_profile_name_without_echoing_it(world, monkeypatch, capsys):
    junk = random_secret()
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", junk])
    assert rc == 2 and no_window(out + err, junk)


def test_undo_for_a_profile_that_no_longer_exists_removes_nothing(world, monkeypatch, capsys):
    setup(world, monkeypatch, capsys)
    shutil.rmtree(world.pdir())
    bystander = world.pdir("other")
    bystander.mkdir(parents=True)
    (bystander / "keep.txt").write_text("mine")
    rc, out, _ = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], ["y"])
    assert rc == 1 and cli.UNDO_NOTHING in out
    assert (bystander / "keep.txt").read_text() == "mine"


def test_undo_with_two_receipts_lists_the_names_and_asks(world, monkeypatch, capsys):
    for name in ("alpha", "bravo"):
        Receipt(profile_name=name).save(world.platform)
    rc, out, _ = run_cli(monkeypatch, capsys, ["undo"], ["2", "n"])
    assert rc == 0 and "alpha" in out and "bravo" in out and "Which profile" in out
    assert "remove the Hermes profile bravo" in out and Receipt.list_receipts(world.platform) == ["alpha", "bravo"]


def test_a_receipt_that_cannot_be_read_removes_nothing(world, monkeypatch, capsys):
    Receipt(profile_name="trader").save(world.platform)
    world.receipt_path().write_text("{ not json")
    rc, out, _ = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"])
    assert rc == 1 and "could not be read" in out


def test_hermes_missing_returns_nonzero_and_prints_the_install_hint(world, monkeypatch, capsys, tmp_path):
    os.environ["PATH"] = str(tmp_path / "empty")
    rc, out, _ = setup(world, monkeypatch, capsys, ["start", "stop"])
    assert rc != 0 and INSTALL_CMD in out
    assert INSTALL_CMD in (ROOT / "docs" / "spike" / "T0-results.md").read_text()
    assert not world.pdir().exists() and not any("install" in " ".join(c) for c in world.calls())


# ---------------------------------------------------------------- scan

def test_scan_output_has_no_values(world, monkeypatch, capsys):
    secret = "sk-LEAK" + random_secret()
    os.environ["ALPACA_API_KEY"] = secret
    rc, out, err = run_cli(monkeypatch, capsys, ["scan"])
    assert rc == 0 and "ALPACA_API_KEY" in out and "broker-key" in out and "present" in out
    assert no_window(out + err, secret) and "sk-LEAK" not in out
    row = next(ln for ln in out.splitlines() if "ALPACA_API_KEY" in ln)
    assert [c.strip() for c in row.split("|")][1:3] == ["broker-key", "present"]


def test_scan_json_has_the_same_fields_and_no_values(world, monkeypatch, capsys):
    secret = "sk-LEAK" + random_secret()
    os.environ["ALPACA_API_KEY"] = secret
    rc, out, err = run_cli(monkeypatch, capsys, ["scan", "--json"])
    rows = json.loads(out)
    assert rc == 0 and all(set(r) == {"id", "kind", "state", "names"} for r in rows)
    assert any(r["names"] == ["ALPACA_API_KEY"] for r in rows) and no_window(out + err, secret)


# ---------------------------------------------------------------- Ctrl-C, EOF, unexpected errors

def _sweep_points():
    return range(len(HAPPY) - 1)           # every prompt up to and including the final review ("do" would write)


@pytest.mark.parametrize("n", _sweep_points())
@pytest.mark.parametrize("then,code", [("interrupt", 130), ("eof", 1)])
def test_ctrl_c_and_eof_at_every_prompt_stop_cleanly_and_leak_nothing(world, monkeypatch, capsys, n, then, code):
    secret = random_secret()
    lines = [secret if x in (KEY_ID, SECRET_KEY) else x for x in HAPPY[:n]]
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(world.dist)], lines, then)
    assert rc == code
    assert err.strip() == cli.STOPPED
    assert "Traceback" not in out + err and "File \"" not in out + err
    assert no_window(out + err, secret)
    state = world.config_home / "trading-agent" / "state.json"
    assert no_window(state.read_text() if state.exists() else "", secret)
    assert not world.pdir().exists()


def test_an_unexpected_exception_prints_only_its_type(world, monkeypatch, capsys):
    secret = random_secret()

    def boom(_args):
        raise RuntimeError(f"details with {secret}")
    monkeypatch.setattr(cli, "_cmd_scan", boom)
    rc, out, err = run_cli(monkeypatch, capsys, ["scan"])
    assert rc == 1 and "RuntimeError" in err and err.count("\n") == 1
    assert no_window(out + err, secret) and "Traceback" not in err


def test_a_closed_pipe_exits_quietly(world, monkeypatch, capsys):
    def boom(_args):
        raise BrokenPipeError
    monkeypatch.setattr(cli, "_cmd_scan", boom)
    monkeypatch.setattr(cli, "_quiet_stdout", lambda: None)
    rc, out, err = run_cli(monkeypatch, capsys, ["scan"])
    assert rc == 1 and "Traceback" not in err


# ---------------------------------------------------------------- catalog build

def test_catalog_build_writes_json_and_exits_0(tmp_path, capsys):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{}")
    out = tmp_path / "out" / "catalog.json"
    assert cli.main(["catalog", "build", "--catalog-root", str(FIXTURE_CATALOG), "--out", str(out),
                     "--overlay", str(overlay)]) == 0
    doc = json.loads(out.read_text())
    assert {e["slug"] for e in doc["entries"]} >= {"alpaca", "etoro"}


def test_catalog_build_on_a_bad_root_fails_with_one_line(tmp_path, capsys):
    rc = cli.main(["catalog", "build", "--catalog-root", str(tmp_path), "--out", str(tmp_path / "o.json")])
    err = capsys.readouterr().err
    assert rc != 0 and err.startswith("error:") and err.count("\n") == 1 and not (tmp_path / "o.json").exists()


# ---------------------------------------------------------------- distribution

def test_a_missing_distribution_override_exits_2_with_one_line(world, monkeypatch, capsys, tmp_path):
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(tmp_path / "nope")])
    assert rc == 2 and err.strip() == cli.NOT_IN_BUILD and out == ""


def test_the_packaged_distribution_missing_exits_2_not_a_traceback(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(paths, "_distribution_root", lambda: tmp_path / "absent")
    rc, out, err = run_cli(monkeypatch, capsys, ["setup"])
    assert rc == 2 and err.strip() == cli.NOT_IN_BUILD and "Traceback" not in err


def test_the_default_distribution_is_the_packaged_one(world, monkeypatch, capsys):
    monkeypatch.setattr(paths, "_distribution_root", lambda: world.dist)
    rc, out, _ = run_cli(monkeypatch, capsys, ["setup"], HAPPY)
    assert rc == 0 and (world.pdir() / "SOUL.md").is_file()


def test_a_zipped_distribution_is_extracted_to_a_real_directory(monkeypatch, tmp_path):
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("dist/distribution.yaml", "name: x\n")
        f.writestr("dist/sub/SOUL.md", "soul\n")
    monkeypatch.setattr(paths, "_distribution_root", lambda: zipfile.Path(z, "dist/"))
    d = paths.distribution_dir()
    assert d is not None and (d / "distribution.yaml").read_text() == "name: x\n"
    assert (d / "sub" / "SOUL.md").read_text() == "soul\n"
    shutil.rmtree(d)


def test_the_real_package_has_no_distribution_yet_or_a_usable_one():
    d = paths.distribution_dir()
    assert d is None or d.is_dir()


# ---------------------------------------------------------------- help, entry points

@pytest.mark.parametrize("argv", [[], ["setup"], ["undo"], ["catalog"], ["catalog", "build"], ["scan"]])
def test_help_works_everywhere_and_mentions_no_secret(world, monkeypatch, capsys, argv):
    os.environ["ALPACA_API_KEY"] = "sk-LEAKHELP"
    rc = cli.main([*argv, "--help"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("usage: trading-agent") and "sk-LEAK" not in out


def test_the_top_level_help_names_every_command(capsys):
    cli.main(["--help"])
    out = capsys.readouterr().out
    for word in ("setup", "undo", "catalog", "scan"):
        assert word in out


def test_usage_errors_exit_2(capsys):
    assert cli.main([]) == 2 and cli.main(["frobnicate"]) == 2
    capsys.readouterr()


def test_pyproject_declares_the_console_script():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["scripts"] == {"trading-agent": "trading_agent.cli:main"}
    assert "include" not in meta["tool"]["hatch"]["build"]["targets"]["wheel"]


def sub_env(w: Env) -> dict:
    return {"PATH": f"{w.root / 'bin'}:/usr/bin:/bin", "HOME": str(w.root / "world" / "home"),
            "XDG_CONFIG_HOME": str(w.config_home), "HERMES_HOME": str(w.hermes_home),
            "PYTHONPATH": str(ROOT / "src")}


def test_python_dash_m_runs_help():
    r = subprocess.run([sys.executable, "-m", "trading_agent", "--help"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode == 0 and "trading-agent" in r.stdout


def test_true_end_to_end_in_a_subprocess_setup_then_undo(tmp_path):
    w = Env(tmp_path)
    (tmp_path / "world" / "home").mkdir(parents=True, exist_ok=True)
    env = sub_env(w)
    cmd = [sys.executable, "-m", "trading_agent"]
    r = subprocess.run([*cmd, "setup", "--distribution", str(w.dist)], input="\n".join(HAPPY) + "\n",
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert dotenv_read(w.pdir() / ".env")["ALPACA_API_KEY"] == KEY_ID
    assert KEY_ID not in r.stdout + r.stderr and SECRET_KEY not in r.stdout + r.stderr
    assert [c for c in w.calls() if c[:2] == ["profile", "install"]]       # the shim ran, not a real hermes
    u = subprocess.run([*cmd, "undo", "--profile", "trader"], input="y\n", capture_output=True, text=True,
                       env=env, timeout=120)
    assert u.returncode == 0 and "Removed the profile trader" in u.stdout
    assert not w.pdir().exists() and not w.receipt_path().exists()
    # Ctrl-C stand-in: end of input mid-wizard through a real pipe
    e = subprocess.run([*cmd, "setup", "--distribution", str(w.dist)], input="start\n", capture_output=True,
                       text=True, env=env, timeout=120)
    assert e.returncode == 1 and e.stderr.strip() == cli.STOPPED and "Traceback" not in e.stderr
