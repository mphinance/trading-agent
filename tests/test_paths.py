"""Which folder setup hands to `hermes profile install`, and whether Hermes can come back to it later.

Hermes records the folder it installed from as `source:` in the profile's distribution.yaml, and
`hermes profile update` reads it back. So an installed wheel must be installed from directly (a durable path
that `uv tool upgrade` refreshes), and a throwaway extracted copy must be flagged and cleaned up."""
from __future__ import annotations

import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from test_cli import HAPPY, run_cli, world  # noqa: F401  (the shim world fixture)
from trading_agent import cli, paths

ROOT = Path(__file__).parents[1]
FILES = ("distribution.yaml", "SOUL.md", "config.yaml")


def _real_dist(root: Path) -> Path:
    root.mkdir(parents=True)
    for n in FILES:
        (root / n).write_text("x\n")
    return root


def _zip_root(tmp_path, monkeypatch, src: Path | None = None):
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as f:
        for n in FILES:
            f.writestr(f"d/{n}", (src / n).read_text() if src else "x\n")
    monkeypatch.setattr(paths, "_packaged_root", lambda: zipfile.Path(z, "d/"))
    monkeypatch.setattr(paths, "_checkout_root", lambda: tmp_path / "absent")


def test_an_installed_wheel_is_used_directly_with_no_temp_copy(monkeypatch, tmp_path):
    site = _real_dist(tmp_path / "site-packages" / "trading_agent" / "data" / "distribution")
    monkeypatch.setattr(paths, "_packaged_root", lambda: site)
    monkeypatch.setattr(paths.tempfile, "mkdtemp", lambda **_: pytest.fail("a temp copy was made"))
    d = paths.distribution_dir()
    assert d == site and paths.source_is_durable(d)


def test_the_checkout_fallback_returns_the_repo_files_and_counts_as_durable(monkeypatch, tmp_path):
    checkout = _real_dist(tmp_path / "clone")
    monkeypatch.setattr(paths, "_packaged_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(paths, "_checkout_root", lambda: checkout)
    d = paths.distribution_dir()
    assert d == checkout and paths.source_is_durable(d)


def test_an_extracted_zip_copy_is_not_a_durable_source(monkeypatch, tmp_path):
    _zip_root(tmp_path, monkeypatch)
    d = paths.distribution_dir()
    assert d is not None and d != tmp_path and not paths.source_is_durable(d)
    other = _real_dist(tmp_path / "elsewhere")
    assert paths.source_is_durable(other)          # only our own temp copies are flagged


@pytest.mark.parametrize("ending", ["pass", "raise RuntimeError('boom')", "raise KeyboardInterrupt", "sys.exit(3)"])
def test_the_temp_copy_is_removed_on_every_exit_path(tmp_path, ending):
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as f:
        for n in FILES:
            f.writestr(f"d/{n}", "x\n")
    marker = tmp_path / "where.txt"
    code = textwrap.dedent(f"""
        import sys, zipfile
        from pathlib import Path
        from trading_agent import paths
        paths._packaged_root = lambda: zipfile.Path({str(z)!r}, "d/")
        paths._checkout_root = lambda: Path("/nonexistent-x")
        d = paths.distribution_dir()
        Path({str(marker)!r}).write_text(str(d))
        {ending}
    """)
    subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60,
                   env={"PYTHONPATH": str(ROOT / "src")})
    gone = Path(marker.read_text())
    assert gone.name.startswith("trading-agent-dist-") and not gone.exists()


# ---------------------------------------------------------------- what setup tells the user

def test_setup_says_so_when_the_source_will_not_survive(world, monkeypatch, capsys, tmp_path):
    _zip_root(tmp_path, monkeypatch, world.dist)
    rc, out, err = run_cli(monkeypatch, capsys, ["setup"], HAPPY)
    assert rc == 0
    assert cli.SOURCE_NOT_DURABLE in out + err and "docs/UPDATING.md" in cli.SOURCE_NOT_DURABLE
    assert "\u2014" not in cli.SOURCE_NOT_DURABLE and "\u2013" not in cli.SOURCE_NOT_DURABLE


def test_setup_from_a_durable_folder_stays_quiet_about_it(world, monkeypatch, capsys):
    rc, out, err = run_cli(monkeypatch, capsys, ["setup", "--distribution", str(world.dist)], HAPPY)
    assert rc == 0 and cli.SOURCE_NOT_DURABLE not in out + err


def test_undo_after_a_zip_install_removes_the_profile(world, monkeypatch, capsys, tmp_path):
    _zip_root(tmp_path, monkeypatch, world.dist)
    assert run_cli(monkeypatch, capsys, ["setup"], HAPPY)[0] == 0
    assert world.pdir().is_dir()
    rc, out, err = run_cli(monkeypatch, capsys, ["undo", "--profile", "trader"], ["y"])
    assert rc == 0 and not world.pdir().exists()
