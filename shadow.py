"""
shadow.py — Price data collector. Always-on background worker.

For every signal in signal.jsonl:
  - Pre-open: logs YES price every 15 min until position opens or signal discarded
  - Post-open: continues logging every 15 min while position is open
  - Post-close: logs for 72 hours after position closes
  - No verdict logic — pure data collection

Writes to: data/shadow.jsonl
Each row: {signal_id, condition_id, yes_price, no_price, elapsed_sec,
           phase, position_status, logged_at_ts, logged_at_iso}
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import SHADOW_POLL_SEC, SHADOW_POST_CLOSE_HOURS
from paths import ensure_dirs, SIGNAL_JSONL, PAPER_POSITIONS, SHADOW_LOG
from polymarket import fetch_last_trade_price


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [shadow] {msg}", flush=True)


def load_signals() -> list[dict]:
    if not SIGNAL_JSONL.exists():
        return []
    signals = []
    for line in open(SIGNAL_JSONL, "r", errors="replace"):
        line = line.strip()
        if line:
            try:
                signals.append(json.loads(line))
            except Exception:
                pass
    return signals


def load_positions_by_signal() -> dict[str, dict]:
    """Returns {signal_id: position_dict} for all positions."""
    if not PAPER_POSITIONS.exists():
        return {}
    result = {}
    for line in open(PAPER_POSITIONS, "r", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
            sid = p.get("signal_id")
            if sid:
                result[sid] = p
        except Exception:
            pass
    return result


def load_already_logged() -> dict[str, float]:
    """
    Returns {signal_id: last_log_ts} so we don't re-log too eagerly.
    """
    last_log = {}
    if not SHADOW_LOG.exists():
        return last_log
    for line in open(SHADOW_LOG, "r", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            sid = r.get("signal_id")
            lts = r.get("logged_at_ts", 0)
            if sid and lts > last_log.get(sid, 0):
                last_log[sid] = lts
        except Exception:
            pass
    return last_log


def should_log(signal: dict, pos: dict | None, last_log_ts: float) -> tuple[bool, str]:
    """
    Returns (should_log, phase).
    phase: pre_open | open | post_close | expired
    """
    now      = time.time()
    sig_status = signal.get("status", "new")

    # Never log discarded signals
    if sig_status == "discarded":
        return False, "expired"

    # Respect 15-min interval
    if now - last_log_ts < SHADOW_POLL_SEC:
        return False, ""

    if pos is None:
        # No position yet — pre-open phase
        # Stop logging if signal is very old and not entered
        signal_age_h = (now - signal.get("signal_ts", now)) / 3600
        if signal_age_h > 4:   # give up shadow if signal aged out with no entry
            return False, "expired"
        return True, "pre_open"

    pos_status = pos.get("status", "open")

    if pos_status == "open":
        return True, "open"

    if pos_status == "closed":
        close_ts   = pos.get("close_ts", now)
        hours_since = (now - close_ts) / 3600
        if hours_since <= SHADOW_POST_CLOSE_HOURS:
            return True, "post_close"
        return False, "expired"

    return False, "expired"


def write_shadow_row(signal: dict, yes_price: float | None, phase: str, pos: dict | None):
    now = time.time()
    row = {
        "signal_id":       signal["signal_id"],
        "condition_id":    signal["condition_id"],
        "question":        signal.get("question", "")[:80],
        "yes_price":       round(yes_price, 4) if yes_price is not None else None,
        "no_price":        round(1 - yes_price, 4) if yes_price is not None else None,
        "signal_yes_price": signal.get("yes_price_now"),
        "elapsed_sec":     round(now - signal.get("signal_ts", now)),
        "phase":           phase,
        "position_status": pos.get("status") if pos else None,
        "position_id":     pos.get("position_id") if pos else None,
        "logged_at_ts":    now,
        "logged_at_iso":   datetime.now(timezone.utc).isoformat(),
    }
    with open(SHADOW_LOG, "a") as f:
        f.write(json.dumps(row) + "\n")


def main():
    ensure_dirs()
    log(f"shadow started  poll={SHADOW_POLL_SEC}s  post_close={SHADOW_POST_CLOSE_HOURS}h")

    # Track which signal_ids we've deemed expired to skip re-checking
    expired_ids: set[str] = set()

    while True:
        try:
            signals   = load_signals()
            positions = load_positions_by_signal()
            last_logs = load_already_logged()
            logged    = 0

            for sig in signals:
                sid = sig["signal_id"]
                if sid in expired_ids:
                    continue

                pos         = positions.get(sid)
                last_log_ts = last_logs.get(sid, 0)

                ok, phase = should_log(sig, pos, last_log_ts)
                if not ok:
                    if phase == "expired":
                        expired_ids.add(sid)
                    continue

                try:
                    yes_px = fetch_last_trade_price(sig["token_id_yes"])
                except Exception as e:
                    log(f"price error {sid[:16]}: {e}")
                    yes_px = None

                write_shadow_row(sig, yes_px, phase, pos)
                last_logs[sid] = time.time()
                logged += 1

            if logged:
                log(f"logged {logged} shadow rows")

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(30)   # inner loop sleeps 30s; per-signal throttle is SHADOW_POLL_SEC


if __name__ == "__main__":
    main()
