"""
monitor_position.py — Watches open positions, polls every 5 min.

Exit rules (all relative to YES price, because we hold NO):
  - TAKE_PROFIT: YES price drops >= EXIT_REVERT_PCT% from entry_yes_price
  - STOP_LOSS:   YES price rises >= EXIT_STOP_LOSS_PCT% from entry_yes_price
  - MAX_HOLD:    position age >= EXIT_MAX_HOLD_HOURS

Writes EXIT verdicts to verdicts.jsonl (trader.py closes the position).
Tracks per-position peak/trough for MFE/MAE (stored in shadow).
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import (
    MONITOR_POS_POLL_SEC,
    EXIT_REVERT_PCT,
    EXIT_STOP_LOSS_PCT,
    EXIT_MAX_HOLD_HOURS,
)
from paths import ensure_dirs, PAPER_POSITIONS, VERDICTS_JSONL
from polymarket import fetch_last_trade_price


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [monitor_position] {msg}", flush=True)


def load_open_positions() -> list[dict]:
    if not PAPER_POSITIONS.exists():
        return []
    positions = []
    for line in open(PAPER_POSITIONS, "r", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
            if p.get("status") == "open":
                positions.append(p)
        except Exception:
            pass
    return positions


def load_emitted_exit_ids() -> set[str]:
    """Position IDs for which we already emitted an EXIT verdict this session."""
    seen = set()
    if not VERDICTS_JSONL.exists():
        return seen
    for line in open(VERDICTS_JSONL, "r", errors="replace"):
        try:
            v = json.loads(line.strip())
            if v.get("action") == "EXIT":
                pid = v.get("position_id")
                if pid:
                    seen.add(pid)
        except Exception:
            pass
    return seen


def emit_exit_verdict(pos: dict, exit_price: float, reason: str):
    entry_yes = pos["entry_yes_price"]
    move_from_entry = round((exit_price - entry_yes) / entry_yes * 100, 2)

    verdict = {
        "verdict_id":      f"{pos['position_id']}_exit_{int(time.time())}",
        "position_id":     pos["position_id"],
        "signal_id":       pos["signal_id"],
        "condition_id":    pos["condition_id"],
        "question":        pos["question"],
        "action":          "EXIT",
        "exit_reason":     reason,
        "exit_price":      round(exit_price, 4),
        "entry_yes_price": entry_yes,
        "move_from_entry_pct": move_from_entry,
        "verdict_ts":      time.time(),
        "verdict_iso":     datetime.now(timezone.utc).isoformat(),
    }
    with open(VERDICTS_JSONL, "a") as f:
        f.write(json.dumps(verdict) + "\n")
    log(f"EXIT {pos['position_id']}  reason={reason}  "
        f"yes_px={exit_price:.3f}  move={move_from_entry:+.1f}%")


def check_position(pos: dict) -> tuple[bool, float | None, str]:
    """
    Returns (should_exit, current_yes_price, reason)
    """
    entry_yes   = pos["entry_yes_price"]
    open_ts     = pos.get("open_ts", time.time())
    age_hours   = (time.time() - open_ts) / 3600.0
    token_id    = pos["token_id_yes"]

    # Max hold check first (no price fetch needed)
    if age_hours >= EXIT_MAX_HOLD_HOURS:
        try:
            px = fetch_last_trade_price(token_id)
        except Exception:
            px = entry_yes  # use entry as fallback for forced exit
        return True, px, "max_hold"

    try:
        px = fetch_last_trade_price(token_id)
    except Exception as e:
        log(f"price error {pos['position_id']}: {e}")
        return False, None, ""

    if px is None:
        return False, None, ""

    drop_pct = (entry_yes - px) / entry_yes * 100   # positive = YES fell = we win
    rise_pct = (px - entry_yes) / entry_yes * 100    # positive = YES rose = we lose

    if drop_pct >= EXIT_REVERT_PCT:
        return True, px, "take_profit"

    if rise_pct >= EXIT_STOP_LOSS_PCT:
        return True, px, "stop_loss"

    return False, px, ""


def main():
    ensure_dirs()
    log("monitor_position started")
    emitted_exits = load_emitted_exit_ids()

    while True:
        try:
            open_positions = load_open_positions()

            for pos in open_positions:
                pid = pos["position_id"]
                if pid in emitted_exits:
                    continue

                try:
                    should_exit, px, reason = check_position(pos)
                    if should_exit and px is not None:
                        emit_exit_verdict(pos, px, reason)
                        emitted_exits.add(pid)
                    elif px is not None:
                        age_h = (time.time() - pos.get("open_ts", time.time())) / 3600
                        log(f"watching {pid}  yes={px:.3f}  age={age_h:.1f}h")
                except Exception as e:
                    log(f"ERROR checking {pid}: {e}")

        except Exception as e:
            log(f"ERROR in position loop: {e}")

        time.sleep(MONITOR_POS_POLL_SEC)


if __name__ == "__main__":
    main()
