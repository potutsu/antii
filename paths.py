"""
paths.py — Canonical file paths for antii standalone
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

# ── Data files ─────────────────────────────────────────────────────
SIGNAL_JSONL        = DATA_DIR / "signal.jsonl"
PAPER_POSITIONS     = DATA_DIR / "paper_positions.jsonl"
TRADE_LOG           = DATA_DIR / "trade.jsonl"
SHADOW_LOG          = DATA_DIR / "shadow.jsonl"
POSTMORTEM_JSONL    = DATA_DIR / "postmortem.jsonl"
VERDICTS_JSONL      = DATA_DIR / "verdicts.jsonl"

# ── Log files (keyed by worker name for manager) ───────────────────
LOGS = {
    "discovery":        LOGS_DIR / "discovery.log",
    "monitor_entry":    LOGS_DIR / "monitor_entry.log",
    "monitor_position": LOGS_DIR / "monitor_position.log",
    "trader":           LOGS_DIR / "trader.log",
    "postmortem":       LOGS_DIR / "postmortem.log",
    "shadow":           LOGS_DIR / "shadow.log",
    "signal":           SIGNAL_JSONL,       # stats reader convenience alias
    "trade":            TRADE_LOG,
    "checkpoint":       POSTMORTEM_JSONL,
}


def get_paper_positions_path() -> Path:
    return PAPER_POSITIONS


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # Touch all data files so tails don't fail on first start
    for f in (SIGNAL_JSONL, PAPER_POSITIONS, TRADE_LOG, SHADOW_LOG,
              POSTMORTEM_JSONL, VERDICTS_JSONL):
        f.touch(exist_ok=True)
