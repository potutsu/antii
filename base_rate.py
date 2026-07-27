"""
base_rate.py — Keyword-based base rate lookup for market titles.
Used in signal records and postmortem for context.

Base rate = rough prior probability before any market signal,
based on category/type of event. Manual curation; extend as needed.
"""

from typing import Optional

# (keywords_any_match, base_rate, label)
# First match wins. Keywords are lowercased substrings.
_RULES: list[tuple[list[str], float, str]] = [
    # Crypto price targets — very low base rate
    (["bitcoin", "btc", "reach", "hit", "exceed", "$"],         0.10, "crypto_price_target"),
    (["ethereum", "eth", "reach", "hit", "exceed", "$"],        0.10, "crypto_price_target"),
    (["crypto", "reach", "hit", "exceed", "$"],                 0.10, "crypto_price_target"),

    # Regulatory / legal outcomes
    (["sec", "approve", "etf"],                                  0.30, "regulatory_approval"),
    (["ban", "banned", "illegal"],                               0.20, "regulation_ban"),
    (["lawsuit", "settle", "indicted"],                          0.25, "legal_outcome"),

    # Elections / political — close to coin flip absent polling
    (["win", "election", "president", "senator", "governor"],   0.50, "election"),
    (["democrat", "republican", "primary"],                      0.50, "election_primary"),
    (["impeach", "resign", "removed"],                           0.15, "political_removal"),

    # Geopolitical — generally low probability events
    (["war", "invade", "invasion", "military"],                  0.20, "military_action"),
    (["ceasefire", "peace", "treaty"],                           0.25, "peace_deal"),
    (["sanction", "sanctions"],                                  0.30, "sanctions"),

    # Economic indicators — moderate, event-dependent
    (["fed", "rate cut", "rate hike", "fomc"],                   0.40, "fed_decision"),
    (["recession", "gdp", "inflation"],                          0.35, "macro_indicator"),
    (["ipo", "public", "listing"],                               0.35, "ipo"),

    # Tech events
    (["launch", "release", "ship", "announce"],                  0.45, "product_launch"),
    (["acquire", "merger", "buyout"],                            0.25, "acquisition"),
    (["layoff", "layoffs", "fired"],                             0.30, "layoffs"),

    # Crypto project-specific
    (["airdrop"],                                                 0.40, "crypto_airdrop"),
    (["hack", "exploit", "breach"],                              0.15, "security_incident"),
    (["delist", "delisted"],                                     0.20, "delist"),
]

_DEFAULT_BASE_RATE = 0.35
_DEFAULT_LABEL     = "unknown"


def lookup(title: str) -> tuple[float, str]:
    """
    Returns (base_rate, label) for a market title.
    Matches on lowercase substring. First rule wins.
    """
    lower = title.lower()
    for keywords, rate, label in _RULES:
        if all(kw in lower for kw in keywords):
            return rate, label
    # Fallback: single-keyword match (more lenient)
    for keywords, rate, label in _RULES:
        if any(kw in lower for kw in keywords):
            return rate, label
    return _DEFAULT_BASE_RATE, _DEFAULT_LABEL


def get_base_rate(title: str) -> float:
    rate, _ = lookup(title)
    return rate


def get_base_rate_label(title: str) -> str:
    _, label = lookup(title)
    return label
