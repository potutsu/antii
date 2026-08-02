"""
polymarket.py — Polymarket CLOB + Gamma API client
Standalone antii — no proba dependency
"""

import time
import requests
from typing import Optional

# ── Base URLs ──────────────────────────────────────────────────────
CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "antii/1.0"})


def _get(url: str, params: dict = None, retries: int = 3, timeout: int = 15) -> Optional[dict]:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                wait = 2 ** attempt * 5
                time.sleep(wait)
            else:
                if attempt == retries - 1:
                    raise
                time.sleep(2)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2)
    return None


# ── Market discovery ───────────────────────────────────────────────

def fetch_sampling_markets(limit: int = 1000) -> list[dict]:
    """
    GET /sampling-markets — returns active markets with embedded token data.
    Returns list of market dicts with keys: condition_id, question, tokens,
    volume_24h, liquidity, etc.
    """
    data = _get(f"{CLOB_BASE}/sampling-markets", params={"limit": limit})
    if not data:
        return []
    # Response is {"data": [...], "next_cursor": ...}
    if isinstance(data, dict):
        return data.get("data", [])
    return data


def fetch_gamma_events(limit: int = 500, offset: int = 0) -> list[dict]:
    """
    GET /events — discovery metadata, categories from tags.
    Used to cross-reference tags/category for filtering.
    """
    data = _get(f"{GAMMA_BASE}/events", params={"limit": limit, "offset": offset, "active": "true"})
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("data", [])


# ── Price history ──────────────────────────────────────────────────

def fetch_price_history(token_id: str, interval: str = "1d", fidelity: int = 1) -> list[dict]:
    """
    GET /prices-history — returns up to 1441 points/day.
    Each point: {"t": unix_ts, "p": price}
    interval: 1d | 1w | 1m | all
    fidelity: 1 = per-minute, 60 = per-hour
    """
    data = _get(
        f"{CLOB_BASE}/prices-history",
        params={"market": token_id, "interval": interval, "fidelity": fidelity},
    )
    if not data:
        return []
    if isinstance(data, dict):
        return data.get("history", [])
    return data


def get_price_60min_ago(token_id: str) -> Optional[float]:
    """
    Return YES token price from ~60 minutes ago using 1d/fidelity=1 history.
    Returns None if insufficient data.
    """
    history = fetch_price_history(token_id, interval="1d", fidelity=1)
    if len(history) < 61:
        return None
    # History is chronological; last entry = now, [-61] = ~60min ago
    point = history[-61]
    return float(point["p"])


def get_price_60min_ago_with_depth(token_id: str) -> tuple[Optional[float], int]:
    """
    Like get_price_60min_ago but also returns the total number of history points
    in the last 24h. Used as a liquidity proxy: active markets have many points
    (each represents a trade or price update); illiquid/stale markets have few.

    Returns: (price_60m_ago, point_count)
      price_60m_ago — None if history has < 61 points
      point_count   — total points in the 1d/fidelity=1 response (0 if fetch failed)
    """
    history = fetch_price_history(token_id, interval="1d", fidelity=1)
    n = len(history)
    if n < 61:
        return None, n
    return float(history[-61]["p"]), n


# ── Real-time price ────────────────────────────────────────────────

def fetch_last_trade_price(token_id: str) -> Optional[float]:
    """
    GET /last-trade-price — real-time price, ~533ms per call.
    Returns float price or None.
    """
    data = _get(f"{CLOB_BASE}/last-trade-price", params={"token_id": token_id})
    if not data:
        return None
    price = data.get("price")
    if price is None:
        return None
    return float(price)


def fetch_last_trade_prices_batch(token_ids: list[str]) -> dict[str, float]:
    """
    Fetch last trade prices for multiple tokens sequentially.
    Returns {token_id: price} dict. Missing tokens omitted.
    """
    results = {}
    for tid in token_ids:
        try:
            p = fetch_last_trade_price(tid)
            if p is not None:
                results[tid] = p
        except Exception:
            pass
    return results


# ── Market metadata helpers ────────────────────────────────────────

def extract_yes_token(market: dict) -> Optional[dict]:
    """
    From a sampling-markets entry, extract the YES token dict.
    Tokens list usually has outcome="Yes" / "No".
    """
    tokens = market.get("tokens", [])
    for t in tokens:
        outcome = (t.get("outcome") or "").strip().lower()
        if outcome == "yes":
            return t
    # Fallback: first token if only two
    if len(tokens) == 2:
        return tokens[0]
    return None


def is_binary_market(market: dict) -> bool:
    """True if market has exactly two tokens (YES/NO binary)."""
    tokens = market.get("tokens", [])
    return len(tokens) == 2


def get_market_tags(market: dict) -> set[str]:
    """Return lowercase tag set from a market dict."""
    tags = market.get("tags", [])
    if isinstance(tags, list):
        return {t.lower() if isinstance(t, str) else (t.get("slug", "") or t.get("label", "")).lower()
                for t in tags}
    return set()
