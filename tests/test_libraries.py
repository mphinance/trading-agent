from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest
import yaml

from trading_agent.libraries import Library, LibraryError, keys_needed, listed_only, load_libraries

REPO = Path(__file__).resolve().parent.parent

GOOD = {
    "id": "x-lib", "kind": "skills", "name": "X", "source_url": "https://example.com/x",
    "license": "MIT", "counts": {"n": 1}, "needs_keys": ["X_API_KEY"], "attribution": "a", "listed": True,
}


def write(root: Path, skills=(), tools=()) -> Path:
    for fname, entries in (("skills.yaml", skills), ("tools.yaml", tools)):
        (root / fname).write_text(yaml.safe_dump({"libraries": list(entries)}), encoding="utf-8")
    return root


def entry(**over):
    d = dict(GOOD)
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}


@pytest.fixture(scope="module")
def libs() -> list[Library]:
    return load_libraries()


def by_id(libs, lib_id) -> Library:
    return next(lib for lib in libs if lib.id == lib_id)


# ---- catalog behaviour ----

def test_packaged_data_loads_via_importlib_resources(libs):
    base = resources.files("trading_agent") / "data" / "libraries"
    assert (base / "skills.yaml").is_file() and (base / "tools.yaml").is_file()
    assert {lib.id for lib in libs} == {"alpha-skills", "broker-skill-docs", "momentum-mcp"}


def test_unlisted_library_marked_and_excluded(libs):
    assert by_id(libs, "alpha-skills").listed is False
    assert by_id(libs, "alpha-skills").unresolved
    offered = listed_only(libs)
    assert "alpha-skills" not in {lib.id for lib in offered}
    assert {lib.id for lib in offered} == {"broker-skill-docs", "momentum-mcp"}


def test_keys_only_for_picked_libraries(libs):
    assert keys_needed([]) == ()
    assert keys_needed([by_id(libs, "broker-skill-docs")]) == ()
    keys = keys_needed([by_id(libs, "momentum-mcp"), by_id(libs, "alpha-skills")])
    assert keys == tuple(sorted(set(keys)))
    assert "SEC_USER_AGENT" in keys and "FMP_API_KEY" not in keys_needed([by_id(libs, "momentum-mcp")])


def test_every_listed_library_has_license_and_source(libs):
    for lib in listed_only(libs):
        assert lib.license.strip() and lib.source_url.strip()


def test_entries_match_design_doc(libs):
    a, b, m = (by_id(libs, i) for i in ("alpha-skills", "broker-skill-docs", "momentum-mcp"))
    assert (a.license, a.kind, a.counts["trading_skills"], a.counts["shippable"]) == ("MIT", "skills", 64, 56)
    assert (b.license, b.kind, b.counts["broker_docs"]) == ("CC0", "skills", 44)
    assert (m.license, m.kind, m.counts["quant_tools"], m.counts["tickertrace_tools"]) == ("Apache-2.0", "tool", 56, 17)
    assert any("73" in u and "74" in u and "UNVERIFIED" in u for u in m.unverified)


def test_keys_are_names_only_and_yaml_has_no_value_shapes():
    for fname in ("skills.yaml", "tools.yaml"):
        text = (resources.files("trading_agent") / "data" / "libraries" / fname).read_text(encoding="utf-8")
        for scalar in re.findall(r"[^\s,\[\]]+", re.sub(r"#.*", "", text)):
            assert not re.fullmatch(r"sk-[A-Za-z0-9_-]{8,}", scalar), "key-shaped value"
            if "://" not in scalar and "/" not in scalar:
                assert not re.fullmatch(r"[A-Za-z0-9+=_-]{32,}", scalar), "token-shaped value"
        for lib in yaml.safe_load(text)["libraries"]:
            assert all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", k) for k in lib["needs_keys"])


def test_third_party_md_mentions_every_library_and_unresolved_note(libs):
    text = (REPO / "THIRD_PARTY.md").read_text(encoding="utf-8")
    for lib in libs:
        assert lib.id in text
    assert "alpha-skills lineage: unresolved" in text
    assert "awesome-broker-mcp" in text and "Hermes" in text


def test_no_dashes_in_new_files():
    for p in (REPO / "THIRD_PARTY.md", REPO / "src/trading_agent/libraries.py", Path(__file__),
              *(REPO / "src/trading_agent/data/libraries").glob("*.yaml")):
        t = p.read_text(encoding="utf-8")
        assert chr(0x2014) not in t and chr(0x2013) not in t, p


# ---- strict loader ----

def test_override_root_loads(tmp_path):
    out = load_libraries(write(tmp_path, [entry()]))
    assert out[0] == Library("x-lib", "skills", "X", "https://example.com/x", "MIT", {"n": 1},
                             ("X_API_KEY",), "a", True)


@pytest.mark.parametrize("field", ["id", "kind", "name", "source_url", "license", "counts",
                                   "needs_keys", "attribution", "listed"])
def test_missing_field(tmp_path, field):
    e = entry()
    del e[field]
    with pytest.raises(LibraryError) as ei:
        load_libraries(write(tmp_path, [e]))
    assert field in str(ei.value)
    if field != "id":
        assert "x-lib" in str(ei.value)


def test_unknown_field(tmp_path):
    with pytest.raises(LibraryError, match=r"x-lib.*surprise"):
        load_libraries(write(tmp_path, [entry(surprise="SECRETVALUE123")]))


@pytest.mark.parametrize("field,value", [
    ("name", 5), ("license", ["MIT"]), ("listed", "yes"), ("counts", [1]), ("counts", {"a": "1"}),
    ("counts", {"a": True}), ("needs_keys", "A_KEY"), ("needs_keys", [1]), ("kind", 3),
])
def test_wrong_types(tmp_path, field, value):
    with pytest.raises(LibraryError) as ei:
        load_libraries(write(tmp_path, [entry(**{field: value})]))
    assert "x-lib" in str(ei.value) and field in str(ei.value)


def test_duplicate_ids(tmp_path):
    with pytest.raises(LibraryError, match="x-lib.*duplicate"):
        load_libraries(write(tmp_path, [entry()], [entry(kind="tool")]))


@pytest.mark.parametrize("field", ["license", "source_url"])
def test_listed_needs_license_and_source(tmp_path, field):
    with pytest.raises(LibraryError, match=field):
        load_libraries(write(tmp_path, [entry(**{field: "  "})]))
    load_libraries(write(tmp_path, [entry(**{field: "", "listed": False})]))


@pytest.mark.parametrize("bad", ["not a url", "http://example.com/x", "https://", "ftp://example.com", "example.com/x",
                                 "https://[::1", "https://host:badport", "https://host:99999/x", "https://host/a b",
                                 "https://host/a\nb", " https://example.com/x", "https://example.com/x ",
                                 "https://user:pw@host/x", "https://" + "h" * 300 + "/x"])
def test_listed_needs_https_url(tmp_path, bad):
    with pytest.raises(LibraryError, match=r"x-lib.*source_url"):
        load_libraries(write(tmp_path, [entry(source_url=bad)]))
    assert load_libraries(write(tmp_path, [entry(source_url=bad, listed=False)]))[0].source_url == bad


def test_good_url_shapes_still_load(tmp_path):
    for ok in ("HTTPS://Example.com", "https://github.com/mphinance/alpha-skills", "https://host:8443/x"):
        assert load_libraries(write(tmp_path, [entry(source_url=ok)]))[0].source_url == ok


def test_bad_url_error_is_content_free(tmp_path):
    with pytest.raises(LibraryError) as ei:
        load_libraries(write(tmp_path, [entry(source_url="https://[::1")]))
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    assert "::1" not in str(ei.value)


@pytest.mark.parametrize("bad", ["Bad Id/../x", "UPPER", "-lead", "", "a_b", "X" * 200])
def test_bad_ids(tmp_path, bad):
    with pytest.raises(LibraryError) as ei:
        load_libraries(write(tmp_path, [entry(id=bad)]))
    msg = str(ei.value)
    assert "'id'" in msg and len(msg) < 200
    assert "X" * 41 not in msg
    assert "\n" not in msg


@pytest.mark.parametrize("bad", [-1, True])
def test_counts_non_negative_ints(tmp_path, bad):
    with pytest.raises(LibraryError, match=r"x-lib.*counts"):
        load_libraries(write(tmp_path, [entry(counts={"n": bad})]))
    assert load_libraries(write(tmp_path, [entry(counts={"n": 0})]))[0].counts == {"n": 0}


def test_unknown_kind(tmp_path):
    with pytest.raises(LibraryError, match=r"x-lib.*kind"):
        load_libraries(write(tmp_path, [entry(kind="plugin")]))


@pytest.mark.parametrize("bad", ["sk-abc def", "lower_case", "1ABC", "A-B", "ABC\n", "", "abc=1", "A B"])
def test_bad_key_names(tmp_path, bad):
    with pytest.raises(LibraryError) as ei:
        load_libraries(write(tmp_path, [entry(needs_keys=[bad])]))
    assert "x-lib" in str(ei.value) and "needs_keys" in str(ei.value)
    assert bad.strip() not in str(ei.value) or bad.strip() == ""


def test_missing_data_file(tmp_path):
    with pytest.raises(LibraryError):
        load_libraries(tmp_path)


def test_non_utf8_data_file(tmp_path):
    write(tmp_path)
    (tmp_path / "tools.yaml").write_bytes(b"libraries: [\xff\xfe]")
    with pytest.raises(LibraryError):
        load_libraries(tmp_path)


def test_invalid_yaml_has_no_content(tmp_path):
    write(tmp_path)
    (tmp_path / "skills.yaml").write_text("libraries: [SECRETVALUE123", encoding="utf-8")
    with pytest.raises(LibraryError) as ei:
        load_libraries(tmp_path)
    assert "SECRETVALUE123" not in str(ei.value)


def test_non_mapping_entry(tmp_path):
    with pytest.raises(LibraryError):
        load_libraries(write(tmp_path, ["just a string"]))
