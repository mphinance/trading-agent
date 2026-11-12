from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from trading_agent.catalog import CatalogError
from trading_agent.catalog import generate
from trading_agent.catalog.generate import build_catalog, build_generated_layer
from trading_agent.catalog.merge import BANNER, merge_overlay

FIXTURE = Path(__file__).parent / "fixtures" / "catalog"
TODAY = date(2026, 11, 12)
SEED_OVERLAY = Path(__file__).parents[1] / "catalog" / "overlay.json"


def by_slug(entries):
    return {e["slug"]: e for e in entries}


@pytest.fixture
def cat(tmp_path):
    root = tmp_path / "catalog"
    shutil.copytree(FIXTURE, root)
    return root


def set_key(root, rel, key, value):
    p = root / rel
    text = p.read_text()
    p.write_text(re.sub(rf"^{key}:.*$", f"{key}: {value}", text, count=1, flags=re.M))


def test_status_split(cat):
    e = by_slug(build_generated_layer(cat, TODAY))["etoro"]
    assert e["status"] == ["official", "community"]


def test_status_with_inline_comment_in_file(cat):
    set_key(cat, "brokers/alpaca.md", "status", "official + community # note")
    assert by_slug(build_generated_layer(cat, TODAY))["alpaca"]["status"] == [
        "official", "community"]


def test_unknown_status_token_fails_naming_file(cat):
    set_key(cat, "brokers/alpaca.md", "status", "banana")
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md"):
        build_generated_layer(cat, TODAY)


def test_unknown_token_inside_combined_status_fails(cat):
    set_key(cat, "brokers/etoro.md", "status", "official + banana")
    with pytest.raises(CatalogError, match=r"brokers/etoro\.md"):
        build_generated_layer(cat, TODAY)


def test_missing_front_matter_fails(cat):
    (cat / "brokers" / "alpaca.md").write_text("# no front matter\n")
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md"):
        build_generated_layer(cat, TODAY)


def test_future_last_verified_fails(cat):
    set_key(cat, "brokers/alpaca.md", "last_verified", "2026-11-13")
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md.*future"):
        build_generated_layer(cat, TODAY)


def test_today_last_verified_ok(cat):
    set_key(cat, "brokers/alpaca.md", "last_verified", "2026-11-12")
    build_generated_layer(cat, TODAY)


@pytest.mark.parametrize("bad", ["pending", "2026-1-5", "2026-02-30", "yesterday"])
def test_malformed_last_verified_fails(cat, bad):
    set_key(cat, "brokers/alpaca.md", "last_verified", bad)
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md"):
        build_generated_layer(cat, TODAY)


def test_trading_must_be_true_false_or_draft_only(cat):
    set_key(cat, "brokers/alpaca.md", "trading", "maybe")
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md.*trading"):
        build_generated_layer(cat, TODAY)


def test_missing_required_key_fails_naming_file(cat):
    p = cat / "brokers" / "alpaca.md"
    p.write_text(re.sub(r"^source_url:.*\n", "", p.read_text(), flags=re.M))
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md.*source_url"):
        build_generated_layer(cat, TODAY)


def test_route_kinds_from_server_type(cat):
    e = by_slug(build_generated_layer(cat, TODAY))
    assert e["alpaca"]["route_kinds"] == ["remote", "local"]
    assert e["etoro"]["route_kinds"] == ["local"]
    assert e["m1-finance"]["route_kinds"] == ["none"]
    assert e["connecttrade"]["route_kinds"] == ["unknown"]


def test_url_fragment_survives_into_entry(cat):
    assert by_slug(build_generated_layer(cat, TODAY))["alpaca"]["source_url"].endswith("#mcp")


def test_readme_only_stubs_generated(cat):
    entries = build_generated_layer(cat, TODAY)
    stubs = [e for e in entries if e["directory"] == "readme-only"]
    assert sorted(s["slug"] for s in stubs) == ["jp-morgan-chase", "sofi"]
    for s in stubs:
        assert s["status"] == ["none"]
        assert s["trading"] == "false"
        assert s["route_kinds"] == ["none"]
        assert s["source"] == "readme-only"
        assert "last_verified" not in s and "source_url" not in s
    assert by_slug(entries)["sofi"]["name"] == "SoFi"
    # linked row (M1 Finance) is a file entry, not a stub
    assert by_slug(entries)["m1-finance"]["source"] == "front-matter"
    assert len(entries) == 3 + 1 + 2


def test_no_readme_means_no_stubs(cat):
    (cat / "readme.md").unlink()
    assert len(build_generated_layer(cat, TODAY, strict_readme=False)) == 4


def test_unreviewed_entry_not_wireable(cat):
    merged = by_slug(merge_overlay(build_generated_layer(cat, TODAY), {}))
    for e in merged.values():
        assert e["routes"] == []
        assert e["wizard"]["banner"] == BANNER
    assert merged["alpaca"]["wizard"]["selectable"] is True
    assert merged["etoro"]["wizard"]["selectable"] is True
    assert merged["m1-finance"]["wizard"]["selectable"] is False
    assert merged["connecttrade"]["wizard"]["selectable"] is False
    assert merged["sofi"]["wizard"]["selectable"] is False


def test_overlay_entry_has_no_banner_and_selectable_from_overlay(cat):
    ov = {"alpaca": {"routes": [], "wizard": {"selectable": False}},
          "etoro": {"routes": []}}
    merged = by_slug(merge_overlay(build_generated_layer(cat, TODAY), ov))
    assert merged["alpaca"]["wizard"] == {"selectable": False}
    assert merged["etoro"]["wizard"] == {"selectable": True}


def test_overlay_slug_without_source_fails(cat):
    with pytest.raises(CatalogError, match="ghost"):
        merge_overlay(build_generated_layer(cat, TODAY), {"ghost": {"routes": []}})


def test_overlay_cannot_override_generated_fields(cat):
    with pytest.raises(CatalogError, match="alpaca"):
        merge_overlay(build_generated_layer(cat, TODAY), {"alpaca": {"status": ["none"]}})


def test_merged_sorted_by_slug(cat):
    slugs = [e["slug"] for e in build_catalog(cat, {}, TODAY)]
    assert slugs == sorted(slugs)


def test_build_catalog_validates_every_entry(cat):
    ov = {"alpaca": {"routes": [{"route_id": "x", "kind": "bogus", "maintainer": "official"}]}}
    with pytest.raises(CatalogError, match=r"alpaca: routes\.0\.kind"):
        build_catalog(cat, ov, TODAY)


def test_main_writes_container(cat, tmp_path):
    out = tmp_path / "out" / "catalog.json"
    ov = tmp_path / "overlay.json"
    ov.write_text("{}")
    assert generate.main(["--catalog-root", str(cat), "--out", str(out),
                          "--overlay", str(ov)]) == 0
    doc = json.loads(out.read_text())
    assert set(doc) == {"generated_at", "catalog_commit", "entries"}
    assert doc["catalog_commit"] is None  # fixture copy is not a git repo
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", doc["generated_at"])
    assert len(doc["entries"]) == 6


def test_main_reports_error_and_fails(cat, tmp_path, capsys):
    set_key(cat, "brokers/alpaca.md", "status", "banana")
    ov = tmp_path / "overlay.json"
    ov.write_text("{}")
    rc = generate.main(["--catalog-root", str(cat), "--out", str(tmp_path / "o.json"),
                        "--overlay", str(ov)])
    assert rc == 1
    assert "brokers/alpaca.md" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


SECRET_SHAPES = [re.compile(r"sk-[A-Za-z0-9_-]{8,}"), re.compile(r"[A-Za-z0-9+/=_-]{33,}"),
                 re.compile(r"\b[0-9a-fA-F]{32,}\b")]


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def assert_no_secret_shapes(obj):
    for s in strings(obj):
        for rx in SECRET_SHAPES:
            assert not rx.search(s), "secret-shaped string in catalog data"


def test_seed_overlay_has_no_secret_shapes_and_valid_env_names():
    overlay = json.loads(SEED_OVERLAY.read_text())
    assert set(overlay) >= {"alpaca", "tradier"}
    assert_no_secret_shapes(overlay)
    for ov in overlay.values():
        for route in ov["routes"]:
            for var in route["auth"]["env_vars"]:
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*", var["name"])
                assert set(var) <= {"name", "role", "required", "secret"}


def test_generated_json_has_no_secret_shapes(cat):
    overlay = json.loads(SEED_OVERLAY.read_text())
    overlay = {k: v for k, v in overlay.items() if k in {"alpaca"}}
    assert_no_secret_shapes(build_catalog(cat, overlay, TODAY))


def test_seed_overlay_validates_on_fixture(cat):
    overlay = {k: v for k, v in json.loads(SEED_OVERLAY.read_text()).items() if k == "alpaca"}
    merged = by_slug(build_catalog(cat, overlay, TODAY))
    assert "banner" not in merged["alpaca"]["wizard"]
    assert merged["alpaca"]["routes"][0]["route_id"] == "local"


def test_strict_missing_readme_fails(cat):
    (cat / "readme.md").unlink()
    with pytest.raises(CatalogError, match=r"readme\.md"):
        build_generated_layer(cat, TODAY)


def test_strict_section_absent_fails(cat):
    r = cat / "readme.md"
    r.write_text(r.read_text().replace("brokers with no MCP route", "brokers without a route"))
    with pytest.raises(CatalogError, match=r"readme\.md.*section"):
        build_generated_layer(cat, TODAY)
    assert len(build_generated_layer(cat, TODAY, strict_readme=False)) == 4


def test_strict_count_mismatch_fails_with_both_numbers(cat):
    r = cat / "readme.md"
    r.write_text(r.read_text().replace("<b>3 brokers", "<b>9 brokers"))
    with pytest.raises(CatalogError, match=r"readme\.md.*9.*3"):
        build_generated_layer(cat, TODAY)


def test_strict_count_mismatch_when_row_dropped(cat):
    r = cat / "readme.md"
    r.write_text(re.sub(r"^\| \*\*SoFi.*\n", "", r.read_text(), flags=re.M))
    with pytest.raises(CatalogError, match=r"readme\.md.*3.*2"):
        build_generated_layer(cat, TODAY)


def test_non_utf8_file_raises_catalog_error_naming_file(cat):
    (cat / "brokers" / "alpaca.md").write_bytes(b"---\nname: \xff\xfe\n---\n")
    with pytest.raises(CatalogError, match=r"brokers/alpaca\.md"):
        build_generated_layer(cat, TODAY)


def test_directory_named_md_is_skipped(cat):
    (cat / "brokers" / "weird.md").mkdir()
    assert "weird" not in by_slug(build_generated_layer(cat, TODAY))


def test_symlinked_md_is_rejected(cat, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text((cat / "brokers" / "alpaca.md").read_text())
    (cat / "brokers" / "linked.md").symlink_to(target)
    with pytest.raises(CatalogError, match=r"brokers/linked\.md.*symlink"):
        build_generated_layer(cat, TODAY)


@pytest.mark.parametrize("bad,needle", [
    ({"alpaca": []}, "alpaca"),
    ([], "overlay"),
    ({"alpaca": {"routes": {}}}, "alpaca.*routes"),
    ({"alpaca": {"wizard": []}}, "alpaca.*wizard"),
    ({"alpaca": {"overlay_reviewed_on": 5}}, "alpaca.*overlay_reviewed_on"),
])
def test_overlay_shape_errors_name_slug(cat, bad, needle):
    with pytest.raises(CatalogError, match=needle):
        merge_overlay(build_generated_layer(cat, TODAY), bad)


def _run_main(cat, tmp_path, overlay_text="{}"):
    ov = tmp_path / "overlay.json"
    ov.write_text(overlay_text)
    return generate.main(["--catalog-root", str(cat), "--out", str(tmp_path / "o.json"),
                          "--overlay", str(ov)])


def test_main_exit1_one_line_for_each_failure(cat, tmp_path, capsys):
    (cat / "brokers" / "alpaca.md").write_bytes(b"---\nname: \xff\n---\n")
    assert _run_main(cat, tmp_path) == 1
    err = capsys.readouterr().err
    assert "brokers/alpaca.md" in err and err.count("\n") == 1 and "Traceback" not in err
    (cat / "brokers" / "alpaca.md").write_text(
        (FIXTURE / "brokers" / "alpaca.md").read_text())
    for text in ('{"alpaca": []}', "[]"):
        assert _run_main(cat, tmp_path, text) == 1
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and "Traceback" not in err
    (cat / "readme.md").unlink()
    assert _run_main(cat, tmp_path) == 1
    assert "readme.md" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("AWESOME_BROKER_MCP"),
                    reason="set AWESOME_BROKER_MCP to the awesome-broker-mcp checkout")
def test_real_catalog_counts():
    root = Path(os.environ["AWESOME_BROKER_MCP"])
    overlay = json.loads(SEED_OVERLAY.read_text())
    entries = build_catalog(root, overlay)  # also validates every entry
    files = [e for e in entries if e["source"] == "front-matter"]
    assert sum(e["directory"] == "brokers" for e in files) == 76
    assert sum(e["directory"] == "aggregators" for e in files) == 14
    assert sum(e["directory"] == "readme-only" for e in entries) == 4
    assert len(entries) == 94
    assert sum("official" in e["status"] for e in entries) == 64
    assert sum(e["status"] == ["none"] for e in entries) == 12
