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
    EXIT_RESOLUTION_PCT,
    SIGNAL_MODE,
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

    SIGNAL_MODE=wallet_copy:
        - Exit when wallet exits same market (SELL detected in activity)
        - Fallback: max_hold, resolution, stop_loss (wider: 25%)
        - No take_profit based on % drop — let wallet decide when to exit

    SIGNAL_MODE=overreaction:
        - Original logic: take_profit on DROP, stop_loss on RISE
    """
    entry_yes = pos["entry_yes_price"]
    open_ts   = pos.get("open_ts", time.time())
    age_hours = (time.time() - open_ts) / 3600.0
    token_id  = pos["token_id_yes"]

    # ── Max hold — both modes ──────────────────────────────────────
    if age_hours >= EXIT_MAX_HOLD_HOURS:
        try:
            px = fetch_last_trade_price(token_id)
        except Exception:
            px = entry_yes
        return True, px, "max_hold"

    try:
        px = fetch_last_trade_price(token_id)
    except Exception as e:
        log(f"price error {pos['position_id']}: {e}")
        return False, None, ""

    if px is None:
        return False, None, ""

    # ── Resolution detection — both modes ──────────────────────────
    if px <= EXIT_RESOLUTION_PCT / 100:
        return True, px, "resolution"

    rise_pct = (px - entry_yes) / entry_yes * 100
    drop_pct = (entry_yes - px) / entry_yes * 100

    # ══════════════════════════════════════════════════════════════
    # WALLET COPY MODE
    # Primary exit: wallet sells → we exit
    # Fallback exits: hard stop loss (35%) or max_hold
    # NO take_profit based on % — we follow the wallet, not a % target
    # ══════════════════════════════════════════════════════════════
    if SIGNAL_MODE == "wallet_copy":
        # ── Primary: wallet exit signal ───────────────────────────
        # Only check every 3rd poll to avoid rate limiting
        # (monitor_position runs every 5 min → check every 15 min)
        check_ts  = pos.get("_last_wallet_check_ts", 0)
        check_due = (time.time() - check_ts) >= 900  # 15 min
        if check_due:
            source_wallet = pos.get("source_wallet", "")
            condition_id  = pos.get("condition_id", "")
            if source_wallet and condition_id:
                if check_wallet_exited(source_wallet, condition_id):
                    return True, px, "wallet_exited"

        # ── Hard stop loss only — wide, 35% ───────────────────────
        # Don't TP on % drop. Let the wallet decide when to exit.
        # Only cut if market moves hard against us.
        COPY_STOP_LOSS_PCT = 35.0
        if rise_pct >= COPY_STOP_LOSS_PCT:
            return True, px, "stop_loss"

        return False, px, ""

    # ══════════════════════════════════════════════════════════════
    # OVERREACTION MODE — original logic
    # ══════════════════════════════════════════════════════════════
    if drop_pct >= EXIT_REVERT_PCT:
        return True, px, "take_profit"

    if rise_pct >= EXIT_STOP_LOSS_PCT:
        return True, px, "stop_loss"

    return False, px, ""


def check_wallet_exited(wallet: str, condition_id: str) -> bool:
    """Check if source wallet has recently sold this market (exit signal)."""
    try:
        import requests
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": wallet, "limit": 20},
            headers={"User-Agent": "antii/2.0", "Referer": "https://polymarket.com/"},
            timeout=10,
        )
        if not r.ok:
            return False
        for trade in r.json() or []:
            if (trade.get("conditionId") == condition_id
                    and trade.get("type") == "TRADE"
                    and trade.get("side", "").upper() == "SELL"):
                return True
    except Exception:
        pass
    return False


def main():
    ensure_dirs()
    log("monitor_position started")
    emitted_exits  = load_emitted_exit_ids()
    last_price_seen: dict[str, tuple[float, float]] = {}  # pid → (price, first_seen_ts)

    while True:
        try:
            open_positions = load_open_positions()

            for pos in open_positions:
                pid = pos["position_id"]
                if pid in emitted_exits:
                    continue

                try:
                    should_exit, px, reason = check_position(pos)

                    # ── Stale price guard ──────────────────────────
                    # If the price hasn't moved in 24h the market is
                    # likely resolved or dead. Force-close to free capital.
                    if px is not None:
                        prev_px, first_ts = last_price_seen.get(pid, (px, time.time()))
                        if abs(px - prev_px) < 0.001:
                            stale_hours = (time.time() - first_ts) / 3600
                            if stale_hours >= 24:
                                should_exit = True
                                reason = "stale_price"
                        else:
                            last_price_seen[pid] = (px, time.time())
                        if pid not in last_price_seen:
                            last_price_seen[pid] = (px, time.time())

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
