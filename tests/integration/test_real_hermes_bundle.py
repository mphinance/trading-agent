"""Export, delete, import with the real hermes in a scratch HERMES_HOME and a scratch HOME.

Opt in with TRADING_AGENT_REAL_HERMES=1. Nothing here touches the real ~/.hermes or ~/.config."""
from __future__ import annotations

import gzip
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from fakes import ScriptedIO
from test_cli import no_window, random_secret
from test_profile_writer import TmpPlatform
from test_real_hermes_install import _clean_checkout
from trading_agent import bundle as B
from trading_agent.profile.hermes_cli import HermesCli
from trading_agent.profile.receipt import Receipt
from trading_agent.profile.writer import WritePlan, rollback, write_profile
from trading_agent.secrets import choose_store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("TRADING_AGENT_REAL_HERMES") != "1",
                       reason="set TRADING_AGENT_REAL_HERMES=1 to run against the real hermes"),
]


def test_real_export_delete_import_round_trip(tmp_path):
    if shutil.which("hermes") is None:
        pytest.skip("hermes is not installed")
    home, hermes_home, config = tmp_path / "home", tmp_path / "hermes", tmp_path / "config"
    for d in (home, hermes_home, config):
        d.mkdir()
    platform = TmpPlatform(home, config, {
        "PATH": os.environ.get("PATH", ""), "HOME": str(home), "HERMES_HOME": str(hermes_home),
        "XDG_CONFIG_HOME": str(config)})
    hermes = HermesCli(platform)
    store = lambda d: choose_store(platform, d)  # noqa: E731
    secret = random_secret()

    dist = _clean_checkout(tmp_path / "checkout")
    receipt = write_profile(WritePlan(profile_name="tabundle", ai=None, broker=None, skills=[],
                                      env_values={"MY_DATA_KEY": secret}),
                            hermes=hermes, source_dir=dist, store_factory=store)
    pdir = hermes.profile_dir("tabundle")
    assert hermes_home in pdir.parents
    (pdir / "memories").mkdir(exist_ok=True)
    (pdir / "memories" / "MEMORY.md").write_text("remember: the round trip worked\n")

    out = tmp_path / "out" / "tabundle.tgz"
    out.parent.mkdir()
    path, manifest = B.export("tabundle", out, hermes=hermes, receipt=receipt)
    assert "MY_DATA_KEY" in manifest.env_names_needed
    with tarfile.open(path, "r:gz") as tar:
        assert sorted(m.name for m in tar.getmembers()) == ["bundle.json", "profile.tar.gz"]
    raw = path.read_bytes()
    assert no_window((raw + gzip.decompress(raw)).decode("latin-1"), secret)

    assert rollback(receipt, hermes=hermes, store_factory=store)
    assert not pdir.exists() and not hermes.profile_exists("tabundle")

    io = ScriptedIO([True, ""])                       # import it, skip the key
    imported = B.import_bundle(path, hermes=hermes, name=None, io=io, store_factory=store)
    assert imported is not None and imported.profile_name == "tabundle"
    assert (pdir / "memories" / "MEMORY.md").read_text() == "remember: the round trip worked\n"
    env_file = pdir / ".env"
    assert not env_file.exists() or "MY_DATA_KEY" not in env_file.read_text()
    for p in pdir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert no_window(p.read_bytes().decode("latin-1"), secret), p.name
    assert no_window(io.everything(), secret)
    assert Receipt.load(platform, "tabundle").run_id == imported.run_id

    assert rollback(imported, hermes=hermes, store_factory=store)      # what undo does
    assert not pdir.exists()
    assert not (home / ".local" / "bin" / "tabundle").exists()
