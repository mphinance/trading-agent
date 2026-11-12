from __future__ import annotations

from trading_agent.wizard.screens import FIRST_SCREEN_SLUGS
from trading_agent.wizard.catalog import (
    is_installable, order_catalog, region_is_uncurated, route_fingerprint, route_names, route_problem, search_catalog,
)


def entry(slug, name=None, *, region_codes=None, status=("official",), trading="true",
          route_kinds=("remote",), region_raw="", selectable=True, routes=None):
    e = {"slug": slug, "name": name or slug.title(), "status": list(status), "trading": trading,
         "route_kinds": list(route_kinds), "region_raw": region_raw, "source_url": f"https://example.test/{slug}",
         "page_path": f"brokers/{slug}.md", "last_verified": "2026-11-01",
         "routes": routes if routes is not None else [{"route_id": "r", "kind": "remote-url", "url": "https://x.test/mcp"}],
         "wizard": {"selectable": selectable, "banner": "setup details not curated yet"}}
    if region_codes is not None:
        e["region_codes"] = region_codes
    return e


CATALOG = [
    entry("zeta-broker", "Zeta", region_codes=["UK"], status=("community",), trading="false"),
    entry("alpaca", "Alpaca", region_codes=["US"], route_kinds=("remote", "local")),
    entry("uk-bank", "UK Bank", region_codes=["UK", "IE"]),
    entry("world-fx", "World FX", region_codes=["GLOBAL"], route_kinds=("local",)),
    entry("mystery", "Mystery Broker", region_raw="somewhere odd"),            # no region_codes at all
    entry("blank-region", "Blank Region", region_codes=[]),                    # empty list: not curated
    entry("tradier", "Tradier", region_codes=["US"], trading="draft-only"),
]


def slugs(rows):
    return [e["slug"] for e in rows]


def test_first_screen_order_is_the_ten_then_a_to_z():
    assert FIRST_SCREEN_SLUGS == ("interactive-brokers", "alpaca", "kraken", "tradier", "tastytrade", "bitget",
                                  "capital-com", "etoro", "trade-agent", "snaptrade")
    ordered = slugs(order_catalog(CATALOG))
    assert ordered[:2] == ["alpaca", "tradier"]            # catalog slugs from the list, in list order; absent ones skipped
    assert ordered[2:] == ["blank-region", "mystery", "uk-bank", "world-fx", "zeta-broker"]
    assert sorted(ordered) == sorted(slugs(CATALOG))        # ordering only: nothing dropped


def test_no_filters_returns_everything_in_order():
    assert slugs(search_catalog(CATALOG)) == slugs(order_catalog(CATALOG))


def test_region_filter_keeps_uncurated_entries_in_a_separate_group_after_matches():
    rows = search_catalog(CATALOG, region="UK")
    got = slugs(rows)
    assert got[:3] == ["uk-bank", "world-fx", "zeta-broker"]          # UK, GLOBAL
    assert set(got[3:]) == {"blank-region", "mystery"}                 # uncurated, never hidden
    assert not region_is_uncurated(rows[0]) and all(region_is_uncurated(e) for e in rows[3:])
    assert "alpaca" not in got and "tradier" not in got               # curated and elsewhere: filtered out


def test_region_filter_is_case_insensitive():
    assert "uk-bank" in slugs(search_catalog(CATALOG, region="uk"))


def test_text_search_matches_name_slug_and_region_raw():
    assert slugs(search_catalog(CATALOG, text="alp")) == ["alpaca"]
    assert slugs(search_catalog(CATALOG, text="ODD")) == ["mystery"]
    assert slugs(search_catalog(CATALOG, text="uk-bank")) == ["uk-bank"]


def test_status_trading_and_route_kind_filters():
    assert slugs(search_catalog(CATALOG, status="community")) == ["zeta-broker"]
    assert slugs(search_catalog(CATALOG, trading="draft-only")) == ["tradier"]
    assert slugs(search_catalog(CATALOG, route_kind="local")) == ["alpaca", "world-fx"]


def test_filters_combine():
    assert slugs(search_catalog(CATALOG, region="US", trading="true")) == ["alpaca", "world-fx", "blank-region", "mystery"]


def test_is_installable_needs_selectable_and_routes():
    assert is_installable(entry("a"))
    assert not is_installable(entry("a", selectable=False))
    assert not is_installable(entry("a", routes=[]))
    assert not is_installable(entry("a", routes=[{"route_id": "g", "kind": "local-gateway-required"}]))


def test_route_names_are_secret_unless_the_catalog_marks_a_mode_switch_explicitly():
    route = {"auth": {"env_vars": [
        {"name": "A_KEY", "role": "key-id"},                                                  # no flag: secret
        {"name": "B_MODE", "role": "mode-switch"},                                            # secret flag absent: secret
        {"name": "C_MODE", "role": "mode-switch", "secret": False},                           # explicit: plain
        {"name": "D_SECRET", "role": "secret", "secret": False}],                             # not a mode switch: secret
        "headers": ["API_KEY", "PAPER_TRADING"], "header_env": {"API_KEY": "T_API_KEY"}}}
    assert [(n.name, n.secret) for n in route_names(route)] == [
        ("A_KEY", True), ("B_MODE", True), ("C_MODE", False), ("D_SECRET", True), ("T_API_KEY", True),
        ("PAPER_TRADING", True)]


def test_route_problem_names_the_defect_without_echoing_values():
    ok = {"route_id": "r", "kind": "remote-url", "auth": {"env_vars": [{"name": "OK_NAME"}]}}
    assert route_problem(ok) is None
    for bad in ({**ok, "kind": "local-gateway-required"}, {**ok, "route_id": "Bad Id"},
                {**ok, "auth": {"env_vars": [{"name": "bad name"}]}}, {**ok, "auth": {"env_vars": [{"name": "X" * 65}]}},
                {**ok, "auth": {"headers": ["bad header!"]}}):
        assert route_problem(bad)


def test_route_fingerprint_changes_when_the_destination_or_names_change():
    a = {"kind": "remote-url", "url": "https://a.test/mcp", "auth": {"env_vars": [{"name": "K_ONE"}]}}
    assert route_fingerprint(a) == route_fingerprint(dict(a))
    assert route_fingerprint(a) != route_fingerprint({**a, "url": "https://evil.test/mcp"})
    assert route_fingerprint(a) != route_fingerprint({**a, "auth": {"env_vars": [{"name": "K_TWO"}]}})
