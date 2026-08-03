"""
discovery.py — Scans Polymarket every 5 min for overreaction signals.

Signal criteria:
  - Binary market (YES/NO only)
  - Category in ALLOWED_CATEGORIES, no sports tags
  - YES price currently between YES_PRICE_MIN and YES_PRICE_MAX
  - YES price rose >= ENTRY_MIN_MOVE_60MIN% in the last 60 minutes
  - volume_24h >= MIN_VOLUME_24H AND liquidity >= MIN_LIQUIDITY
    (fetched from gamma /markets?clob_token_ids= — the only reliable bridge
     between sampling-markets CLOB IDs and gamma volume/liquidity data)

Emits to: data/signal.jsonl
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import (
    DISCOVERY_INTERVAL_SEC,
    ENTRY_MIN_MOVE_60MIN,
    MIN_VOLUME_24H,
    MIN_LIQUIDITY,
    YES_PRICE_MIN,
    YES_PRICE_MAX,
    ALLOWED_CATEGORIES,
    SPORTS_TAGS,
    FINNHUB_API_KEY,
)
from paths import ensure_dirs, SIGNAL_JSONL
from polymarket import (
    fetch_sampling_markets,
    fetch_last_trade_price,
    fetch_gamma_market_by_token,
    get_price_60min_ago,
    extract_yes_token,
    is_binary_market,
)
from base_rate import lookup as base_rate_lookup
from finnhub_client import fetch_news_at_signal


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [discovery] {msg}", flush=True)


DEDUP_WINDOW_SEC = 4 * 3600   # 4 hours — allow re-signal after this

def load_seen_ids() -> set[str]:
    """
    Load condition_ids signalled within the last DEDUP_WINDOW_SEC.
    Allows re-signalling the same market after the window expires
    (e.g. a new spike after the previous one reverted).
    """
    seen     = set()
    cutoff   = time.time() - DEDUP_WINDOW_SEC
    if SIGNAL_JSONL.exists():
        try:
            for line in open(SIGNAL_JSONL, "r", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                cid = rec.get("condition_id")
                sig_ts = float(rec.get("signal_ts", 0))
                if cid and sig_ts > cutoff:
                    seen.add(cid)
        except Exception:
            pass
    return seen


# Sports tag slugs — block these
_SPORTS_SLUGS = {
    "sports", "soccer", "football", "basketball", "baseball", "tennis",
    "golf", "nba", "nfl", "nhl", "mlb", "mls", "nascar", "ufc", "mma",
    "boxing", "rugby", "cricket", "hockey", "swimming", "athletics",
    "olympics", "esports", "world-cup", "fifa-world-cup",
    "champions-league", "premier-league", "nba-offseason",
    "nba-free-agency", "fifa", "la-liga", "bundesliga", "serie-a",
}


def _market_tags(market: dict) -> set:
    """Extract tags from a sampling-markets record (plain strings)."""
    tags = set()
    for t in (market.get("tags") or []):
        if isinstance(t, str):
            tags.add(t.lower().strip())
        elif isinstance(t, dict):
            for key in ("slug", "label"):
                v = str(t.get(key, "") or "").lower().strip()
                if v:
                    tags.add(v)
    return tags


def _category_from_tags(tags: set) -> str:
    """
    Map plain string tags from /sampling-markets to category.
    Tags here are labels like 'Politics', 'Crypto', 'Economics'.
    """
    # Direct label matches (case-insensitive, already lowered)
    label_map = {
        "politics": "politics", "elections": "politics", "election": "politics",
        "us election": "politics", "midterms": "politics", "primaries": "politics",
        "primary elections": "politics", "democratic primary": "politics",
        "house elections": "politics", "senate": "politics",
        "government": "politics", "government shutdown": "politics",
        "congress": "politics", "impeachment": "politics",
        "geopolitics": "geopolitics", "war": "geopolitics",
        "middle east": "geopolitics", "ukraine": "geopolitics",
        "russia": "geopolitics", "china": "geopolitics", "iran": "geopolitics",
        "israel": "geopolitics", "nato": "geopolitics", "military": "geopolitics",
        "nuclear": "geopolitics", "ceasefire": "geopolitics",
        "sanctions": "geopolitics", "world": "geopolitics",
        "economics": "economics", "economy": "economics",
        "federal reserve": "economics", "fed": "economics",
        "interest rates": "economics", "inflation": "economics",
        "recession": "economics", "gdp": "economics",
        "economic policy": "economics", "tariff": "economics",
        "finance": "economics", "business": "economics",
        "crypto": "crypto", "cryptocurrency": "crypto",
        "bitcoin": "crypto", "ethereum": "crypto",
        "defi": "crypto", "nft": "crypto", "web3": "crypto",
        "solana": "crypto", "btc": "crypto",
        "pre-market": "crypto", "fdv": "crypto", "token launch": "crypto",
        "crypto prices": "crypto", "hit price": "crypto",
        "ai": "tech", "artificial intelligence": "tech",
        "tech": "tech", "technology": "tech",
        "openai": "tech", "antitrust": "tech",
        "big tech": "tech", "ipo": "tech",
        "celebrities": "politics",   # celebrity markets tend to be pop-culture politics
    }
    for tag in tags:
        cat = label_map.get(tag)
        if cat:
            return cat
    return ""


def _category_from_title(title: str) -> str:
    """Last-resort: infer category from market title keywords."""
    t = title.lower()
    if any(k in t for k in ("bitcoin", "ethereum", "crypto", "btc", "eth", "defi", "solana",
                             "uni ", "aave", "chainlink", "polygon", "matic", "avax",
                             "base chain", "hyperliquid", "token", "coin price",
                             "market cap", "altcoin", "nft", "web3", "blockchain",
                             "binance", "coinbase", "ftx", "pump.fun", "fdv")):
        return "crypto"
    if any(k in t for k in ("fed ", "fomc", "rate cut", "rate hike", "cpi", "recession",
                             "inflation", "gdp", "tariff", "trade war", "unemployment",
                             "interest rate", "treasury yield", "fiscal", "deficit",
                             "budget", "shutdown", "debt ceiling", "imf", "world bank")):
        return "economics"
    if any(k in t for k in ("war", "invasion", "ceasefire", "nato", "nuclear", "missile",
                             "airspace", "sanction", "iran", "ukraine", "russia", "israel",
                             "china", "taiwan", "north korea", "military", "troops",
                             "gaza", "hamas", "hezbollah", "putin", "xi jinping",
                             "kim jong", "zelensky", "middle east", "coup")):
        return "geopolitics"
    if any(k in t for k in ("president", "congress", "senate", "election", "vote",
                             "trump", "democrat", "republican", "white house", "impeach",
                             "supreme court", "veto", "executive order", "pardon",
                             "governor", "mayor", "nominee", "primary", "ballot",
                             "harris", "biden", "pelosi", "schumer", "mcconnell",
                             "elon musk", "government shutdown", "legislation", "bill ",
                             "act ", "policy", "cabinet", "administration")):
        return "politics"
    if any(k in t for k in ("openai", "anthropic", "google", "microsoft", "apple",
                             "ai ", "ipo", "antitrust", "tech", "startup",
                             "nvidia", "meta ", "amazon", "tesla", "spacex",
                             "tiktok", "snapchat", "x.com", "twitter", "linkedin",
                             "samsung", "qualcomm", "amd ", "intel ", "arm ",
                             "chatgpt", "gemini", "grok", "llm", "artificial intel",
                             "software", "semiconductor", "data center", "cloud")):
        return "tech"
    return ""


def is_sports(tag_slugs: set) -> bool:
    return bool(tag_slugs & _SPORTS_SLUGS)


def emit_signal(record: dict):
    with open(SIGNAL_JSONL, "a") as f:
        f.write(json.dumps(record) + "\n")
    log(f"SIGNAL → {record['question'][:60]}  YES={record['yes_price_now']:.3f}  "
        f"move={record['move_pct']:.1f}%  cat={record['category']}")


def scan():
    log("fetching sampling-markets...")
    try:
        markets = fetch_sampling_markets(limit=1000)
    except Exception as e:
        log(f"ERROR fetch_sampling_markets: {e}")
        return 0

    log(f"fetched {len(markets)} markets — running pass1...")

    seen_ids = load_seen_ids()
    signals  = 0

    # ── Pass 1: static filters (no CLOB calls) ───────────────────
    # /sampling-markets: condition_id, tags=plain strings, volume24hr=None
    # Category from string tags + title fallback. No volume filter (None on this endpoint).
    candidates = []
    skip_stats = {"dedup":0,"not_binary":0,"sports":0,"no_cat":0,"no_token":0,"price_range":0}

    for mkt in markets:
        try:
            # /sampling-markets uses condition_id not conditionId
            cid = str(mkt.get("condition_id") or mkt.get("conditionId") or "").strip()
            if not cid or cid in seen_ids:
                skip_stats["dedup"] += 1
                continue

            if not is_binary_market(mkt):
                skip_stats["not_binary"] += 1
                continue

            # Tags are plain strings on /sampling-markets
            tags = _market_tags(mkt)

            # Block sports
            if tags & _SPORTS_SLUGS:
                skip_stats["sports"] += 1
                continue

            # Category: try string tag labels first, then title keywords
            category = _category_from_tags(tags)
            if not category:
                category = _category_from_title(mkt.get("question", ""))
            if not category:
                skip_stats["no_cat"] += 1
                continue

            # YES token
            yes_tok = extract_yes_token(mkt)
            if not yes_tok:
                skip_stats["no_token"] += 1
                continue
            token_id = str(yes_tok.get("token_id") or "").strip()
            if not token_id:
                skip_stats["no_token"] += 1
                continue

            # Price range from embedded token price (no CLOB call)
            yes_price_static = float(yes_tok.get("price") or 0)
            if yes_price_static > 0:
                if not (YES_PRICE_MIN <= yes_price_static <= YES_PRICE_MAX):
                    skip_stats["price_range"] += 1
                    continue

            no_tok = next(
                (t for t in mkt.get("tokens", [])
                 if (t.get("outcome") or "").lower() == "no"),
                None,
            )
            no_token_id = str(no_tok.get("token_id") or "") if no_tok else ""

            candidates.append({
                "cid":          cid,
                "question":     mkt.get("question", ""),
                "category":     category,
                "token_id_yes": token_id,
                "token_id_no":  no_token_id,
                "tags":         list(tags),
            })

        except Exception as e:
            log(f"ERROR static filter {mkt.get('condition_id','?')}: {e}")

    total_skipped = sum(skip_stats.values())
    log(f"pass1: {len(markets)} markets → {len(candidates)} candidates "
        f"(skipped {total_skipped}: {skip_stats})")

    if not candidates:
        return 0

    # ── Pass 2: CLOB price checks on candidates only ──────────────
    for c in candidates:
        try:
            token_id = c["token_id_yes"]

            # ── Gamma vol/liq gate — first, before any CLOB calls ─────
            # Kills ~98% of candidates cheaply. Gamma is a single fast HTTP
            # call; CLOB price-history is slow. No point fetching history for
            # markets that will fail vol/liq anyway.
            gm = fetch_gamma_market_by_token(token_id)
            if gm is None:
                continue
            vol24h = float(gm.get("volume24hr") or 0)
            liq    = float(gm.get("liquidity")  or 0)
            if vol24h < MIN_VOLUME_24H or liq < MIN_LIQUIDITY:
                continue

            # Use gamma's category as a fallback if pass1 couldn't assign one
            category = c["category"] or str(gm.get("category") or "").lower().strip()
            if not category:
                continue

            # ── CLOB price checks — only on vol/liq survivors ─────────
            yes_now = fetch_last_trade_price(token_id)
            if yes_now is None:
                continue
            if not (YES_PRICE_MIN <= yes_now <= YES_PRICE_MAX):
                continue

            yes_60m = get_price_60min_ago(token_id)
            if yes_60m is None or yes_60m <= 0:
                continue

            move_pct = (yes_now - yes_60m) / yes_60m * 100
            if move_pct < ENTRY_MIN_MOVE_60MIN:
                continue

            # ── Signal confirmed ───────────────────────────────────────
            cid      = c["cid"]
            question = c["question"]

            base_rate, br_label = base_rate_lookup(question)
            news = fetch_news_at_signal(question, FINNHUB_API_KEY)

            record = {
                "signal_id":       f"{cid}_{int(time.time())}",
                "condition_id":    cid,
                "question":        question,
                "category":        category,
                "token_id_yes":    token_id,
                "token_id_no":     c["token_id_no"],
                "tags":            c.get("tags", []),
                "yes_price_60m":   round(yes_60m,  4),
                "yes_price_now":   round(yes_now,  4),
                "move_pct":        round(move_pct, 2),
                "volume_24h":      round(vol24h, 2),
                "liquidity":       round(liq, 2),
                "base_rate":       base_rate,
                "base_rate_label": br_label,
                "news_at_signal":  news,
                "news_annotation": "",
                "signal_ts":       time.time(),
                "signal_iso":      datetime.now(timezone.utc).isoformat(),
                "status":          "new",
            }
            emit_signal(record)
            seen_ids.add(cid)
            signals += 1

        except Exception as e:
            log(f"ERROR clob check {c.get('cid','?')}: {e}")

    return signals


def main():
    ensure_dirs()
    log("discovery started")
    while True:
        try:
            n = scan()
            log(f"scan complete — {n} new signals — sleeping {DISCOVERY_INTERVAL_SEC}s")
        except Exception as e:
            log(f"FATAL scan error: {e}")
        time.sleep(DISCOVERY_INTERVAL_SEC)


if __name__ == "__main__":
    main()
