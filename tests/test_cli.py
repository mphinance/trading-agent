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
from trading_agent.wizard.flow import Detectors
from trading_agent.wizard.screens import INSTALL_CMD

ROOT = Path(__file__).parents[1]
FIXTURE_CATALOG = Path(__file__).parent / "fixtures" / "catalog"
REAL_DETECTORS = cli._detectors
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
    # Fast fakes: the handler tests do not need to probe ports or spawn a login shell. The real detectors run in
    # the scan tests (real_detectors) and in the one subprocess end-to-end test.
    monkeypatch.setattr(cli, "_detectors", lambda platform: Detectors(
        env=lambda: [], mcp_files=lambda enabled: [], ai_clis=lambda: [], ports=lambda: []))
    w.environ = clean
    return w


@pytest.fixture
def real_detectors(monkeypatch):
    monkeypatch.setattr(cli, "_detectors", REAL_DETECTORS)


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
    assert rc == 1 and err.strip() == cli.NO_RECEIPT_ANY and out == ""
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "nobody"])
    assert rc == 1 and "no setup receipt for nobody" in err and out == ""


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
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], ["y"])
    assert rc == 1 and cli.UNDO_NOTHING in err
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
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"])
    assert rc == 1 and "could not be read" in err


def test_hermes_missing_returns_nonzero_and_prints_the_install_hint(world, monkeypatch, capsys, tmp_path):
    os.environ["PATH"] = str(tmp_path / "empty")
    rc, out, _ = setup(world, monkeypatch, capsys, ["start", "stop"])
    assert rc != 0 and INSTALL_CMD in out
    assert INSTALL_CMD in (ROOT / "docs" / "spike" / "T0-results.md").read_text()
    assert not world.pdir().exists() and not any("install" in " ".join(c) for c in world.calls())


# ---------------------------------------------------------------- scan

def test_scan_output_has_no_values(world, real_detectors, monkeypatch, capsys):
    secret = "sk-LEAK" + random_secret()
    os.environ["ALPACA_API_KEY"] = secret
    rc, out, err = run_cli(monkeypatch, capsys, ["scan"])
    assert rc == 0 and "ALPACA_API_KEY" in out and "broker-key" in out and "present" in out
    assert no_window(out + err, secret) and "sk-LEAK" not in out
    row = next(ln for ln in out.splitlines() if "ALPACA_API_KEY" in ln)
    assert [c.strip() for c in row.split("|")][1:3] == ["broker-key", "present"]


def test_scan_json_has_the_same_fields_and_no_values(world, real_detectors, monkeypatch, capsys):
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
    assert err.strip() == cli.STOPPED and "setup" in err
    assert "Traceback" not in out + err and "File \"" not in out + err
    assert no_window(out + err, secret)
    state = world.config_home / "trading-agent" / "state.json"
    assert no_window(state.read_text() if state.exists() else "", secret)
    assert not world.pdir().exists()


def test_an_unexpected_exception_prints_only_its_type(world, monkeypatch, capsys):
    secret = random_secret()

    def boom(_args):
        raise RuntimeError(f"details with {secret}")
    monkeypatch.setattr(cli, "_cmd_setup", boom)
    rc, out, err = run_cli(monkeypatch, capsys, ["setup"])
    assert rc == 1 and "RuntimeError" in err and err.count("\n") == 1
    assert "trading-agent undo" in err and "Nothing was changed" not in err
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
    assert rc == 2 and err.strip() == cli.NOT_A_DISTRIBUTION and out == ""


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
    for word in ("setup", "undo", "export", "import", "catalog", "scan"):
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


# ---------------------------------------------------------------- fix round 1

def _plant_receipt_copy(world, as_name):
    shutil.copy(world.receipt_path(), world.receipt_path(as_name))


def test_undo_refuses_a_receipt_file_whose_body_names_another_profile(world, monkeypatch, capsys):
    setup(world, monkeypatch, capsys)
    _plant_receipt_copy(world, "alpha")                       # alpha.json says profile_name trader
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "alpha"], ["y"])
    assert rc == 1 and err.strip() == cli.BAD_RECEIPT.format(name="alpha") and "Remove it?" not in out
    assert (world.pdir() / ".env").is_file() and world.receipt_path().is_file()


def test_undo_refuses_a_symlinked_receipt_file(world, monkeypatch, capsys):
    setup(world, monkeypatch, capsys)
    world.receipt_path("zulu").symlink_to(world.receipt_path())
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "zulu"], ["y"])
    assert rc == 1 and err.strip() == cli.BAD_RECEIPT.format(name="zulu")
    assert (world.pdir() / ".env").is_file()
    # even when the body happens to name the same profile as the link's own name
    world.receipt_path("zulu").unlink()
    world.receipt_path("trader").rename(world.receipt_path("moved"))
    world.receipt_path("trader").symlink_to(world.receipt_path("moved"))
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], ["y"])
    assert rc == 1 and (world.pdir() / ".env").is_file()


def test_undo_validates_the_receipts_profile_name_before_asking(world, monkeypatch, capsys):
    Receipt(profile_name="ok").save(world.platform)
    doc = json.loads(world.receipt_path("ok").read_text())
    doc["profile_name"] = "../evil"
    world.receipt_path("ok").write_text(json.dumps(doc))
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "ok"], ["y"])
    assert rc == 1 and err.strip() == cli.BAD_RECEIPT.format(name="ok") and "Remove it?" not in out


def test_undo_prints_receipt_names_through_the_safe_filter(world, monkeypatch, capsys):
    Receipt(profile_name="trader", env_names_written=["\x1b[31mEVIL\x07", "sk-" + "a1B2" * 10]).save(world.platform)
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], ["n"])
    assert rc == 0 and "\x1b" not in out + err and "\x07" not in out + err and "EVIL" in out
    assert "sk-" + "a1B2" * 10 not in out


def test_setup_profile_flag_names_the_new_profile(world, monkeypatch, capsys):
    lines = [x for x in HAPPY if x != ""]                       # no name prompt any more
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--profile", "myagent", "--distribution", str(world.dist)],
                           lines)
    assert rc == 0 and world.pdir("myagent").is_dir() and not world.pdir("trader").exists()
    assert "trading-agent undo --profile myagent" in out and "Pick a name" not in out


@pytest.mark.parametrize("bad", ["../x", "Hermes", "default", ""])
def test_setup_profile_flag_is_validated_and_never_echoed(world, monkeypatch, capsys, bad):
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--profile", bad, "--distribution", str(world.dist)])
    assert rc == 2 and err.strip() == cli.BAD_NAME and (not bad or bad not in out + err)


def test_setup_profile_flag_junk_is_not_echoed(world, monkeypatch, capsys):
    junk = random_secret()
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--profile", junk, "--distribution", str(world.dist)])
    assert rc == 2 and no_window(out + err, junk)


def test_a_menu_prompt_never_prints_what_was_typed(world, monkeypatch, capsys):
    junk = random_secret()
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(world.dist)],
                           ["start", "", "scan", junk, junk, "skip", "continue", "none", "done", "do"])
    assert rc == 0 and "Type a number from the list." in out and no_window(out + err, junk)


def test_ctrl_c_at_startup_import_is_a_clean_stop(world, monkeypatch, capsys):
    import importlib.abc
    name = "trading_agent.wizard.flow"

    class Boom(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == name:
                raise KeyboardInterrupt
    monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [Boom(), *sys.meta_path])
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(world.dist)])
    assert rc == 130 and err.strip() == cli.STOPPED and "Traceback" not in err


def test_cli_imports_nothing_heavy_at_module_load():
    code = ("import sys, trading_agent.cli; "
            "print(sorted(m for m in ('jsonschema', 'yaml', 'trading_agent.wizard.flow', 'trading_agent.catalog.generate') "
            "if m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")})
    assert r.stdout.strip() == "[]", r.stderr


@pytest.mark.parametrize("argv,lines", [(["undo"], []), (["scan"], [])])
@pytest.mark.parametrize("then,code", [("interrupt", 130), ("eof", 1)])
def test_stop_line_is_neutral_outside_setup(world, monkeypatch, capsys, argv, lines, then, code):
    Receipt(profile_name="a").save(world.platform)
    Receipt(profile_name="b").save(world.platform)                # undo asks which one, scan is stopped by a patch
    if argv == ["scan"]:
        monkeypatch.setattr(cli, "_cmd_scan", lambda a: (_ for _ in ()).throw(
            KeyboardInterrupt if then == "interrupt" else EOFError))
    rc, out, err = run_cli(monkeypatch, capsys, argv, lines, then)
    assert rc == code and err.strip() == cli.STOPPED_NEUTRAL and "setup" not in err


def test_catalog_build_unwritable_out_gives_one_error_line(tmp_path, capsys):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{}")
    (tmp_path / "adir").mkdir()
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        for out in (tmp_path / "adir", ro / "o.json", tmp_path / "afile" / "o.json"):
            (tmp_path / "afile").write_text("x")
            rc = cli.main(["catalog", "build", "--catalog-root", str(FIXTURE_CATALOG), "--out", str(out),
                           "--overlay", str(overlay)])
            err = capsys.readouterr().err
            assert rc == 1 and err.startswith("error:") and err.count("\n") == 1 and "Traceback" not in err
            assert list(tmp_path.glob("**/.*.tmp")) == []
    finally:
        ro.chmod(0o700)


def test_catalog_build_writes_atomically_and_leaves_no_temp_file(tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{}")
    out = tmp_path / "o.json"
    out.write_text("OLD")
    assert cli.main(["catalog", "build", "--catalog-root", str(FIXTURE_CATALOG), "--out", str(out),
                     "--overlay", str(overlay)]) == 0
    assert json.loads(out.read_text())["entries"] and sorted(p.name for p in tmp_path.iterdir()) == ["o.json", "overlay.json"]


def test_a_failed_build_keeps_the_old_output_file(tmp_path, capsys):
    out = tmp_path / "o.json"
    out.write_text("OLD")
    assert cli.main(["catalog", "build", "--catalog-root", str(tmp_path), "--out", str(out)]) == 1
    assert out.read_text() == "OLD"
    capsys.readouterr()


def test_catalog_build_deeply_nested_overlay_gives_an_error_line(tmp_path, capsys):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("[" * 200000 + "]" * 200000)
    rc = cli.main(["catalog", "build", "--catalog-root", str(FIXTURE_CATALOG), "--out", str(tmp_path / "o.json"),
                   "--overlay", str(overlay)])
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("error:") and err.count("\n") == 1 and "Traceback" not in err


@pytest.mark.parametrize("make", ["file", "empty", "nodist"])
def test_distribution_override_that_is_not_a_distribution_gets_its_own_line(world, monkeypatch, capsys, tmp_path, make):
    target = tmp_path / "d"
    if make == "file":
        target.write_text("x")
    else:
        target.mkdir()
        if make == "nodist":
            (target / "SOUL.md").write_text("x")
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(target)])
    assert rc == 2 and err.strip() == cli.NOT_A_DISTRIBUTION and "no distribution.yaml" in err
    assert cli.NOT_A_DISTRIBUTION != cli.NOT_IN_BUILD


def test_scan_masks_a_key_shaped_mcp_server_name(world, real_detectors, monkeypatch, capsys):
    key = "sk-" + "AbCd1234" * 5
    (Path(os.environ["HOME"]) / ".claude.json").write_text(json.dumps(
        {"mcpServers": {key: {"command": "node", "args": ["srv.js"]}}}))
    rc, out, err = run_cli(monkeypatch, capsys, ["scan"])
    assert rc == 0 and "claude" in out.lower() and key not in out + err and no_window(out + err, key[3:])
    rc, out, err = run_cli(monkeypatch, capsys, ["scan", "--json"])
    assert key not in out


# ---------------------------------------------------------------- fix round 2

def _interrupted_at_ai_route(world, monkeypatch, capsys):
    rc, _, _ = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(world.dist)], ["start", "", "scan"])
    assert rc == 1
    state = json.loads((world.config_home / "trading-agent" / "state.json").read_text())
    assert state["current"] == "AI_ROUTE" and state["decisions"]["profile_name"] == "trader"


def test_resumed_setup_with_a_different_profile_flag_starts_over_with_a_warning(world, monkeypatch, capsys):
    _interrupted_at_ai_route(world, monkeypatch, capsys)
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--profile", "other", "--distribution", str(world.dist)],
                           ["start", "scan", "skip", "continue", "none", "done", "do"])
    assert rc == 0 and world.pdir("other").is_dir() and not world.pdir("trader").exists()
    assert "Starting a new setup for other. The saved setup for trader was not finished and is left alone." in out
    assert "trading-agent undo --profile other" in out


def test_resumed_setup_with_the_same_profile_flag_just_resumes(world, monkeypatch, capsys):
    _interrupted_at_ai_route(world, monkeypatch, capsys)
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--profile", "trader", "--distribution", str(world.dist)],
                           ["skip", "continue", "none", "done", "do"])
    assert rc == 0 and world.pdir("trader").is_dir() and "Starting a new setup" not in out


def test_a_differing_profile_flag_refuses_when_the_saved_setup_is_mid_write(world, monkeypatch, capsys):
    _interrupted_at_ai_route(world, monkeypatch, capsys)
    path = world.config_home / "trading-agent" / "state.json"
    doc = json.loads(path.read_text())
    doc["current"] = "WRITE"
    path.write_text(json.dumps(doc))
    before = path.read_text()
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--profile", "other", "--distribution", str(world.dist)])
    assert rc == 2 and err.strip() == cli.RESUME_CONFLICT.format(old="trader") and out == ""
    assert path.read_text() == before and not world.pdir("other").exists()


def test_catalog_build_write_is_atomic(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay.json"
    overlay.write_text("{}")
    out = tmp_path / "o.json"
    out.write_bytes(b"OLD-CONTENT")

    def refuse(*_a, **_k):
        raise OSError("replace refused")
    monkeypatch.setattr(os, "replace", refuse)
    rc = cli.main(["catalog", "build", "--catalog-root", str(FIXTURE_CATALOG), "--out", str(out),
                   "--overlay", str(overlay)])
    assert rc == 1 and out.read_bytes() == b"OLD-CONTENT"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.json", "overlay.json"]


@pytest.mark.parametrize("argv", [["scan"], ["catalog", "build", "--catalog-root", "x", "--out", "y"]])
def test_unexpected_tail_mentions_undo_only_for_setup(world, monkeypatch, capsys, argv):
    monkeypatch.setattr(cli, "_cmd_scan", lambda a: 1 / 0)
    monkeypatch.setattr(cli, "_cmd_catalog_build", lambda a: 1 / 0)
    rc, out, err = run_cli(monkeypatch, capsys, argv)
    assert rc == 1 and err.strip() == cli.UNEXPECTED_PLAIN.format(kind="ZeroDivisionError")
    monkeypatch.setattr(cli, "_cmd_setup", lambda a: 1 / 0)
    rc, out, err = run_cli(monkeypatch, capsys, ["setup"])
    assert "trading-agent undo" in err and "ZeroDivisionError" in err


# ---------------------------------------------------------------- export and import

def _set_up_with_memory(world, monkeypatch, capsys):
    assert setup(world, monkeypatch, capsys)[0] == 0
    (world.pdir() / "memories" / "MEMORY.md").write_text("remember: trim AAPL at 200\n")


def test_export_picks_the_only_receipt_and_prints_the_summary(world, monkeypatch, capsys):
    _set_up_with_memory(world, monkeypatch, capsys)
    out = world.root / "agent.tgz"
    rc, so, se = run_cli(monkeypatch, capsys, ["export", "-o", str(out)])
    assert rc == 0 and se == ""
    assert "This contains: memories, config, SOUL.md." in so
    assert "It does not contain: keys, sign-ins." in so
    assert "ALPACA_API_KEY" in so and "ALPACA_SECRET_KEY" in so
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    blob = out.read_bytes()
    for s in (KEY_ID, SECRET_KEY):
        assert s.encode() not in blob and s not in so + se


def test_export_files_writes_a_folder(world, monkeypatch, capsys):
    _set_up_with_memory(world, monkeypatch, capsys)
    rc, so, _ = run_cli(monkeypatch, capsys, ["export", "--files", str(world.root / "f")])
    assert rc == 0 and (world.root / "f" / "memories" / "MEMORY.md").is_file() and "This contains:" in so


def test_export_output_and_files_are_mutually_exclusive(world, monkeypatch, capsys):
    rc, _, se = run_cli(monkeypatch, capsys, ["export", "-o", "a.tgz", "--files", "d"])
    assert rc == 2 and "not allowed with" in se


@pytest.mark.parametrize("argv", [["export", "--profile", "Bad/Name"], ["import", "x.tgz", "--name", "Bad/Name"]])
def test_export_and_import_validate_names_without_echoing(world, monkeypatch, capsys, argv):
    rc, so, se = run_cli(monkeypatch, capsys, argv)
    assert rc == 2 and se.strip() == cli.BAD_NAME and "Bad/Name" not in so + se


def test_export_with_no_receipt_says_so_in_one_line(world, monkeypatch, capsys):
    rc, _, se = run_cli(monkeypatch, capsys, ["export"])
    assert rc == 1 and se.strip() == cli.NO_RECEIPT_EXPORT


def test_export_refuses_an_existing_file_in_one_line(world, monkeypatch, capsys):
    _set_up_with_memory(world, monkeypatch, capsys)
    out = world.root / "agent.tgz"
    out.write_text("mine")
    rc, _, se = run_cli(monkeypatch, capsys, ["export", "-o", str(out)])
    assert rc == 1 and len(se.strip().splitlines()) == 1 and out.read_text() == "mine"


def test_import_round_trip_through_the_cli_then_undo(world, monkeypatch, capsys):
    _set_up_with_memory(world, monkeypatch, capsys)
    out = world.root / "agent.tgz"
    assert run_cli(monkeypatch, capsys, ["export", "-o", str(out)])[0] == 0
    new_key = random_secret()
    rc, so, se = run_cli(monkeypatch, capsys, ["import", str(out), "--name", "moved"], ["y", new_key, "", ""])
    assert rc == 0, se
    assert (world.pdir("moved") / "memories" / "MEMORY.md").read_text() == "remember: trim AAPL at 200\n"
    assert dotenv_read(world.pdir("moved") / ".env") == {"ALPACA_API_KEY": new_key}
    assert "Left to finish later:" in so and "ALPACA_SECRET_KEY" in so
    assert "trading-agent undo --profile moved" in so
    assert no_window(so + se, new_key) and KEY_ID not in so + se
    rc, so, _ = run_cli(monkeypatch, capsys, ["undo", "--profile", "moved"], ["y"])
    assert rc == 0 and not world.pdir("moved").exists() and world.pdir().is_dir()


def test_import_of_a_hostile_file_is_one_plain_line(world, monkeypatch, capsys):
    bad = world.root / "bad.tgz"
    bad.write_bytes(b"\x1b[2Jnot a bundle")
    rc, so, se = run_cli(monkeypatch, capsys, ["import", str(bad)])
    assert rc == 1 and len(se.strip().splitlines()) == 1 and "Traceback" not in se and "\x1b" not in se
    rc, _, se = run_cli(monkeypatch, capsys, ["import", str(world.root / "missing.tgz")])
    assert rc == 1 and len(se.strip().splitlines()) == 1


@pytest.mark.parametrize("then,code", [("interrupt", 130), ("eof", 1)])
def test_import_stopped_at_a_prompt_creates_nothing(world, monkeypatch, capsys, then, code):
    _set_up_with_memory(world, monkeypatch, capsys)
    out = world.root / "agent.tgz"
    run_cli(monkeypatch, capsys, ["export", "-o", str(out)])
    rc, _, se = run_cli(monkeypatch, capsys, ["import", str(out), "--name", "moved"], ["y"], then)
    assert rc == code and se.strip() == cli.STOPPED_NEUTRAL and not world.pdir("moved").exists()


@pytest.mark.parametrize("argv", [["export"], ["import", "x.tgz"]])
def test_export_and_import_unexpected_errors_print_only_the_type(world, monkeypatch, capsys, argv):
    from trading_agent import bundle
    monkeypatch.setattr(bundle, "export", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(bundle, "import_bundle", lambda *a, **k: 1 / 0)
    Receipt(profile_name="trader").save(world.platform)
    rc, _, se = run_cli(monkeypatch, capsys, argv)
    assert rc == 1 and se.strip() == cli.UNEXPECTED_PLAIN.format(kind="ZeroDivisionError")


@pytest.mark.parametrize("argv", [["export"], ["import"]])
def test_export_and_import_help(world, monkeypatch, capsys, argv):
    rc = cli.main([*argv, "--help"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("usage: trading-agent")


def test_export_soft_warning_defaults_to_no_and_exits_0(world, monkeypatch, capsys):
    _set_up_with_memory(world, monkeypatch, capsys)
    (world.pdir() / "memories" / "hash.md").write_text("commit 0123456789abcdef0123456789abcdef01234567\n")
    out = world.root / "agent.tgz"
    rc, so, se = run_cli(monkeypatch, capsys, ["export", "-o", str(out)], [""])
    assert rc == 0 and "memories/hash.md" in so and "[y/N]" in so and not out.exists()
    rc, so, _ = run_cli(monkeypatch, capsys, ["export", "-o", str(out)], ["y"])
    assert rc == 0 and out.is_file()
