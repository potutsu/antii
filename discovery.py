"""
discovery.py — Scans Polymarket every 5 min for overreaction signals.

Signal criteria:
  - Binary market (YES/NO only)
  - Category in ALLOWED_CATEGORIES, no sports tags
  - volume_24h >= MIN_VOLUME_24H, liquidity >= MIN_LIQUIDITY
  - YES price currently between YES_PRICE_MIN and YES_PRICE_MAX
  - YES price rose >= ENTRY_MIN_MOVE_60MIN% in the last 60 minutes

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
    fetch_gamma_events,
    fetch_last_trade_price,
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


# ── Tag slug → category mapping ────────────────────────────────────
# Gamma tags use specific slugs that don't match ALLOWED_CATEGORIES directly.
# This maps known tag slugs/labels to our category buckets.
_TAG_TO_CATEGORY = {
    # politics
    "politics": "politics", "political": "politics", "election": "politics",
    "elections": "politics", "president": "politics", "congress": "politics",
    "senate": "politics", "democrat": "politics", "republican": "politics",
    "trump": "politics", "biden": "politics", "harris": "politics",
    "white-house": "politics", "donald-trump": "politics",
    "us-politics": "politics", "government": "politics",
    "2024-elections": "politics", "2026-elections": "politics",
    "executive-order": "politics", "supreme-court": "politics",
    # geopolitics
    "geopolitics": "geopolitics", "geopolitical": "geopolitics",
    "war": "geopolitics", "nato": "geopolitics", "military": "geopolitics",
    "iran": "geopolitics", "ukraine": "geopolitics", "russia": "geopolitics",
    "china": "geopolitics", "taiwan": "geopolitics", "middle-east": "geopolitics",
    "north-korea": "geopolitics", "israel": "geopolitics", "gaza": "geopolitics",
    "sanctions": "geopolitics", "nuclear": "geopolitics",
    "ceasefire": "geopolitics", "invasion": "geopolitics",
    # economics / macro
    "economy": "economics", "economics": "economics",
    "economic-policy": "economics", "fed": "economics",
    "fed-rates": "economics", "fomc": "economics",
    "jerome-powell": "economics", "inflation": "economics",
    "cpi": "economics", "cpi-release": "economics",
    "recession": "economics", "gdp": "economics",
    "interest-rates": "economics", "treasury": "economics",
    "fiscal": "economics", "tariff": "economics",
    "trade": "economics", "trade-war": "economics",
    "unemployment": "economics", "jobs": "economics",
    "economic-indicators": "economics",
    # crypto
    "crypto": "crypto", "cryptocurrency": "crypto",
    "bitcoin": "crypto", "ethereum": "crypto",
    "defi": "crypto", "nft": "crypto", "web3": "crypto",
    "solana": "crypto", "btc": "crypto", "eth": "crypto",
    "crypto-prices": "crypto",
    # tech
    "ai": "tech", "openai": "tech",
    "artificial-intelligence": "tech", "tech": "tech",
    "technology": "tech", "antitrust": "tech",
    "ipo": "tech",
}

# Sports tag slugs — block these
_SPORTS_SLUGS = {
    "sports", "soccer", "football", "basketball", "baseball", "tennis",
    "golf", "nba", "nfl", "nhl", "mlb", "mls", "nascar", "ufc", "mma",
    "boxing", "rugby", "cricket", "hockey", "swimming", "athletics",
    "olympics", "esports", "world-cup", "fifa-world-cup",
    "champions-league", "premier-league", "nba-offseason",
    "nba-free-agency", "fifa", "la-liga", "bundesliga", "serie-a",
}


def _event_tag_slugs(event: dict) -> set:
    """Return set of lowercase tag slugs + labels from an event."""
    slugs = set()
    for t in (event.get("tags") or []):
        if isinstance(t, dict):
            for key in ("slug", "label"):
                v = str(t.get(key, "") or "").lower().strip()
                if v:
                    slugs.add(v)
        elif isinstance(t, str):
            slugs.add(t.lower().strip())
    return slugs


def _category_from_title(title: str) -> str:
    """Last-resort: infer category from market title keywords."""
    t = title.lower()
    if any(k in t for k in ("bitcoin", "ethereum", "crypto", "btc ", "eth ", "defi", "solana")):
        return "crypto"
    if any(k in t for k in ("fed ", "fomc", "rate cut", "rate hike", "cpi", "recession",
                             "inflation", "gdp", "tariff", "trade war", "unemployment")):
        return "economics"
    if any(k in t for k in ("war", "invasion", "ceasefire", "nato", "nuclear", "missile",
                             "airspace", "sanction", "iran", "ukraine", "russia", "israel",
                             "china", "taiwan", "north korea", "military", "troops")):
        return "geopolitics"
    if any(k in t for k in ("president", "congress", "senate", "election", "vote",
                             "trump", "democrat", "republican", "white house", "impeach",
                             "supreme court", "veto", "executive order", "pardon")):
        return "politics"
    if any(k in t for k in ("openai", "anthropic", "google", "microsoft", "apple",
                             "ai ", "ipo", "antitrust", "tech", "startup")):
        return "tech"
    return ""


def build_category_map(gamma_events: list[dict]) -> dict[str, str]:
    """
    Build {condition_id: category} from Gamma events using tag slug mapping.
    Falls back to title keyword matching.
    """
    cat_map = {}
    for ev in gamma_events:
        markets   = ev.get("markets", [])
        tag_slugs = _event_tag_slugs(ev)

        # Block sports events entirely
        if tag_slugs & _SPORTS_SLUGS:
            continue

        # Map slugs to category
        category = ""
        for slug in tag_slugs:
            if slug in _TAG_TO_CATEGORY:
                category = _TAG_TO_CATEGORY[slug]
                break

        # Fallback to title keyword matching
        if not category:
            ev_title = ev.get("title", "")
            category = _category_from_title(ev_title)
            if not category:
                # Try first market question
                for m in markets[:1]:
                    category = _category_from_title(m.get("question", ""))
                    if category:
                        break

        if not category:
            continue

        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                cat_map[cid] = category

    return cat_map


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

    log(f"fetched {len(markets)} markets — fetching gamma events...")
    try:
        gamma_events = fetch_gamma_events(limit=500)
        cat_map      = build_category_map(gamma_events)
    except Exception as e:
        log(f"WARN could not fetch gamma events: {e}")
        cat_map = {}

    seen_ids = load_seen_ids()
    signals  = 0

    # ── Pass 1: static filters (no CLOB calls) ───────────────────
    candidates = []
    skip_stats = {"dedup":0,"not_binary":0,"sports":0,"no_cat":0,"vol_liq":0,"no_token":0,"price_range":0}

    for mkt in markets:
        try:
            cid = mkt.get("condition_id") or mkt.get("conditionId", "")
            if not cid or cid in seen_ids:
                skip_stats["dedup"] += 1
                continue

            if not is_binary_market(mkt):
                skip_stats["not_binary"] += 1
                continue

            # Category from gamma cat_map — no CLOB needed
            category = cat_map.get(cid, "")
            if not category:
                # Fallback: title keyword match
                category = _category_from_title(mkt.get("question", ""))
            if not category:
                skip_stats["no_cat"] += 1
                continue

            # Volume / liquidity from static market data
            vol24 = float(mkt.get("volume24hr", mkt.get("volume_24h", 0)) or 0)
            liq   = float(mkt.get("liquidity", 0) or 0)
            if vol24 < MIN_VOLUME_24H or liq < MIN_LIQUIDITY:
                skip_stats["vol_liq"] += 1
                continue

            # YES token exists
            yes_tok = extract_yes_token(mkt)
            if not yes_tok:
                skip_stats["no_token"] += 1
                continue
            token_id = yes_tok.get("token_id") or yes_tok.get("tokenId", "")
            if not token_id:
                skip_stats["no_token"] += 1
                continue

            # Price range from embedded token price (no CLOB call yet)
            yes_price_static = float(yes_tok.get("price", 0) or 0)
            if yes_price_static > 0:
                if not (YES_PRICE_MIN <= yes_price_static <= YES_PRICE_MAX):
                    skip_stats["price_range"] += 1
                    continue

            no_tok = next((t for t in mkt.get("tokens", []) if
                           (t.get("outcome") or "").lower() == "no"), None)
            no_token_id = no_tok.get("token_id", "") if no_tok else ""

            candidates.append({
                "cid":          cid,
                "question":     mkt.get("question", ""),
                "category":     category,
                "token_id_yes": token_id,
                "token_id_no":  no_token_id,
                "vol24":        vol24,
                "liq":          liq,
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

            # Live YES price
            yes_now = fetch_last_trade_price(token_id)
            if yes_now is None:
                continue
            if not (YES_PRICE_MIN <= yes_now <= YES_PRICE_MAX):
                continue

            # 60-min price history
            yes_60m = get_price_60min_ago(token_id)
            if yes_60m is None or yes_60m <= 0:
                continue

            move_pct = (yes_now - yes_60m) / yes_60m * 100
            if move_pct < ENTRY_MIN_MOVE_60MIN:
                continue

            # ── Signal confirmed ───────────────────────────────────
            cid      = c["cid"]
            question = c["question"]

            base_rate, br_label = base_rate_lookup(question)
            news = fetch_news_at_signal(question, FINNHUB_API_KEY)

            record = {
                "signal_id":       f"{cid}_{int(time.time())}",
                "condition_id":    cid,
                "question":        question,
                "category":        c["category"],
                "token_id_yes":    token_id,
                "token_id_no":     c["token_id_no"],
                "yes_price_60m":   round(yes_60m,  4),
                "yes_price_now":   round(yes_now,  4),
                "move_pct":        round(move_pct, 2),
                "volume_24h":      c["vol24"],
                "liquidity":       c["liq"],
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
