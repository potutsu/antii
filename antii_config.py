"""
antii_config.py — Overreaction Fading Strategy Config
Standalone antii project — no proba dependency
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# ── Mode ───────────────────────────────────────────────────────────
MODE = os.environ.get("ANTII_MODE", "paper")   # paper | live

# ── Strategy Parameters ────────────────────────────────────────────
ENTRY_MIN_MOVE_60MIN    = float(os.environ.get("ENTRY_MIN_MOVE_60MIN",    7.0))   # % YES price rise in last 60 min
ENTRY_MIN_REVERT_PCT    = float(os.environ.get("ENTRY_MIN_REVERT_PCT",    3.0))   # % drop from signal price to confirm entry
ENTRY_MAX_WAIT_MIN      = int(os.environ.get("ENTRY_MAX_WAIT_MIN",        120))   # timeout entry if no 3% revert
EXIT_REVERT_PCT         = float(os.environ.get("EXIT_REVERT_PCT",         10.0))  # % drop from entry price → take profit
EXIT_MAX_HOLD_HOURS     = float(os.environ.get("EXIT_MAX_HOLD_HOURS",     48.0))  # force close after N hours
EXIT_STOP_LOSS_PCT      = float(os.environ.get("EXIT_STOP_LOSS_PCT",      15.0))  # % rise from entry → stop loss
EXIT_RESOLUTION_PCT     = float(os.environ.get("EXIT_RESOLUTION_PCT",      2.0))  # YES <= this % → treat as resolved NO

# ── Market Filters ─────────────────────────────────────────────────
MIN_VOLUME_24H          = float(os.environ.get("MIN_VOLUME_24H",          5000))  # USD — from gamma events volume24hr
MIN_LIQUIDITY           = float(os.environ.get("MIN_LIQUIDITY",           2000))  # USD — from gamma events liquidity
YES_PRICE_MIN           = float(os.environ.get("YES_PRICE_MIN",           0.25))
YES_PRICE_MAX           = float(os.environ.get("YES_PRICE_MAX",           0.45))
ALLOWED_CATEGORIES      = {"politics", "geopolitics", "economics", "crypto", "tech"}
SPORTS_TAGS             = {"sports", "nfl", "nba", "mlb", "nhl", "soccer", "football",
                           "basketball", "baseball", "hockey", "tennis", "golf", "mma",
                           "ufc", "boxing", "racing", "esports", "olympics"}

# ── Position Sizing ────────────────────────────────────────────────
NOTIONAL_PER_TRADE      = float(os.environ.get("NOTIONAL_PER_TRADE",      40.0))  # USD paper notional
MAX_OPEN_POSITIONS      = int(os.environ.get("MAX_OPEN_POSITIONS",         50))
MAX_WATCHED_SIGNALS     = int(os.environ.get("MAX_WATCHED_SIGNALS",        50))

# ── Polling Cadence ────────────────────────────────────────────────
DISCOVERY_INTERVAL_SEC  = int(os.environ.get("DISCOVERY_INTERVAL_SEC",    300))   # 5 min
MONITOR_ENTRY_POLL_SEC  = int(os.environ.get("MONITOR_ENTRY_POLL_SEC",    120))   # 2 min
MONITOR_POS_POLL_SEC    = int(os.environ.get("MONITOR_POS_POLL_SEC",      300))   # 5 min
SHADOW_POLL_SEC         = int(os.environ.get("SHADOW_POLL_SEC",           900))   # 15 min
SHADOW_POST_CLOSE_HOURS = float(os.environ.get("SHADOW_POST_CLOSE_HOURS", 72.0))  # hours after close

# ── Finnhub ────────────────────────────────────────────────────────
NEWSDATA_API_KEY     = os.environ.get("NEWSDATA_API_KEY", "")
WALLET_COMBOS_CSV    = os.environ.get("WALLET_COMBOS_CSV",
                        str(BASE_DIR / "data" / "edge_combos.csv"))
WALLET_ACTIVITY_DAYS = int(os.environ.get("WALLET_ACTIVITY_DAYS", 30))
FINNHUB_API_KEY         = os.environ.get("FINNHUB_API_KEY", "")

# ── Telegram Alerts ────────────────────────────────────────────────
ALERT_BOT_TOKEN         = os.environ.get("ANTII_BOT_TOKEN", "")
ALERT_CHAT_ID           = os.environ.get("ANTII_CHAT_ID", "")

# ── Manager Behaviour ──────────────────────────────────────────────
STARTUP_DELAY_SEC       = 1.5
RESTART_COOLDOWN_SEC    = 10
MAX_RESTARTS            = 20   # raised from 5 — workers must stay alive
RESTART_RESET_SEC       = 600
ON_CRASH                = "restart+alert"   # restart+alert | restart_only | alert_only | none
REFRESH_SEC             = 1
LOG_LINES               = 80
LOG_VIEW_INIT           = "discovery"

# ── Scripts registry (consumed by manager.py) ──────────────────────
_L = str(BASE_DIR / "logs")

SCRIPTS = [
    # ── Pipeline workers (start manually) ─────────────────────────
    {
        "name":  "wallet_watcher",
        "key":   "1",
        "group": "pipeline",
        "desc":  "Watches wallet × category combos, emits signals",
        "path":  str(BASE_DIR / "wallet_watcher.py"),
        "log":   f"{_L}/wallet_watcher.log",
    },
    {
        "name":  "monitor_entry",
        "key":   "2",
        "group": "pipeline",
        "desc":  "Watches signals, issues ENTER/DISCARD verdicts",
        "path":  str(BASE_DIR / "monitor_entry.py"),
        "log":   f"{_L}/monitor_entry.log",
    },
    {
        "name":  "monitor_position",
        "key":   "3",
        "group": "pipeline",
        "desc":  "Watches open positions, issues EXIT verdicts",
        "path":  str(BASE_DIR / "monitor_position.py"),
        "log":   f"{_L}/monitor_position.log",
    },
    {
        "name":  "trader",
        "key":   "4",
        "group": "pipeline",
        "desc":  "Opens and closes paper trades on verdict",
        "path":  str(BASE_DIR / "trader.py"),
        "log":   f"{_L}/trader.log",
    },
    {
        "name":  "postmortem",
        "key":   "5",
        "group": "pipeline",
        "desc":  "Computes MFE/MAE/ROI on closed positions",
        "path":  str(BASE_DIR / "postmortem.py"),
        "log":   f"{_L}/postmortem.log",
    },
    # ── Background workers (always-on) ────────────────────────────
    {
        "name":  "shadow",
        "key":   "6",
        "group": "background",
        "desc":  "Price logger — every signal regardless of trade",
        "path":  str(BASE_DIR / "shadow.py"),
        "log":   f"{_L}/shadow.log",
    },
    {
        "name":  "telegram_bot",
        "key":   "7",
        "group": "background",
        "desc":  "User-initiated status bot",
        "path":  str(BASE_DIR / "telegram_bot.py"),
        "log":   f"{_L}/telegram_bot.log",
    },
]
