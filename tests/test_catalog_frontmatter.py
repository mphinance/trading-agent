from __future__ import annotations

import pytest

from trading_agent.catalog import CatalogError
from trading_agent.catalog.frontmatter import parse_front_matter


def test_flat_keys_and_trim():
    fm = parse_front_matter("---\nname:   Alpaca  \nregion: US\n---\n\n# x\n")
    assert fm == {"name": "Alpaca", "region": "US"}


def test_trailing_comment_stripped():
    fm = parse_front_matter("---\nstatus: official + community # note\n---\n")
    assert fm["status"] == "official + community"


def test_hash_without_leading_whitespace_survives():
    fm = parse_front_matter("---\nsource_url: https://example.com/a#frag\n---\n")
    assert fm["source_url"] == "https://example.com/a#frag"


def test_crlf_line_endings():
    fm = parse_front_matter("---\r\nname: A\r\nstatus: none\r\n---\r\nbody")
    assert fm == {"name": "A", "status": "none"}


def test_no_block_raises_naming_file():
    with pytest.raises(CatalogError, match="brokers/x.md"):
        parse_front_matter("# just a page\n", "brokers/x.md")


def test_unclosed_block_raises():
    with pytest.raises(CatalogError):
        parse_front_matter("---\nname: A\n")
