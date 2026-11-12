"""Catalog helpers for the wizard: ordering, search, route checks and plain-word labels. Pure functions."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Sequence

from trading_agent.profile.writer import build_mcp_entry
from trading_agent.wizard import screens as S

FIRST_SET = frozenset(S.FIRST_SCREEN_SLUGS)
_WRITABLE_KINDS = frozenset({"remote-url", "local-command"})  # the kinds writer.build_mcp_entry can write
_NAME_OK = re.compile(r"[A-Z_][A-Z0-9_]*")
_HEADER_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*|[A-Z][A-Z0-9_]*")
_ROUTE_ID_OK = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_SLUG_OK = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")   # the writer's server-name rule (writer._SERVER_RE)
COMMAND_MAX = 200
URL_MAX = 512
NAME_MAX = 64

_KIND_WORDS = {"local-command": "runs a program on this computer", "remote-url": "connects to a hosted server"}
_MAINTAINER_WORDS = {"official": "made by the broker", "community": "made by a third party",
                     "vendor": "made by a vendor"}
_CONFIRM_WORDS = {
    "none-documented": "none described on the broker's page", "client-prompt-only": "only a prompt in your AI app",
    "draft-then-execute": "a draft first, then a confirm step", "preview-token": "a preview, then a token",
    "dry-run-then-confirm": "a dry run, then a confirm step", "typed-confirm": "you type a confirmation",
    "in-broker-ui": "you approve in the broker's own app", "unconfirmed": "not confirmed",
}


def plain(token: object, table: dict[str, str] | None = None) -> str:
    """A catalog token in plain words: a table hit, else the token with dashes turned into spaces."""
    t = str(token)
    return (table or {}).get(t, t.replace("-", " ").replace("_", " "))


def order_catalog(entries: Sequence[dict]) -> list[dict]:
    """First-screen slugs (those present), in list order, then the rest A to Z. Ordering only: nothing is dropped."""
    by_slug = {e["slug"]: e for e in entries}
    first = [by_slug[s] for s in S.FIRST_SCREEN_SLUGS if s in by_slug]
    rest = sorted((e for e in entries if e["slug"] not in FIRST_SET),
                  key=lambda e: (str(e.get("name", "")).casefold(), e["slug"]))
    return first + rest


def region_is_uncurated(entry: dict) -> bool:
    return not entry.get("region_codes")


def _text_ok(e: dict, needle: str) -> bool:
    hay = " ".join([str(e.get("name", "")), e["slug"], str(e.get("region_raw", "")),
                    *map(str, e.get("region_codes") or [])]).casefold()
    return needle in hay


def search_catalog(entries: Sequence[dict], *, text: str = "", region: str | None = None,
                   status: str | None = None, trading: str | None = None,
                   route_kind: str | None = None) -> list[dict]:
    """Filter the merged catalog. With a region, entries without ``region_codes`` follow the matches
    as the "region not curated" group and are never hidden."""
    needle = text.strip().casefold()
    rows = [e for e in order_catalog(entries)
            if (not needle or _text_ok(e, needle))
            and (not status or status in (e.get("status") or []))
            and (not trading or e.get("trading") == trading)
            and (not route_kind or route_kind in (e.get("route_kinds") or []))]
    if not region or not region.strip():
        return rows
    want = region.strip().upper()
    matched, uncurated = [], []
    for e in rows:
        codes = {str(c).upper() for c in e.get("region_codes") or []}
        if not codes:
            uncurated.append(e)
        elif want in codes or "GLOBAL" in codes:
            matched.append(e)
    return matched + uncurated


@dataclass(frozen=True)
class NameSpec:
    name: str
    secret: bool


def route_names(route: dict) -> list[NameSpec]:
    """EVERY env var NAME and header NAME of the route (a header's variable is ``header_env[h]`` or h itself).
    A name is typed in plain ONLY when the catalog marks it ``role: mode-switch`` with ``secret: false``
    explicitly. Everything else is secret (hidden input)."""
    auth = route.get("auth") or {}
    out: list[NameSpec] = []
    seen: set[str] = set()
    for v in auth.get("env_vars") or []:
        name = v.get("name")
        if isinstance(name, str) and name not in seen:
            out.append(NameSpec(name, not (v.get("role") == "mode-switch" and v.get("secret") is False)))
            seen.add(name)
    header_env = auth.get("header_env") or {}
    for h in auth.get("headers") or []:
        var = header_env.get(h, h)
        if isinstance(var, str) and var not in seen:
            out.append(NameSpec(var, True))
            seen.add(var)
    return out


def _display_safe(text: object, limit: int) -> bool:
    """Plain printable ASCII only (no newline, control, ANSI escape or bidi character), within LIMIT."""
    return isinstance(text, str) and 0 < len(text) <= limit and all(" " <= c <= "~" for c in text)


def route_problem(route: dict) -> str | None:
    """Why this route cannot be installed (a short reason), or None. Names must be plain env var names, the
    command or address must be safe to show and to write."""
    if route.get("kind") not in _WRITABLE_KINDS:
        return "route kind not supported yet"
    if not isinstance(route.get("route_id"), str) or not _ROUTE_ID_OK.fullmatch(route["route_id"]):
        return "route id not valid"
    auth = route.get("auth") or {}
    names = [v.get("name") for v in auth.get("env_vars") or []]
    header_env = auth.get("header_env") or {}
    names += [header_env.get(h, h) for h in auth.get("headers") or []]
    for h in auth.get("headers") or []:
        if not isinstance(h, str) or not _HEADER_OK.fullmatch(h):
            return "header name not valid"
    if not all(isinstance(n, str) and len(n) <= NAME_MAX and _NAME_OK.fullmatch(n) for n in names):
        return "variable name not valid"
    shown, limit = ((route.get("install") or {}).get("command_text"), COMMAND_MAX) if route["kind"] == "local-command" \
        else (route.get("url"), URL_MAX)
    if not _display_safe(shown, limit):
        return "command or address not safe to show"
    try:
        build_mcp_entry(route, frozenset())    # everything the writer would refuse, refused here instead
    except ValueError:
        return "command or address not valid"
    return None


def installable_routes(entry: dict) -> list[dict]:
    """Routes v1 can install: the entry is selectable and the route is a writable kind with valid names."""
    slug = entry.get("slug")
    if not (entry.get("wizard") or {}).get("selectable") or not isinstance(slug, str) or not _SLUG_OK.fullmatch(slug):
        return []
    return [r for r in entry.get("routes") or [] if isinstance(r, dict) and route_problem(r) is None]


def is_installable(entry: dict) -> bool:
    return bool(installable_routes(entry))


def route_fingerprint(route: dict) -> str:
    """A non-secret digest of where a route sends values: kind, url or command, sorted names."""
    inst = route.get("install") or {}
    doc = {"kind": route.get("kind"), "url": route.get("url"), "command": inst.get("command_text"),
           "names": sorted(n.name for n in route_names(route))}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


def route_destination(route: dict) -> str:
    """Where a value will go, in plain words: the command, or the host and path of the URL (whitespace collapsed)."""
    if route.get("kind") == "local-command":
        return "runs " + " ".join(str((route.get("install") or {}).get("command_text", "a local program")).split())
    url = str(route.get("url", ""))
    return "connects to " + (" ".join(url.split("://", 1)[-1].split()) or "an address not stated")


def route_label(r: dict) -> str:
    how = (r.get("paper") or {}).get("how_to_enable")
    base = (f"{r['route_id']}: {plain(r.get('kind'), _KIND_WORDS)}, "
            f"{plain(r.get('maintainer', 'maintainer not stated'), _MAINTAINER_WORDS)}")
    return base + (f". Paper trading: {how}" if how else "")


def trading_text(e: dict) -> str:
    return {"true": "yes", "false": "no", "draft-only": "draft only"}.get(str(e.get("trading")), "not stated")


def entry_tags(e: dict, *, flag_region: bool = False) -> list[str]:
    st = e.get("status") or []
    tags = []
    if "official" in st:
        tags.append(S.TAG_OFFICIAL)
    if "community" in st:
        tags.append(S.TAG_COMMUNITY)
    if "aggregator-only" in st or "none" in st:
        tags.append(S.TAG_NO_DIRECT)
    if not is_installable(e):
        tags.append(S.TAG_SELF_SETUP)
    if flag_region and region_is_uncurated(e):
        tags.append(S.REGION_NOT_CURATED)
    return tags


def submit_words(sub: dict) -> str:
    return (f"can submit orders: {plain(sub.get('can_submit_order'))}; "
            f"confirmation: {plain(sub.get('confirmation', 'unconfirmed'), _CONFIRM_WORDS)}")
