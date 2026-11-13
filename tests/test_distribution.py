"""The shipped Hermes distribution: three files at the repo root, force-included into the wheel."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from test_cli import HAPPY, Env, sub_env
from trading_agent import cli, paths
from trading_agent.profile.hermes_cli import RESERVED_NAMES

ROOT = Path(__file__).parents[1]
SHIPPED = ("distribution.yaml", "SOUL.md", "config.yaml")


def test_distribution_yaml_valid():
    m = yaml.safe_load((ROOT / "distribution.yaml").read_text(encoding="utf-8"))
    assert m["name"] == "trading-agent" and m["name"] not in RESERVED_NAMES
    assert m["version"] == "0.1.0" and m["license"] == "MIT"
    assert m["hermes_requires"] == ">=0.23.0"
    assert m["env_requires"] == []
    # config.yaml must be listed or Hermes never installs it (verified on 0.23.1); update still keeps user edits
    assert m["distribution_owned"] == ["SOUL.md", "config.yaml", "distribution.yaml"]


def test_soul_has_no_trading_or_risk_language():
    text = (ROOT / "SOUL.md").read_text(encoding="utf-8").lower()
    assert text.strip() and not [w for w in ("risk", "order", "trade", "broker") if w in text]


def test_config_has_no_mcp_servers_or_secrets():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    assert "mcp_servers" not in cfg


def test_no_dashes_in_shipped_text():
    for name in SHIPPED:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, name


def test_no_mcp_json_shipped():
    assert not (ROOT / "mcp.json").exists()


# ---------------------------------------------------------------- lookup

def _fake_dist(d: Path) -> Path:
    d.mkdir(parents=True)
    (d / "distribution.yaml").write_text("name: x\n")
    return d


def test_lookup_prefers_the_packaged_files(monkeypatch, tmp_path):
    packaged = _fake_dist(tmp_path / "packaged")
    checkout = _fake_dist(tmp_path / "checkout")
    monkeypatch.setattr(paths, "_packaged_root", lambda: packaged)
    monkeypatch.setattr(paths, "_checkout_root", lambda: checkout)
    assert paths.distribution_dir() == packaged


def test_lookup_falls_back_to_the_checkout_root(monkeypatch, tmp_path):
    checkout = _fake_dist(tmp_path / "checkout")
    monkeypatch.setattr(paths, "_packaged_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(paths, "_checkout_root", lambda: checkout)
    assert paths.distribution_dir() == checkout


def test_lookup_is_none_when_neither_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_packaged_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(paths, "_checkout_root", lambda: tmp_path / "absent2")
    assert paths.distribution_dir() is None


def test_lookup_works_from_a_zipped_wheel_style_resource(monkeypatch, tmp_path):
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as f:
        for n in SHIPPED:
            f.writestr(f"trading_agent/data/distribution/{n}", (ROOT / n).read_text(encoding="utf-8"))
    monkeypatch.setattr(paths, "_packaged_root", lambda: zipfile.Path(z, "trading_agent/data/distribution/"))
    monkeypatch.setattr(paths, "_checkout_root", lambda: tmp_path / "absent")
    d = paths.distribution_dir()
    assert d is not None and sorted(p.name for p in d.iterdir()) == sorted(SHIPPED)
    shutil.rmtree(d)


def test_the_real_checkout_resolves_to_the_root_files():
    d = paths.distribution_dir()
    assert d is not None and (d / "distribution.yaml").is_file() and (d / "SOUL.md").is_file()


def test_the_checkout_fallback_is_the_repo_root():
    assert paths._checkout_root() == ROOT


# ---------------------------------------------------------------- wheel

def test_the_wheel_contains_the_distribution(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed, so the wheel cannot be built")
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "UV_CACHE_DIR", "XDG_CACHE_HOME")}
    try:
        r = subprocess.run([uv, "build", "--wheel", "--offline", "--out-dir", str(out), str(ROOT)],
                           capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        pytest.skip("uv build timed out")
    if r.returncode != 0:
        pytest.skip("uv could not build the wheel offline here")
    (wheel,) = out.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for n in SHIPPED:
        assert f"trading_agent/data/distribution/{n}" in names
    assert not any(n.endswith("mcp.json") for n in names)


def test_real_setup_with_no_override_reaches_the_wizard(tmp_path):
    w = Env(tmp_path)
    (tmp_path / "world" / "home").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "trading_agent", "setup"], input="start\n", capture_output=True,
                       text=True, env=sub_env(w), timeout=120)
    assert cli.NOT_IN_BUILD not in r.stderr
    assert r.returncode == 1 and r.stderr.strip() == cli.STOPPED


def test_declared_minimum_python_covers_the_tar_extraction_filter():
    """bundle.py passes filter="data" to extractall, which needs Python 3.11.4 or newer. Keep the pair in step."""
    import re
    import tomllib
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    m = re.fullmatch(r">=\s*(\d+)\.(\d+)(?:\.(\d+))?", declared.strip())
    assert m, f"unexpected requires-python form: {declared}"
    assert tuple(int(x or 0) for x in m.groups()) >= (3, 11, 4)
    src = (ROOT / "src" / "trading_agent" / "bundle.py").read_text(encoding="utf-8")
    assert 'extractall(stage, filter="data")' in src
