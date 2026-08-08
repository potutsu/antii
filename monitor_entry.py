"""
monitor_entry.py — Watches new signals, polls every 2 min, issues verdicts.

Entry rules:
  - ENTER if YES price drops >= ENTRY_MIN_REVERT_PCT% from signal price
  - ENTER (timeout) after ENTRY_MAX_WAIT_MIN if price still in valid range
  - DISCARD if price moves outside YES_PRICE_MIN..YES_PRICE_MAX (signal invalidated)
  - DISCARD if signal_ts is older than ENTRY_MAX_WAIT_MIN and price is out of range

Verdicts written to data/verdicts.jsonl
Signal status updated in data/signal.jsonl (rewrite on change)
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import (
    MONITOR_ENTRY_POLL_SEC,
    ENTRY_MIN_REVERT_PCT,
    ENTRY_MAX_WAIT_MIN,
    YES_PRICE_MIN,
    YES_PRICE_MAX,
    MAX_WATCHED_SIGNALS,
    MAX_OPEN_POSITIONS,
    SIGNAL_MODE,
)
from paths import ensure_dirs, SIGNAL_JSONL, VERDICTS_JSONL, PAPER_POSITIONS
from polymarket import fetch_last_trade_price


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [monitor_entry] {msg}", flush=True)


def load_signals() -> list[dict]:
    signals = []
    if not SIGNAL_JSONL.exists():
        return signals
    for line in open(SIGNAL_JSONL, "r", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            signals.append(json.loads(line))
        except Exception:
            pass
    return signals


def save_signals(signals: list[dict]):
    tmp = SIGNAL_JSONL.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for s in signals:
            f.write(json.dumps(s) + "\n")
    tmp.replace(SIGNAL_JSONL)


def count_open_positions() -> int:
    if not PAPER_POSITIONS.exists():
        return 0
    count = 0
    for line in open(PAPER_POSITIONS, "r", errors="replace"):
        try:
            p = json.loads(line.strip())
            if p.get("status") == "open":
                count += 1
        except Exception:
            pass
    return count


def emit_verdict(verdict: dict):
    with open(VERDICTS_JSONL, "a") as f:
        f.write(json.dumps(verdict) + "\n")


def make_verdict(signal: dict, action: str, current_price: float, reason: str) -> dict:
    return {
        "verdict_id":   f"{signal['signal_id']}_v",
        "signal_id":    signal["signal_id"],
        "condition_id": signal["condition_id"],
        "question":     signal["question"],
        "category":     signal["category"],
        "token_id_yes": signal["token_id_yes"],
        "token_id_no":  signal["token_id_no"],
        "action":       action,
        "reason":       reason,
        "entry_price":  round(current_price, 4),
        "signal_price": signal["yes_price_now"],
        "verdict_ts":   time.time(),
        "verdict_iso":  datetime.now(timezone.utc).isoformat(),
        "volume_24h":   signal.get("volume_24h", 0),
        "liquidity":    signal.get("liquidity", 0),
        "base_rate":    signal.get("base_rate", 0),
        "news_at_signal":  signal.get("news_at_signal", []),
        # pass wallet copy fields through to position
        "source":          signal.get("source", ""),
        "source_wallet":   signal.get("source_wallet", ""),
        "wallet_side":     signal.get("wallet_side", ""),
        "wallet_outcome":  signal.get("wallet_outcome", ""),
        "wallet_price":    signal.get("wallet_price", None),
        "source_win_rate": signal.get("source_win_rate", None),
    }


def process_signal(sig: dict, open_positions: int) -> tuple[str | None, dict | None, float | None]:
    """
    Returns (new_status, verdict_or_None, current_price_or_None)
    new_status: None = no change, "watching", "entered", "discarded"

    SIGNAL_MODE=wallet_copy : enter immediately, no reversion wait
    SIGNAL_MODE=overreaction: original logic, wait for 3% reversion
    """
    token_id  = sig["token_id_yes"]
    signal_px = sig["yes_price_now"]
    signal_ts = sig["signal_ts"]
    age_min   = (time.time() - signal_ts) / 60.0

    try:
        current_px = fetch_last_trade_price(token_id)
    except Exception as e:
        log(f"price fetch error for {sig['condition_id']}: {e}")
        return None, None, None

    if current_px is None:
        return None, None, None

    # ── Position cap — both modes ──────────────────────────────────
    if open_positions >= MAX_OPEN_POSITIONS:
        v = make_verdict(sig, "DISCARD", current_px, "position_cap_reached")
        return "discarded", v, current_px

    # ══════════════════════════════════════════════════════════════
    # WALLET COPY MODE — enter immediately on signal
    # ══════════════════════════════════════════════════════════════
    if SIGNAL_MODE == "wallet_copy":
        # Only discard if signal is stale (> 10 min old, wallet already moved on)
        if age_min > 10:
            v = make_verdict(sig, "DISCARD", current_px, "stale_wallet_signal")
            return "discarded", v, current_px
        v = make_verdict(sig, "ENTER", current_px, "wallet_copy_immediate")
        return "entered", v, current_px

    # ══════════════════════════════════════════════════════════════
    # OVERREACTION MODE — wait for reversion
    # ══════════════════════════════════════════════════════════════
    # Out of valid price range — discard
    if not (YES_PRICE_MIN <= current_px <= YES_PRICE_MAX):
        v = make_verdict(sig, "DISCARD", current_px, "price_out_of_range")
        return "discarded", v, current_px

    # Reversion confirmed — enter
    drop_pct = (signal_px - current_px) / signal_px * 100
    if drop_pct >= ENTRY_MIN_REVERT_PCT:
        v = make_verdict(sig, "ENTER", current_px, "reversion_confirmed")
        return "entered", v, current_px

    # Timeout — discard if no reversion within watch window
    if age_min >= ENTRY_MAX_WAIT_MIN:
        v = make_verdict(sig, "DISCARD", current_px, "no_reversion_timeout")
        return "discarded", v, current_px

    return "watching", None, current_px


def main():
    ensure_dirs()
    log("monitor_entry started")

    while True:
        try:
            signals       = load_signals()
            open_pos      = count_open_positions()
            changed       = False
            watching_count = 0

            for sig in signals:
                status = sig.get("status", "new")

                if status not in ("new", "watching"):
                    continue

                # Cap simultaneous watches
                if watching_count >= MAX_WATCHED_SIGNALS:
                    break

                watching_count += 1

                new_status, verdict, px = process_signal(sig, open_pos)

                if new_status == "watching" and status == "new":
                    sig["status"] = "watching"
                    changed = True
                    log(f"watching {sig['condition_id'][:16]}  YES={px:.3f}")
                    continue

                if new_status in ("entered", "discarded") and verdict:
                    sig["status"] = new_status
                    emit_verdict(verdict)
                    changed = True
                    log(f"{verdict['action']} {sig['condition_id'][:16]}  "
                        f"reason={verdict['reason']}  px={px:.3f}")
                    if new_status == "entered":
                        open_pos += 1   # optimistic count for this loop pass

            if changed:
                save_signals(signals)

        except Exception as e:
            log(f"ERROR in monitor loop: {e}")

        time.sleep(MONITOR_ENTRY_POLL_SEC)


if __name__ == "__main__":
    main()
