"""The committed catalog snapshot (package data): needs no env var and no local clone."""
from __future__ import annotations

import json
import re
from collections import Counter
from importlib import resources
from pathlib import Path

from trading_agent.catalog import generate
from trading_agent.catalog.merge import BANNER
from trading_agent.catalog.schema import validate_entry
from trading_agent.catalog.snapshot import load_catalog

DATA = resources.files("trading_agent").joinpath("data")


def _doc():
    return json.loads(DATA.joinpath("catalog.json").read_text(encoding="utf-8"))


def test_snapshot_validates_entry_by_entry_and_has_the_real_counts():
    entries = _doc()["entries"]
    for e in entries:
        validate_entry(e)
    assert len(entries) == 94
    assert Counter(e["directory"] for e in entries) == {"brokers": 76, "aggregators": 14, "readme-only": 4}
    assert sum("official" in e["status"] for e in entries) == 64
    assert sum(e["status"] == ["none"] for e in entries) == 12


def test_snapshot_installable_entries_are_exactly_the_overlay_slugs():
    """The overlay is the only source of routes. Other selectable entries are listed with the uncurated banner
    and no routes, so the wizard shows them but cannot install them (merge.py semantics from Task 6)."""
    overlay = json.loads(DATA.joinpath("overlay.json").read_text(encoding="utf-8"))
    entries = _doc()["entries"]
    installable = {e["slug"] for e in entries if e["wizard"]["selectable"] and e["routes"]}
    assert installable == set(overlay)
    for e in entries:
        if e["slug"] not in overlay:
            assert e["routes"] == [] and e["wizard"].get("banner") == BANNER


def test_snapshot_embeds_no_local_path():
    doc = _doc()
    text = json.dumps(doc)
    # a legitimate URL may contain /home/; a local file path may not
    assert all(text[max(0, m.start() - 8):m.start()].endswith(("com", "org", "io", "net")) or "http" in text[max(0, m.start() - 60):m.start()]
               for m in re.finditer("/home/", text))
    assert str(Path.home()) not in text and "\\Users\\" not in text
    assert set(doc) == {"generated_at", "catalog_commit", "entries"}
    assert doc["catalog_commit"] is None or len(doc["catalog_commit"]) == 40


def test_load_catalog_reads_the_packaged_snapshot():
    assert len(load_catalog()) == 94


def test_load_catalog_falls_back_to_empty_with_a_plain_notice(tmp_path, capsys):
    bad = tmp_path / "catalog.json"
    bad.write_text("{ not json")
    assert load_catalog(bad) == []
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "catalog" in err.lower() and str(tmp_path) not in err
    assert load_catalog(tmp_path / "missing.json") == []
    capsys.readouterr()


def test_default_overlay_is_the_packaged_copy():
    assert generate.default_overlay_text() == DATA.joinpath("overlay.json").read_text(encoding="utf-8")


def test_a_missing_default_overlay_warns_on_one_line_instead_of_silently_going_uncurated(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(generate, "_packaged_overlay", lambda: tmp_path / "nope.json")
    assert generate.default_overlay_text() is None
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "overlay" in err.lower() and str(tmp_path) not in err
