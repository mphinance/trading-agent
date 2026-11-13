"""Installs the real distribution with the real hermes into a scratch HERMES_HOME.

Opt in with TRADING_AGENT_REAL_HERMES=1. The scratch home lives under tmp; the real ~/.hermes is never used."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("TRADING_AGENT_REAL_HERMES") != "1",
                       reason="set TRADING_AGENT_REAL_HERMES=1 to run against the real hermes"),
]


def _hermes(home: Path, *args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("hermes")
    if exe is None:
        pytest.skip("hermes is not installed")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    env["HOME"] = str(home / "home")
    env["HERMES_HOME"] = str(home / "hermes")
    return subprocess.run([exe, *args], capture_output=True, text=True, env=env, timeout=600)


def ready_scratch_hermes(home: Path, hermes_home: Path) -> None:
    """Hermes 0.23.1 keeps its dependency environment per data root, so a scratch HERMES_HOME needs its own before
    any command runs (it refuses with "run `hermes pm repair`"). The repair writes only under the scratch root."""
    exe = shutil.which("hermes")
    if exe is None:
        pytest.skip("hermes is not installed")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    env.update(HOME=str(home), HERMES_HOME=str(hermes_home))
    probe = subprocess.run([exe, "--version"], capture_output=True, text=True, env=env, timeout=120)
    if "pm repair" in probe.stdout + probe.stderr:
        fixed = subprocess.run([exe, "pm", "repair"], capture_output=True, text=True, env=env, timeout=600)
        assert fixed.returncode == 0, fixed.stdout + fixed.stderr


def _clean_checkout(dest: Path) -> Path:
    """What a git clone of the repo would hold: tracked and unignored files only. Installing the working tree
    directly fails when it holds a .venv, because Hermes refuses symlinks in a distribution."""
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.splitlines()
    for f in files:
        src = ROOT / f
        if src.is_file() and not src.is_symlink():
            (dest / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / f)
    return dest


def test_install_then_update_preserves_a_user_edited_config(tmp_path):
    (tmp_path / "home").mkdir()
    ready_scratch_hermes(tmp_path / "home", tmp_path / "hermes")
    src = _clean_checkout(tmp_path / "checkout")
    r = _hermes(tmp_path, "profile", "install", str(src), "--name", "tatest", "-y")
    assert r.returncode == 0 and "Installed 'tatest'" in r.stdout, r.stdout + r.stderr
    found = re.search(r"^\s*Profile path:\s*(.+)$", r.stdout, re.M)
    assert found, r.stdout
    prof = Path(found.group(1).strip())
    assert tmp_path in prof.parents, prof          # the scratch home, never the real one
    assert (prof / "SOUL.md").is_file()
    cfg = prof / "config.yaml"
    assert cfg.is_file()                            # shipped defaults really land (needs config.yaml in distribution_owned)
    cfg.write_text(cfg.read_text() + "\nuser_setting: kept\n")
    u = _hermes(tmp_path, "profile", "update", "tatest", "-y")
    assert u.returncode == 0 and "Updated 'tatest'" in u.stdout, u.stdout + u.stderr
    assert "user_setting: kept" in cfg.read_text()


def test_the_recorded_source_is_the_folder_installed_from_and_update_keeps_it(tmp_path):
    """The packaged folder of an installed wheel is a real directory, so Hermes records exactly that path and
    `profile update` can come back to it (no temporary copy in between)."""
    (tmp_path / "home").mkdir()
    ready_scratch_hermes(tmp_path / "home", tmp_path / "hermes")
    src = _clean_checkout(tmp_path / "checkout")
    r = _hermes(tmp_path, "profile", "install", str(src), "--name", "tasrc", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    prof = Path(re.search(r"^\s*Profile path:\s*(.+)$", r.stdout, re.M).group(1).strip())
    manifest = yaml.safe_load((prof / "distribution.yaml").read_text())
    assert "".join(str(manifest["source"]).split()) == "".join(str(src).split())   # Hermes may wrap a long line
    u = _hermes(tmp_path, "profile", "update", "tasrc", "-y")
    assert u.returncode == 0 and "Updated 'tasrc'" in u.stdout, u.stdout + u.stderr
