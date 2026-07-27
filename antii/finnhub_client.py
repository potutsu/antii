"""
finnhub_client.py — Fetch relevant news headlines at signal time.
Stored in signal record as news_at_signal (list of headline strings).
Manual annotation field left blank for human to fill after resolution.
"""

import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

FINNHUB_BASE = "https://finnhub.io/api/v1"


def _extract_keywords(title: str) -> list[str]:
    """
    Pull likely ticker-or-company-style keywords from a market title
    to use as Finnhub search terms.
    Max 2 terms to keep results relevant.
    """
    stop = {"will", "the", "be", "to", "in", "of", "a", "an", "by", "at",
            "for", "is", "are", "or", "and", "on", "with", "does", "do",
            "reach", "hit", "exceed", "above", "below", "before", "after",
            "this", "that", "its", "his", "her", "their", "than", "when"}
    words = [w.strip("?.,!") for w in title.split() if len(w) > 3]
    keywords = [w for w in words if w.lower() not in stop]
    # Prefer capitalised words (likely proper nouns / tickers)
    caps = [w for w in keywords if w[0].isupper()]
    if caps:
        return caps[:2]
    return keywords[:2]


def fetch_news_at_signal(title: str, api_key: str, lookback_hours: int = 6) -> list[str]:
    """
    Fetch up to 5 recent news headlines relevant to a market title.
    Uses Finnhub /news/search endpoint.
    Returns list of headline strings. Empty list on any failure.
    """
    if not api_key:
        return []

    keywords = _extract_keywords(title)
    if not keywords:
        return []

    query = " ".join(keywords)
    now_ts = int(time.time())
    from_ts = now_ts - lookback_hours * 3600

    try:
        r = requests.get(
            f"{FINNHUB_BASE}/news",
            params={
                "category": "general",
                "token":    api_key,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        articles = r.json()
        if not isinstance(articles, list):
            return []

        # Filter to recent and keyword-relevant
        headlines = []
        for art in articles:
            ts = art.get("datetime", 0)
            headline = art.get("headline", "")
            if not headline or ts < from_ts:
                continue
            hl_lower = headline.lower()
            if any(kw.lower() in hl_lower for kw in keywords):
                headlines.append(headline.strip())
            if len(headlines) >= 5:
                break

        return headlines

    except Exception:
        return []
