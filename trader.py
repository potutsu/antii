"""
trader.py — Consumes verdicts.jsonl, opens/closes paper positions.

On ENTER verdict  → open paper position in paper_positions.jsonl
On EXIT verdict   → close paper position, record P&L, write trade.jsonl

We buy NO tokens when YES pumps (fade the spike).
Entry price = YES price at verdict time → our NO cost = 1 - YES price.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import NOTIONAL_PER_TRADE, MODE
from paths import ensure_dirs, VERDICTS_JSONL, PAPER_POSITIONS, TRADE_LOG


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [trader] {msg}", flush=True)


def load_verdicts() -> list[dict]:
    if not VERDICTS_JSONL.exists():
        return []
    verdicts = []
    for line in open(VERDICTS_JSONL, "r", errors="replace"):
        line = line.strip()
        if line:
            try:
                verdicts.append(json.loads(line))
            except Exception:
                pass
    return verdicts


def load_positions() -> list[dict]:
    if not PAPER_POSITIONS.exists():
        return []
    positions = []
    for line in open(PAPER_POSITIONS, "r", errors="replace"):
        line = line.strip()
        if line:
            try:
                positions.append(json.loads(line))
            except Exception:
                pass
    return positions


def save_positions(positions: list[dict]):
    tmp = PAPER_POSITIONS.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for p in positions:
            f.write(json.dumps(p) + "\n")
    tmp.replace(PAPER_POSITIONS)


def append_trade_log(record: dict):
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_processed_verdict_ids() -> set[str]:
    """Verdict IDs already acted on — prevents double-processing."""
    seen = set()
    for pos in load_positions():
        vid = pos.get("verdict_id")
        if vid:
            seen.add(vid)
    # Also check trade log for exits
    if TRADE_LOG.exists():
        for line in open(TRADE_LOG, "r", errors="replace"):
            try:
                r = json.loads(line.strip())
                vid = r.get("exit_verdict_id")
                if vid:
                    seen.add(vid)
            except Exception:
                pass
    return seen


def open_position(verdict: dict) -> dict:
    """
    Create a paper position buying NO tokens.
    YES price at entry = verdict['entry_price']
    NO cost = 1 - YES price (binary market)
    Shares = NOTIONAL / no_cost
    """
    yes_px  = verdict["entry_price"]
    no_cost = round(1.0 - yes_px, 4)
    shares  = round(NOTIONAL_PER_TRADE / no_cost, 4) if no_cost > 0 else 0

    pos = {
        "position_id":      f"pos_{verdict['signal_id']}",
        "verdict_id":       verdict["verdict_id"],
        "signal_id":        verdict["signal_id"],
        "condition_id":     verdict["condition_id"],
        "question":         verdict["question"],
        "category":         verdict["category"],
        "token_id_yes":     verdict["token_id_yes"],
        "token_id_no":      verdict["token_id_no"],
        "strategy_type":    "overreaction",
        "side":             "NO",                   # always buying NO
        "entry_yes_price":  yes_px,
        "entry_no_cost":    no_cost,
        "shares":           shares,
        "notional":         NOTIONAL_PER_TRADE,
        "status":           "open",
        "opened":           True,
        "open_ts":          time.time(),
        "open_iso":         datetime.now(timezone.utc).isoformat(),
        "close_ts":         None,
        "close_iso":        None,
        "exit_yes_price":   None,
        "exit_no_value":    None,
        "pnl_usd":          None,
        "roi_pct":          None,
        "correct":          None,
        "exit_reason":      None,
        "exit_verdict_id":  None,
        "mode":             MODE,
        "base_rate":        verdict.get("base_rate"),
        "volume_24h":       verdict.get("volume_24h"),
        "liquidity":        verdict.get("liquidity"),
        "news_at_signal":   verdict.get("news_at_signal", []),
    }
    return pos


def close_position(pos: dict, exit_verdict: dict) -> dict:
    """
    Close a paper position. Update P&L.
    We hold NO tokens: value at exit = shares * (1 - current_yes_price)

    Stale exit: if exit_yes_price == entry_yes_price exactly, the market
    likely had no trades during the hold — last-trade-price returned a stale
    value. These are flagged as data_quality="stale_exit" and excluded from
    strategy analytics.
    """
    exit_yes_px  = exit_verdict["exit_price"]
    exit_no_val  = round(1.0 - exit_yes_px, 4)
    position_val = round(pos["shares"] * exit_no_val, 4)
    pnl          = round(position_val - NOTIONAL_PER_TRADE, 4)
    roi_pct      = round(pnl / NOTIONAL_PER_TRADE * 100, 2)

    # Correct = YES price fell (NO tokens appreciated)
    correct = exit_no_val > pos["entry_no_cost"]

    # Flag stale exits: exit price identical to entry price means no trades
    # occurred during the hold period — the price feed returned a stale quote.
    stale = abs(exit_yes_px - pos["entry_yes_price"]) < 0.0001
    data_quality = "stale_exit" if stale else "ok"

    pos = dict(pos)
    pos.update({
        "status":          "closed",
        "close_ts":        time.time(),
        "close_iso":       datetime.now(timezone.utc).isoformat(),
        "exit_yes_price":  exit_yes_px,
        "exit_no_value":   exit_no_val,
        "pnl_usd":         pnl,
        "roi_pct":         roi_pct,
        "correct":         correct,
        "exit_reason":     exit_verdict.get("exit_reason", ""),
        "exit_verdict_id": exit_verdict["verdict_id"],
        "data_quality":    data_quality,
    })
    if stale:
        log(f"STALE EXIT {pos['position_id']}  entry=exit={exit_yes_px:.4f}  market had no trades")
    return pos


def process_enter_verdicts(verdicts: list[dict], processed: set[str]):
    """Open positions for ENTER verdicts not yet acted on."""
    positions = load_positions()
    pos_by_signal = {p["signal_id"]: p for p in positions}
    changed = False

    for v in verdicts:
        if v.get("action") != "ENTER":
            continue
        vid = v["verdict_id"]
        if vid in processed:
            continue
        sid = v["signal_id"]
        if sid in pos_by_signal:
            continue   # already opened

        pos = open_position(v)
        positions.append(pos)
        processed.add(vid)
        changed = True
        log(f"OPEN  {pos['position_id']}  NO@{pos['entry_no_cost']:.3f}  "
            f"shares={pos['shares']:.2f}  {pos['question'][:50]}")
        append_trade_log({"event": "open", "opened": True, **pos})

    if changed:
        save_positions(positions)


def process_exit_verdicts(verdicts: list[dict], processed: set[str]):
    """Close positions for EXIT verdicts not yet acted on."""
    positions    = load_positions()
    pos_by_posid = {p["position_id"]: p for p in positions}
    changed      = False

    for v in verdicts:
        if v.get("action") != "EXIT":
            continue
        vid = v["verdict_id"]
        if vid in processed:
            continue

        pos_id = v.get("position_id")
        pos    = pos_by_posid.get(pos_id)
        if pos is None or pos.get("status") != "open":
            continue

        closed_pos = close_position(pos, v)
        pos_by_posid[pos_id] = closed_pos
        processed.add(vid)
        changed = True
        log(f"CLOSE {pos_id}  pnl={closed_pos['pnl_usd']:.2f}  "
            f"roi={closed_pos['roi_pct']:.1f}%  reason={closed_pos['exit_reason']}")
        append_trade_log({"event": "close", **closed_pos})

    if changed:
        save_positions(list(pos_by_posid.values()))


def main():
    ensure_dirs()
    log(f"trader started  mode={MODE}  notional=${NOTIONAL_PER_TRADE}")
    processed = load_processed_verdict_ids()

    while True:
        try:
            verdicts = load_verdicts()
            process_enter_verdicts(verdicts, processed)
            process_exit_verdicts(verdicts, processed)
        except Exception as e:
            log(f"ERROR: {e}")
        time.sleep(5)


if __name__ == "__main__":
    main()
