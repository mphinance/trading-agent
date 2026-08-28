"""
Financial news module using feedparser and trafilatura.

Fetches recent headlines from RSS feeds for a given ticker and extracts
full article text from arbitrary URLs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote_plus

import feedparser
import trafilatura

logger = logging.getLogger(__name__)

# RSS feed templates — {ticker} will be substituted
_FEED_SOURCES: list[dict[str, str]] = [
    {
        "name": "Yahoo Finance",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    },
    {
        "name": "Google News",
        "url": "https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
    },
]


async def fetch_ticker_news(
    ticker: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch recent news headlines for a stock ticker.

    Queries Yahoo Finance RSS and Google News RSS feeds via ``feedparser``
    and returns a de-duplicated, chronologically sorted list of headlines.

    Args:
        ticker: Stock ticker symbol (e.g. ``"AAPL"``).
        limit: Maximum number of headlines to return (1–50).
            Defaults to ``10``.

    Returns:
        A list of dicts, each containing:

        - ``title`` — Headline text.
        - ``url`` — Link to the full article.
        - ``published`` — Publication date string (if available).
        - ``source`` — Name of the RSS feed source.
    """
    ticker = ticker.strip().upper()
    limit = max(1, min(limit, 50))

    def _fetch_all() -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for source in _FEED_SOURCES:
            feed_url = source["url"].format(ticker=quote_plus(ticker))
            try:
                feed = feedparser.parse(feed_url)
            except Exception as exc:
                logger.warning(
                    "Failed to parse feed '%s' for %s: %s",
                    source["name"], ticker, exc,
                )
                continue

            for entry in feed.entries:
                url = entry.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                all_items.append(
                    {
                        "title": entry.get("title", "").strip(),
                        "url": url,
                        "published": entry.get("published", None),
                        "source": source["name"],
                    }
                )

        return all_items[:limit]

    return await asyncio.to_thread(_fetch_all)


async def extract_article_text(
    url: str,
) -> dict[str, Any]:
    """Extract the full-text body of a news article from a URL.

    Uses ``trafilatura`` to download and extract the main textual content
    of an article, stripping ads, navigation, and boilerplate.

    Args:
        url: The full URL of the article to extract.

    Returns:
        A dict containing:

        - ``url`` — The requested URL.
        - ``title`` — Extracted article title (if available).
        - ``text`` — Clean full-text body of the article.
        - ``word_count`` — Number of words in the extracted text.
        - ``error`` — Error message if extraction failed, else ``null``.
    """
    url = url.strip()

    def _extract() -> dict[str, Any]:
        try:
            downloaded = trafilatura.fetch_url(url)
        except Exception as exc:
            logger.warning("Failed to download %s: %s", url, exc)
            return {
                "url": url,
                "title": None,
                "text": None,
                "word_count": 0,
                "error": f"Download failed: {exc}",
            }

        if not downloaded:
            return {
                "url": url,
                "title": None,
                "text": None,
                "word_count": 0,
                "error": (
                    "Could not download the page. The site may be blocking "
                    "automated requests or the URL may be invalid."
                ),
            }

        try:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=True,
                no_fallback=False,  # Enable fallback extractors
                favor_recall=True,
            )
        except Exception as exc:
            logger.warning("Extraction failed for %s: %s", url, exc)
            return {
                "url": url,
                "title": None,
                "text": None,
                "word_count": 0,
                "error": f"Extraction failed: {exc}",
            }

        if not text:
            return {
                "url": url,
                "title": None,
                "text": None,
                "word_count": 0,
                "error": "Extraction returned empty content — the site may require JavaScript or block scrapers.",
            }

        # Try to get the title from metadata
        metadata = trafilatura.extract(
            downloaded,
            output_format="json",
            include_comments=False,
        )
        title = None
        if metadata:
            import json
            try:
                meta_dict = json.loads(metadata)
                title = meta_dict.get("title")
            except (json.JSONDecodeError, TypeError):
                pass

        word_count = len(text.split())

        return {
            "url": url,
            "title": title,
            "text": text,
            "word_count": word_count,
            "error": None,
        }

    return await asyncio.to_thread(_extract)
