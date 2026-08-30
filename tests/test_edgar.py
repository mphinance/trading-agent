"""edgar.py — SEC EDGAR client.

Hermetic per repo convention (no network in the suite): every test monkeypatches
edgar._get, the one seam every public function goes through, rather than mocking
requests directly.
"""

from __future__ import annotations

import pandas as pd
import pytest

import edgar as E

TICKERS_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 886982, "ticker": "GME", "title": "GameStop Corp."},
}

SUBMISSIONS_GME = {
    "cik": "0000886982",
    "filings": {
        "recent": {
            "filingDate": ["2026-08-01", "2026-06-15", "2020-01-10"],
            "form": ["8-K", "SC 13G", "10-K"],
            "reportDate": ["", "", "2025-01-31"],
            "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-20-000003"],
            "primaryDocument": ["ex.htm", "primary_doc.xml", "10k.htm"],
            "items": ["2.02", None, None],
        }
    },
}


@pytest.fixture(autouse=True)
def _reset_cache_and_env(monkeypatch):
    E._CACHE.clear()
    monkeypatch.setenv("SEC_USER_AGENT", "Vesper Test (contact: test@example.com)")
    yield
    E._CACHE.clear()


def _patched_get(monkeypatch, table: dict):
    """table maps url -> (kind, payload). Records calls for assertions."""
    calls: list[str] = []

    def fake(url, kind, as_json=True):
        calls.append(url)
        if url not in table:
            raise AssertionError(f"unexpected URL requested: {url}")
        return table[url]

    monkeypatch.setattr(E, "_get", fake)
    return calls


# --------------------------------------------------------------------- cik / name


def test_cik_for_zero_pads(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: TICKERS_JSON)
    assert E.cik_for("gme") == "0000886982"


def test_cik_for_unknown_ticker_raises(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: TICKERS_JSON)
    with pytest.raises(ValueError):
        E.cik_for("NOSUCHTICKER")


def test_company_name_for_returns_title(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: TICKERS_JSON)
    assert E.company_name_for("aapl") == "Apple Inc."


def test_company_name_for_swallows_errors(monkeypatch):
    def boom(url, kind, as_json=True):
        raise RuntimeError("network down")

    monkeypatch.setattr(E, "_get", boom)
    assert E.company_name_for("AAPL") == ""


def test_get_raises_clear_error_without_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        E._get("https://data.sec.gov/x", "facts")


# ------------------------------------------------------------------------ filings


def test_filings_builds_source_urls_and_sorts_desc(monkeypatch):
    _patched_get(monkeypatch, {
        f"{E.WWW_URL}/files/company_tickers.json": TICKERS_JSON,
        f"{E.DATA_URL}/submissions/CIK0000886982.json": SUBMISSIONS_GME,
    })
    df = E.filings("GME", months=0)
    assert list(df["form"]) == ["8-K", "SC 13G", "10-K"]
    assert df.iloc[0]["source_url"] == (
        f"{E.WWW_URL}/Archives/edgar/data/886982/000126000001/ex.htm"
    )


def test_filings_filters_by_form(monkeypatch):
    _patched_get(monkeypatch, {
        f"{E.WWW_URL}/files/company_tickers.json": TICKERS_JSON,
        f"{E.DATA_URL}/submissions/CIK0000886982.json": SUBMISSIONS_GME,
    })
    df = E.filings("GME", months=0, forms=["8-K"])
    assert list(df["form"]) == ["8-K"]


def test_filings_windows_by_months(monkeypatch):
    """The 2020 10-K falls outside a 12-month window from 'now'."""
    _patched_get(monkeypatch, {
        f"{E.WWW_URL}/files/company_tickers.json": TICKERS_JSON,
        f"{E.DATA_URL}/submissions/CIK0000886982.json": SUBMISSIONS_GME,
    })
    df = E.filings("GME", months=12)
    assert "10-K" not in set(df["form"])


# --------------------------------------------------------------------- financials


FACTS_ACCRUAL_GAP = {
    "facts": {
        "us-gaap": {
            "NetIncomeLoss": {
                "units": {"USD": [
                    {"end": "2025-12-31", "start": "2025-01-01", "fy": 2025, "fp": "FY",
                     "form": "10-K", "val": 1000, "accn": "b"},
                ]}
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {"USD": [
                    {"end": "2025-12-31", "start": "2025-01-01", "fy": 2025, "fp": "FY",
                     "form": "10-K", "val": 300, "accn": "b"},
                ]}
            },
        }
    }
}


def test_financials_computes_accrual_gap(monkeypatch):
    _patched_get(monkeypatch, {
        f"{E.WWW_URL}/files/company_tickers.json": TICKERS_JSON,
        f"{E.DATA_URL}/api/xbrl/companyfacts/CIK0000886982.json": FACTS_ACCRUAL_GAP,
    })
    monkeypatch.setattr(E, "cik_for", lambda t: "0000886982")
    wide = E.financials("GME", annual=True)
    assert wide.loc[pd.Timestamp("2025-12-31"), "accrual_gap"] == 700


def test_financials_prefers_latest_accession_on_restatement(monkeypatch):
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {"USD": [
                        {"end": "2025-12-31", "form": "10-K", "val": 100, "accn": "a"},
                        {"end": "2025-12-31", "form": "10-K", "val": 150, "accn": "z"},
                    ]}
                },
            }
        }
    }
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: facts)
    monkeypatch.setattr(E, "cik_for", lambda t: "0000886982")
    wide = E.financials("GME", annual=True)
    assert wide.loc[pd.Timestamp("2025-12-31"), "net_income"] == 150


def test_financials_empty_when_no_matching_forms(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: {"facts": {}})
    monkeypatch.setattr(E, "cik_for", lambda t: "0000886982")
    assert E.financials("GME").empty


# ---------------------------------------------------------------- shares outstanding


def test_shares_outstanding_reads_cover_page(monkeypatch):
    facts = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2025-12-31", "val": 300_000_000,
                                           "form": "10-K", "accn": "a"}]}
                }
            },
            "us-gaap": {},
        }
    }
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: facts)
    monkeypatch.setattr(E, "cik_for", lambda t: "0000886982")
    out = E.shares_outstanding("GME")
    assert out["basic_cover_page"] == 300_000_000
    assert out["as_of"] == "2025-12-31"


def test_shares_outstanding_rejects_implausible_scale(monkeypatch):
    """A diluted count wildly off from the cover-page basic count (not a thousands
    mixup either) must be rejected, not returned as a plausible-looking number.
    """
    facts = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2025-12-31", "val": 300_000_000,
                                           "form": "10-K", "accn": "a"}]}
                }
            },
            "us-gaap": {
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "units": {"shares": [{"end": "2025-12-31", "val": 9_000_000}]}
                }
            },
        }
    }
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: facts)
    monkeypatch.setattr(E, "cik_for", lambda t: "0000886982")
    out = E.shares_outstanding("GME")
    assert out["diluted_weighted_avg"] is None
    assert "REJECTED" in out["diluted_scale_warning"]


def test_shares_outstanding_scales_thousands_mixup(monkeypatch):
    """A filer reporting the diluted count in THOUSANDS without changing the unit
    tag must be caught and scaled, not silently poison every per-share number.
    """
    facts = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"end": "2025-12-31", "val": 105_004_818,
                                           "form": "10-K", "accn": "a"}]}
                }
            },
            "us-gaap": {
                "WeightedAverageNumberOfDilutedSharesOutstanding": {
                    "units": {"shares": [{"end": "2025-12-31", "val": 105_005}]}
                }
            },
        }
    }
    monkeypatch.setattr(E, "_get", lambda url, kind, as_json=True: facts)
    monkeypatch.setattr(E, "cik_for", lambda t: "0000886982")
    out = E.shares_outstanding("GME")
    assert out["diluted_weighted_avg"] == 105_005_000
    assert "THOUSANDS" in out["diluted_scale_warning"]


# ------------------------------------------------------------------- as-filer 13D/G


def _stub_13g_xml(reporting_person: str, issuer: str, issuer_cik: str) -> str:
    """Matches SEC's real electronic Schedule 13D/G XML schema (X0202), confirmed
    live against GameStop's 13D/A on its eBay stake -- not the tag names an
    earlier draft of this module guessed (reportingPersonBeneficiallyOwned...,
    classPercent, eventDateRequiresFilingThisStatement), which do not exist in
    any real filing checked so far.
    """
    return (
        f"<issuerCIK>{issuer_cik}</issuerCIK>"
        f"<issuerCusips><issuerCusipNumber>123456789</issuerCusipNumber></issuerCusips>"
        f"<issuerName>{issuer}</issuerName>"
        f"<dateOfEvent>06/15/2026</dateOfEvent>"
        f"<reportingPersonName>{reporting_person}</reportingPersonName>"
        f"<aggregateAmountOwned>1000000</aggregateAmountOwned>"
        f"<percentOfClass>9.8</percentOfClass>"
    )


def test_stakes_held_keeps_only_as_filer_rows(monkeypatch):
    """The GME-in-eBay case: a 13G filed BY GME (issuer_cik != our_cik) must be
    kept; one filed ABOUT GME (issuer_cik == our_cik) must be dropped.
    """
    submissions = {
        "cik": "0000886982",
        "filings": {"recent": {
            "filingDate": ["2026-06-15", "2026-06-16"],
            "form": ["SC 13G", "SC 13G"],
            "reportDate": ["", ""],
            "accessionNumber": ["0001-26-000010", "0001-26-000011"],
            "primaryDocument": ["primary_doc.xml", "primary_doc.xml"],
            "items": [None, None],
        }},
    }
    as_filer_xml = _stub_13g_xml("GameStop Corp.", "eBay Inc.", "1065088")
    as_subject_xml = _stub_13g_xml("Some Fund LP", "GameStop Corp.", "886982")

    def fake_get(url, kind, as_json=True):
        if "company_tickers" in url:
            return TICKERS_JSON
        if "submissions" in url:
            return submissions
        if "000010" in url:
            return as_filer_xml
        if "000011" in url:
            return as_subject_xml
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(E, "_get", fake_get)
    out = E.stakes_held("GME")
    assert len(out) == 1
    row = out.iloc[0]
    assert row["issuer"] == "eBay Inc."
    assert row["shares"] == 1000000.0
    assert row["percent_of_class"] == 9.8
    assert row["event_date"] == "06/15/2026"
    assert out.iloc[0]["percent_of_class"] == 9.8


def test_stakes_held_empty_when_no_13d_or_13g_filings(monkeypatch):
    submissions = {
        "cik": "0000886982",
        "filings": {"recent": {
            "filingDate": ["2026-06-15"],
            "form": ["10-K"],
            "reportDate": [""],
            "accessionNumber": ["0001-26-000010"],
            "primaryDocument": ["10k.htm"],
            "items": [None],
        }},
    }
    _patched_get(monkeypatch, {
        f"{E.WWW_URL}/files/company_tickers.json": TICKERS_JSON,
        f"{E.DATA_URL}/submissions/CIK0000886982.json": submissions,
    })
    out = E.stakes_held("GME")
    assert out.empty


def test_num_handles_commas_and_junk():
    assert E._num("1,234,567") == 1234567.0
    assert E._num(None) is None
    assert E._num("not a number") is None
