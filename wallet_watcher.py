"""
wallet_watcher.py — Wallet copy signal source for antii
Replaces discovery.py as the signal emitter.

What it does every 5 min:
  1. Loads edge_combos.csv (wallet × category winners from analyzer)
  2. For each wallet, fetches current open positions via Polymarket Data API
  3. Checks wallet was active in last 30 days (skips stale wallets)
  4. For any NEW position not seen before:
     - Validates market is liquid enough
     - Fetches news snapshot via NewsData.io (optional, needs API key)
     - Emits signal record into signal.jsonl in exact antii format
  5. Shadow + monitor_entry + trader pick it up automatically

Config needed in antii_config.py (add these):
    NEWSDATA_API_KEY  = os.environ.get("NEWSDATA_API_KEY", "")
    WALLET_COMBOS_CSV = str(BASE_DIR / "data" / "edge_combos.csv")
    WALLET_ACTIVITY_DAYS = 30
    WALLET_POLL_SEC  = 300

Or just set env vars:
    export NEWSDATA_API_KEY=your_key_here
"""

import csv, hashlib, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import (
    MIN_VOLUME_24H, MIN_LIQUIDITY,
    ALLOWED_CATEGORIES, DISCOVERY_INTERVAL_SEC,
)
from paths import ensure_dirs, SIGNAL_JSONL
from polymarket import fetch_last_trade_price, fetch_gamma_events

# ── Config (extend antii_config or use env) ───────────────────────
NEWSDATA_API_KEY    = os.environ.get("NEWSDATA_API_KEY", "")
WALLET_COMBOS_CSV   = os.environ.get(
    "WALLET_COMBOS_CSV",
    str(Path(__file__).resolve().parent / "data" / "edge_combos.csv")
)
WALLET_ACTIVITY_DAYS = int(os.environ.get("WALLET_ACTIVITY_DAYS", 30))
POLL_SEC             = int(os.environ.get("WALLET_POLL_SEC", DISCOVERY_INTERVAL_SEC))
MAX_NEWS_HEADLINES   = 5
MAX_POSITIONS_FETCH  = 100   # per wallet per poll

DATA_API = "https://data-api.polymarket.com"
GAMMA    = "https://gamma-api.polymarket.com"

import requests
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "antii-wallet-watcher/2.0",
    "Accept":     "application/json",
    "Referer":    "https://polymarket.com/",
})


# ── Logging ───────────────────────────────────────────────────────
def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    print(f"[{ts()}] [wallet_watcher] {msg}", flush=True)


# ── Helpers ───────────────────────────────────────────────────────
def _get(url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                log("rate limited, waiting 10s")
                time.sleep(10)
            elif attempt == retries:
                return None
        except Exception:
            if attempt == retries:
                return None
            time.sleep(2)
    return None


# ── Load edge combos ──────────────────────────────────────────────
def load_combos() -> list[dict]:
    """Load wallet × category combos from edge_combos.csv."""
    path = Path(WALLET_COMBOS_CSV)
    if not path.exists():
        log(f"edge_combos.csv not found at {path} — run wallet_edge_analyzer.py first")
        return []
    combos = []
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                addr = row.get("full_address") or row.get("wallet", "")
                cat  = row.get("category", "unknown").lower().strip()
                if addr and cat:
                    combos.append({
                        "address":  addr.strip(),
                        "category": cat,
                        "win_rate": float(row.get("win_%") or row.get("win_rate", 0)),
                        "trades":   int(row.get("trades", 0)),
                    })
    except Exception as e:
        log(f"error loading combos: {e}")
    log(f"loaded {len(combos)} wallet × category combos")
    return combos


# ── Activity check ────────────────────────────────────────────────
def get_last_activity_days(address: str) -> float:
    """Returns days since last trade. Uses activity endpoint with unix timestamp field."""
    try:
        data = _get(f"{DATA_API}/activity", {
            "user":  address,
            "limit": 5,
        })
        if not data or not isinstance(data, list):
            return 999
        timestamps = []
        for p in data:
            ts_val = p.get("timestamp")
            if ts_val:
                try:
                    timestamps.append(float(ts_val))
                except Exception:
                    pass
        if not timestamps:
            return 999
        latest = max(timestamps)
        days = (time.time() - latest) / 86400
        return round(days, 1)
    except Exception as e:
        return 999


# ── Fetch recent activity ────────────────────────────────────────
ACTIVITY_LOOKBACK_DAYS = int(os.environ.get("ACTIVITY_LOOKBACK_DAYS", 7))

def get_recent_activity(address: str, limit: int = 100) -> list[dict]:
    """Fetch recent BUY trades from the last ACTIVITY_LOOKBACK_DAYS days."""
    try:
        data = _get(f"{DATA_API}/activity", {
            "user":  address,
            "limit": limit,
        })
        if not data or not isinstance(data, list):
            return []
        cutoff = time.time() - (ACTIVITY_LOOKBACK_DAYS * 86400)
        trades = [
            t for t in data
            if t.get("type") == "TRADE"
            and float(t.get("timestamp") or 0) >= cutoff
        ]
        return trades
    except Exception:
        return []


# ── Enrich position with market metadata ─────────────────────────
def get_market_meta(condition_id: str) -> dict:
    """Get question, tokens, volume, liquidity from Gamma."""
    try:
        data = _get(f"{GAMMA}/markets", {"conditionId": condition_id})
        markets = data if isinstance(data, list) else (data or {}).get("markets", [])
        if not markets:
            return {}
        m = markets[0]
        tokens   = m.get("tokens") or m.get("clobTokenIds") or []
        token_yes, token_no = "", ""
        if isinstance(tokens, list) and len(tokens) >= 2:
            token_yes = tokens[0].get("token_id", "") if isinstance(tokens[0], dict) else tokens[0]
            token_no  = tokens[1].get("token_id", "") if isinstance(tokens[1], dict) else tokens[1]
        return {
            "question":     m.get("question", ""),
            "token_id_yes": token_yes,
            "token_id_no":  token_no,
            "volume_24h":   float(m.get("volume24hr") or m.get("volumeNum") or 0),
            "liquidity":    float(m.get("liquidity") or m.get("liquidityNum") or 0),
            "end_date":     m.get("endDate") or m.get("end_date_iso") or "",
        }
    except Exception:
        return {}


# ── News fetch ────────────────────────────────────────────────────
CATEGORY_TO_NEWS = {
    "politics":   "politics",
    "economics":  "business",
    "crypto":     "technology",
    "sports":     "sports",
    "esports":    "entertainment",
    "culture":    "entertainment",
}

def fetch_news(question: str, category: str) -> list[str]:
    """Fetch relevant headlines via NewsData.io free tier."""
    if not NEWSDATA_API_KEY:
        return []
    try:
        # Extract keywords from question (skip stop words)
        stop = {"will", "the", "be", "to", "in", "of", "a", "an", "by", "at",
                "for", "is", "are", "or", "and", "on", "with", "does", "do",
                "reach", "hit", "win", "lose", "who", "what", "when", "which"}
        words    = [w.strip("?.,!") for w in question.split() if len(w) > 3]
        keywords = [w for w in words if w.lower() not in stop]
        caps     = [w for w in keywords if w[0].isupper()]
        query    = " ".join((caps or keywords)[:3])
        if not query:
            return []

        news_cat = CATEGORY_TO_NEWS.get(category, "")
        params = {
            "apikey":   NEWSDATA_API_KEY,
            "q":        query,
            "language": "en",
        }
        if news_cat:
            params["category"] = news_cat

        r = SESSION.get("https://newsdata.io/api/1/news", params=params, timeout=10)
        if r.status_code != 200:
            return []

        articles = r.json().get("results", [])
        headlines = []
        for a in articles[:MAX_NEWS_HEADLINES]:
            title = a.get("title", "").strip()
            if title:
                headlines.append(title)
        return headlines
    except Exception:
        return []


# ── Signal dedup ──────────────────────────────────────────────────
def load_seen_signal_ids() -> set:
    seen = set()
    if not SIGNAL_JSONL.exists():
        return seen
    for line in open(SIGNAL_JSONL, errors="replace"):
        try:
            s = json.loads(line.strip())
            seen.add(s.get("signal_id", ""))
        except Exception:
            pass
    return seen


def make_signal_id(condition_id: str, wallet: str) -> str:
    raw = f"{condition_id}_{wallet}_wallet"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()


# ── Emit signal ───────────────────────────────────────────────────
def emit_signal(record: dict):
    with open(SIGNAL_JSONL, "a") as f:
        f.write(json.dumps(record) + "\n")
    log(f"SIGNAL → {record['question'][:60]}  cat={record['category']}  "
        f"yes={record['yes_price_now']:.3f}  wallet={record.get('source_wallet','')[:12]}")


# ── Main scan ─────────────────────────────────────────────────────
def scan(combos: list[dict], seen_ids: set):
    new_signals = 0

    # Cache per address so we don't re-fetch same wallet for multiple categories
    wallets_seen_this_scan: dict[str, list] = {}  # address -> activity list

    for combo in combos:
        address  = combo["address"]
        category = combo["category"]

        # ── Fetch activity (once per wallet per scan) ─────────────
        if address not in wallets_seen_this_scan:
            activity_days = get_last_activity_days(address)
            if activity_days > WALLET_ACTIVITY_DAYS:
                log(f"skip {address[:12]}… inactive ({activity_days:.1f}d)")
                wallets_seen_this_scan[address] = []
                time.sleep(0.3)
                continue
            trades = get_recent_activity(address, limit=50)
            wallets_seen_this_scan[address] = trades
            log(f"active {address[:12]}… {activity_days:.1f}d ago | {len(trades)} recent trades")
            time.sleep(0.4)

        trades = wallets_seen_this_scan[address]
        if not trades:
            continue

        for trade in trades:
            condition_id = trade.get("conditionId")
            if not condition_id:
                continue

            signal_id = make_signal_id(condition_id, address)
            if signal_id in seen_ids:
                continue

            # ── Trade direction from activity ─────────────────────
            # side=BUY outcome=Yes  → wallet is bullish YES
            # side=BUY outcome=No   → wallet is bullish NO
            side    = trade.get("side", "").upper()    # BUY | SELL
            outcome = trade.get("outcome", "").upper() # Yes | No
            wallet_price = float(trade.get("price") or 0)

            # Only follow BUY signals — wallet is opening/adding, not exiting
            if side != "BUY":
                continue

            # ── Get market metadata ───────────────────────────────
            meta = get_market_meta(condition_id)
            if not meta.get("token_id_yes"):
                continue

            # ── Resolution date filter — only markets closing within 30 days ──
            end_date = meta.get("end_date")
            if end_date:
                try:
                    from datetime import date
                    end_dt = date.fromisoformat(str(end_date)[:10])
                    days_to_close = (end_dt - date.today()).days
                    if days_to_close > 30 or days_to_close < 0:
                        continue
                except Exception:
                    pass  # if we can't parse, allow through

            # ── Liquidity filter ──────────────────────────────────
            if meta.get("volume_24h", 0) < MIN_VOLUME_24H:
                continue
            if meta.get("liquidity", 0) < MIN_LIQUIDITY:
                continue

            # ── Get current YES price ─────────────────────────────
            try:
                yes_price = fetch_last_trade_price(meta["token_id_yes"])
            except Exception:
                yes_price = None

            if yes_price is None:
                continue

            # ── News ──────────────────────────────────────────────
            question = meta.get("question") or trade.get("title", "")
            news = fetch_news(question, category)
            time.sleep(0.2)

            # ── Emit signal ───────────────────────────────────────
            now = time.time()
            record = {
                "signal_id":          signal_id,
                "condition_id":       condition_id,
                "question":           question,
                "category":           category,
                "token_id_yes":       meta["token_id_yes"],
                "token_id_no":        meta["token_id_no"],
                "yes_price_now":      round(yes_price, 4),
                "move_pct":           0.0,
                "volume_24h":         meta.get("volume_24h", 0),
                "liquidity":          meta.get("liquidity", 0),
                "signal_ts":          now,
                "signal_iso":         datetime.now(timezone.utc).isoformat(),
                "status":             "new",
                "source":             "wallet_copy",
                "wallet_side":        side,
                "wallet_outcome":     outcome,
                "wallet_price":       wallet_price,
                "source_wallet":   address,
                "source_win_rate": combo["win_rate"],
                "source_trades":   combo["trades"],
                "news_at_signal":  news,
                "news_annotation": "",
                "base_rate":       0.5,
            }
            emit_signal(record)
            seen_ids.add(signal_id)
            new_signals += 1
            time.sleep(0.4)

    return new_signals


# ── Entry point ───────────────────────────────────────────────────
def main():
    ensure_dirs()
    log(f"wallet_watcher started  poll={POLL_SEC}s  activity_window={WALLET_ACTIVITY_DAYS}d")

    if not NEWSDATA_API_KEY:
        log("NEWSDATA_API_KEY not set — news disabled (set env var to enable)")

    combos = load_combos()
    if not combos:
        log("no combos loaded — exiting. Run wallet_edge_analyzer.py first.")
        return

    log(f"watching {len(combos)} wallet × category combos")

    seen_ids = load_seen_signal_ids()
    log(f"pre-loaded {len(seen_ids)} existing signal IDs")

    while True:
        try:
            log(f"scanning {len(combos)} combos...")
            n = scan(combos, seen_ids)
            log(f"scan complete — {n} new signals emitted")
        except Exception as e:
            log(f"ERROR in scan: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
