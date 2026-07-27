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
    get_market_tags,
)
from base_rate import lookup as base_rate_lookup
from finnhub_client import fetch_news_at_signal


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [discovery] {msg}", flush=True)


def load_seen_ids() -> set[str]:
    """Load condition_ids already signalled to avoid duplicates within session."""
    seen = set()
    if SIGNAL_JSONL.exists():
        try:
            for line in open(SIGNAL_JSONL, "r", errors="replace"):
                rec = json.loads(line.strip())
                cid = rec.get("condition_id")
                if cid:
                    seen.add(cid)
        except Exception:
            pass
    return seen


def build_category_map(gamma_events: list[dict]) -> dict[str, str]:
    """
    Build {condition_id: category} from Gamma events.
    Tags on gamma events carry category info.
    """
    cat_map = {}
    for ev in gamma_events:
        markets = ev.get("markets", [])
        tags    = ev.get("tags", [])
        if not tags:
            continue
        # Determine category from first allowed tag match
        category = None
        for tag in tags:
            slug = (tag.get("slug", "") or tag.get("label", "") or "").lower()
            if slug in ALLOWED_CATEGORIES:
                category = slug
                break
        if not category:
            continue
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                cat_map[cid] = category
    return cat_map


def is_sports(tags: set[str]) -> bool:
    return bool(tags & SPORTS_TAGS)


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

    for mkt in markets:
        try:
            cid = mkt.get("condition_id") or mkt.get("conditionId", "")
            if not cid or cid in seen_ids:
                continue

            # Binary only
            if not is_binary_market(mkt):
                continue

            # Sports filter
            tags = get_market_tags(mkt)
            if is_sports(tags):
                continue

            # Category filter — must be in gamma cat_map with allowed category
            category = cat_map.get(cid, "")
            if not category:
                # Fallback: check tags directly
                for t in tags:
                    if t in ALLOWED_CATEGORIES:
                        category = t
                        break
            if not category:
                continue

            # Volume / liquidity
            vol24  = float(mkt.get("volume24hr", mkt.get("volume_24h", 0)) or 0)
            liq    = float(mkt.get("liquidity",  0) or 0)
            if vol24 < MIN_VOLUME_24H or liq < MIN_LIQUIDITY:
                continue

            # YES token
            yes_tok = extract_yes_token(mkt)
            if not yes_tok:
                continue
            token_id = yes_tok.get("token_id") or yes_tok.get("tokenId", "")
            if not token_id:
                continue

            # Current YES price
            yes_now = fetch_last_trade_price(token_id)
            if yes_now is None:
                continue

            # Price range filter
            if not (YES_PRICE_MIN <= yes_now <= YES_PRICE_MAX):
                continue

            # 60-min baseline price
            yes_60m = get_price_60min_ago(token_id)
            if yes_60m is None or yes_60m <= 0:
                continue

            move_pct = (yes_now - yes_60m) / yes_60m * 100

            if move_pct < ENTRY_MIN_MOVE_60MIN:
                continue

            # ── Signal confirmed ───────────────────────────────────
            base_rate, br_label = base_rate_lookup(mkt.get("question", ""))
            news = fetch_news_at_signal(mkt.get("question", ""), FINNHUB_API_KEY)

            no_tok   = next((t for t in mkt.get("tokens", []) if
                             (t.get("outcome") or "").lower() == "no"), None)
            no_token_id = no_tok.get("token_id", "") if no_tok else ""

            record = {
                "signal_id":        f"{cid}_{int(time.time())}",
                "condition_id":     cid,
                "question":         mkt.get("question", ""),
                "category":         category,
                "token_id_yes":     token_id,
                "token_id_no":      no_token_id,
                "yes_price_60m":    round(yes_60m,  4),
                "yes_price_now":    round(yes_now,  4),
                "move_pct":         round(move_pct, 2),
                "volume_24h":       vol24,
                "liquidity":        liq,
                "base_rate":        base_rate,
                "base_rate_label":  br_label,
                "news_at_signal":   news,
                "news_annotation":  "",        # human fills post-resolution
                "signal_ts":        time.time(),
                "signal_iso":       datetime.now(timezone.utc).isoformat(),
                "status":           "new",     # new → watching → entered/discarded
            }
            emit_signal(record)
            seen_ids.add(cid)
            signals += 1

        except Exception as e:
            log(f"ERROR processing market {mkt.get('condition_id','?')}: {e}")

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
