"""
postmortem.py — Analytics on closed positions.

Runs on every position close (triggered by watching trade.jsonl),
and as a manual batch command: python postmortem.py --batch

Output fields (per spec):
  mfe_pct               max favourable excursion (best NO value vs cost)
  mae_pct               max adverse excursion (worst NO value vs cost)
  age_at_ath_sec        seconds from open to best shadow price point
  exit_vs_peak_pct      how far exit was from the peak (% of peak gain left on table)
  signal_to_entry_sec   seconds from signal_ts to open_ts
  revert_confirmed_at_sec  seconds from signal_ts to reversion >= ENTRY_MIN_REVERT_PCT
  exit_reason           from position record
  category              market category
  base_rate             from signal record
  news_at_signal        from signal record
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import NOTIONAL_PER_TRADE, ENTRY_MIN_REVERT_PCT
from paths import ensure_dirs, PAPER_POSITIONS, SHADOW_LOG, POSTMORTEM_JSONL, TRADE_LOG


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str):
    print(f"[{ts()}] [postmortem] {msg}", flush=True)


def load_closed_positions() -> list[dict]:
    if not PAPER_POSITIONS.exists():
        return []
    result = []
    for line in open(PAPER_POSITIONS, "r", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
            if p.get("status") == "closed":
                result.append(p)
        except Exception:
            pass
    return result


def load_shadow_for_signal(signal_id: str) -> list[dict]:
    """Return all shadow rows for a signal, sorted by logged_at_ts."""
    if not SHADOW_LOG.exists():
        return []
    rows = []
    for line in open(SHADOW_LOG, "r", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if r.get("signal_id") == signal_id:
                rows.append(r)
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("logged_at_ts", 0))
    return rows


def load_already_computed() -> set[str]:
    """Position IDs already in postmortem.jsonl."""
    seen = set()
    if not POSTMORTEM_JSONL.exists():
        return seen
    for line in open(POSTMORTEM_JSONL, "r", errors="replace"):
        try:
            r = json.loads(line.strip())
            pid = r.get("position_id")
            if pid:
                seen.add(pid)
        except Exception:
            pass
    return seen


def compute_postmortem(pos: dict) -> dict | None:
    """
    Compute all postmortem fields for a closed position.
    Shadow data provides the MFE/MAE/ATH price series.
    """
    sid       = pos["signal_id"]
    entry_yes = pos["entry_yes_price"]
    entry_no  = pos["entry_no_cost"]
    exit_yes  = pos.get("exit_yes_price", entry_yes)
    exit_no   = 1.0 - exit_yes
    open_ts   = pos.get("open_ts", 0)
    signal_ts_approx = open_ts  # fallback if signal not found

    # Load shadow rows
    shadow_rows = load_shadow_for_signal(sid)

    # ── MFE / MAE from shadow ──────────────────────────────────────
    # We hold NO: favourable = YES falls, adverse = YES rises
    # MFE = max (entry_no_cost → peak_no_value) / entry_no_cost
    # MAE = max (entry_no_cost → trough_no_value) / entry_no_cost
    best_no_value  = entry_no
    worst_no_value = entry_no
    best_no_ts     = open_ts

    post_open_rows = [r for r in shadow_rows
                      if r.get("phase") in ("open", "post_close")
                      and r.get("yes_price") is not None]

    for r in post_open_rows:
        yes_px  = r["yes_price"]
        no_val  = 1.0 - yes_px
        if no_val > best_no_value:
            best_no_value = no_val
            best_no_ts    = r.get("logged_at_ts", open_ts)
        if no_val < worst_no_value:
            worst_no_value = no_val

    mfe_pct = round((best_no_value - entry_no) / entry_no * 100, 2) if entry_no > 0 else 0.0
    mae_pct = round((entry_no - worst_no_value) / entry_no * 100, 2) if entry_no > 0 else 0.0
    mae_pct = max(0.0, mae_pct)  # negative MAE = no adverse excursion

    age_at_ath_sec = round(best_no_ts - open_ts) if open_ts else None

    # ── exit_vs_peak_pct ──────────────────────────────────────────
    # How much of the peak gain did we leave on the table?
    # 0% = perfect exit at peak, 100% = exited at entry (zero gain)
    peak_gain = best_no_value - entry_no
    exit_gain = exit_no - entry_no
    if peak_gain > 0:
        exit_vs_peak_pct = round((1 - exit_gain / peak_gain) * 100, 2)
    else:
        exit_vs_peak_pct = None

    # ── signal_to_entry_sec ────────────────────────────────────────
    # Approximate from pre_open shadow rows
    pre_rows = [r for r in shadow_rows if r.get("phase") == "pre_open"]
    if pre_rows:
        signal_ts_approx = pre_rows[0].get("logged_at_ts", open_ts)
    signal_to_entry_sec = round(open_ts - signal_ts_approx) if open_ts else None

    # ── revert_confirmed_at_sec ────────────────────────────────────
    # First shadow row where yes_price <= signal_yes_price * (1 - ENTRY_MIN_REVERT_PCT/100)
    signal_yes = None
    if shadow_rows:
        signal_yes = shadow_rows[0].get("signal_yes_price")

    revert_confirmed_at_sec = None
    if signal_yes:
        threshold = signal_yes * (1 - ENTRY_MIN_REVERT_PCT / 100)
        for r in shadow_rows:
            yp = r.get("yes_price")
            if yp is not None and yp <= threshold:
                revert_confirmed_at_sec = round(r.get("logged_at_ts", open_ts) - signal_ts_approx)
                break

    pm = {
        "position_id":              pos["position_id"],
        "signal_id":                sid,
        "condition_id":             pos["condition_id"],
        "question":                 pos.get("question", "")[:120],
        "category":                 pos.get("category", ""),
        "exit_reason":              pos.get("exit_reason", ""),
        "correct":                  pos.get("correct"),
        "pnl_usd":                  pos.get("pnl_usd"),
        "roi_pct":                  pos.get("roi_pct"),
        "notional":                 NOTIONAL_PER_TRADE,
        "entry_yes_price":          entry_yes,
        "entry_no_cost":            entry_no,
        "exit_yes_price":           exit_yes,
        "mfe_pct":                  mfe_pct,
        "mae_pct":                  mae_pct,
        "age_at_ath_sec":           age_at_ath_sec,
        "exit_vs_peak_pct":         exit_vs_peak_pct,
        "signal_to_entry_sec":      signal_to_entry_sec,
        "revert_confirmed_at_sec":  revert_confirmed_at_sec,
        "base_rate":                pos.get("base_rate"),
        "news_at_signal":           pos.get("news_at_signal", []),
        "news_annotation":          "",
        "shadow_rows_count":        len(shadow_rows),
        "computed_at_ts":           time.time(),
        "computed_at_iso":          datetime.now(timezone.utc).isoformat(),
    }
    return pm


def run_batch(force: bool = False):
    closed    = load_closed_positions()
    computed  = load_already_computed() if not force else set()
    new_count = 0

    stale_count = 0

    for pos in closed:
        pid = pos["position_id"]
        if pid in computed:
            continue

        # Skip stale exits — exit price == entry price means no trades occurred
        # during the hold. These produce $0.00 P&L and corrupt MFE/MAE averages.
        if pos.get("data_quality") == "stale_exit":
            stale_count += 1
            log(f"SKIP stale_exit {pid}")
            computed.add(pid)   # mark so we don't revisit on every run
            continue

        try:
            pm = compute_postmortem(pos)
            if pm:
                with open(POSTMORTEM_JSONL, "a") as f:
                    f.write(json.dumps(pm) + "\n")
                log(f"postmortem {pid}  mfe={pm['mfe_pct']:+.1f}%  mae={pm['mae_pct']:.1f}%  "
                    f"roi={pm['roi_pct']:.1f}%  reason={pm['exit_reason']}")
                computed.add(pid)
                new_count += 1
        except Exception as e:
            log(f"ERROR {pid}: {e}")

    log(f"batch complete — {new_count} new postmortems  {stale_count} stale skipped  "
        f"({len(closed)} closed total)")
    return new_count


def main():
    ensure_dirs()

    parser = argparse.ArgumentParser(description="antii postmortem worker")
    parser.add_argument("--batch", action="store_true", help="Run batch and exit")
    parser.add_argument("--force", action="store_true", help="Recompute all (with --batch)")
    args = parser.parse_args()

    if args.batch:
        log("batch mode")
        run_batch(force=args.force)
        return

    # Daemon mode: watches trade log for new close events
    log("postmortem daemon started — watching for position closes")
    computed = load_already_computed()

    prev_trade_size = 0

    while True:
        try:
            trade_size = TRADE_LOG.stat().st_size if TRADE_LOG.exists() else 0
            if trade_size != prev_trade_size:
                prev_trade_size = trade_size
                closed = load_closed_positions()
                # Dedup by position_id — paper_positions.jsonl can have
                # duplicate lines for same position from append races
                seen_this_batch = {}
                for pos in closed:
                    pid = pos["position_id"]
                    if pid not in seen_this_batch:
                        seen_this_batch[pid] = pos
                closed = list(seen_this_batch.values())

                for pos in closed:
                    pid = pos["position_id"]
                    if pid in computed:
                        continue
                    if pos.get("data_quality") == "stale_exit":
                        log(f"SKIP stale_exit {pid}")
                        computed.add(pid)
                        continue
                    pm = compute_postmortem(pos)
                    if pm:
                        with open(POSTMORTEM_JSONL, "a") as f:
                            f.write(json.dumps(pm) + "\n")
                        log(f"postmortem {pid}  mfe={pm['mfe_pct']:+.1f}%  "
                            f"mae={pm['mae_pct']:.1f}%  roi={pm['roi_pct']:.1f}%")
                        computed.add(pid)
        except Exception as e:
            log(f"ERROR: {e}")
        time.sleep(15)


if __name__ == "__main__":
    main()
