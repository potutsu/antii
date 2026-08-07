"""
telegram_bot.py — User-initiated status bot for antii
Responds to commands, never spams.

Commands:
    /status   — worker status + uptime
    /stats    — signals, open, closed, win rate, PnL
    /log <name> — last 10 lines of a worker log
    /ping     — heartbeat check

Setup:
    1. Message @BotFather on Telegram → /newbot → get token
    2. Get your chat ID: message @userinfobot
    3. export ANTII_BOT_TOKEN=your_token
    4. export ANTII_CHAT_ID=your_chat_id
    5. Add to antii_config.py SCRIPTS as a background worker
"""

import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from antii_config import (
    ALERT_BOT_TOKEN, ALERT_CHAT_ID, SCRIPTS, MODE
)
from paths import SIGNAL_JSONL, PAPER_POSITIONS, POSTMORTEM_JSONL

import requests

BASE_URL = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}"
POLL_SEC = 2  # long-poll timeout
LOG_DIR  = Path(__file__).resolve().parent / "logs"

# ── Helpers ───────────────────────────────────────────────────────
def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def send(chat_id, text):
    try:
        requests.post(f"{BASE_URL}/sendMessage", json={
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }, timeout=10)
    except Exception as e:
        print(f"send error: {e}", flush=True)

def get_updates(offset=None):
    try:
        r = requests.get(f"{BASE_URL}/getUpdates", params={
            "timeout":         30,
            "allowed_updates": ["message"],
            "offset":          offset,
        }, timeout=35)
        if r.ok:
            return r.json().get("result", [])
    except Exception:
        pass
    return []

# ── Data readers ──────────────────────────────────────────────────
def read_stats():
    s = {"signals": 0, "open": 0, "closed": 0,
         "correct": 0, "pnl": 0.0, "postmortems": 0}
    try:
        if SIGNAL_JSONL.exists():
            s["signals"] = sum(1 for _ in open(SIGNAL_JSONL, errors="replace"))
    except Exception:
        pass
    try:
        if PAPER_POSITIONS.exists():
            for line in open(PAPER_POSITIONS, errors="replace"):
                try:
                    p = json.loads(line.strip())
                    status = p.get("status", "")
                    if status == "open":
                        s["open"] += 1
                    elif status == "closed":
                        s["closed"] += 1
                        if p.get("correct"):
                            s["correct"] += 1
                        s["pnl"] += float(p.get("pnl_usd") or 0)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        if POSTMORTEM_JSONL.exists():
            s["postmortems"] = sum(
                1 for _ in open(POSTMORTEM_JSONL, errors="replace"))
    except Exception:
        pass
    return s

def read_log_tail(name, n=10):
    log_path = LOG_DIR / f"{name}.log"
    if not log_path.exists():
        return f"No log found for `{name}`"
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        tail  = lines[-n:] if len(lines) >= n else lines
        return "\n".join(tail) if tail else "(empty)"
    except Exception as e:
        return f"Error reading log: {e}"

STATUS_JSON = Path(__file__).resolve().parent / "data" / "status.json"

STATUS_ICON = {
    "RUNNING":   "🟢",
    "STOPPED":   "⚪",
    "CRASHED":   "🔴",
    "FAILED":    "🔴",
    "HALTED":    "🟡",
    "ERROR":     "🔴",
    "NOT FOUND": "🔴",
    "STOPPING":  "🟡",
    "UNKNOWN":   "❓",
    "STALE":     "🟠",
}

def read_status_json() -> dict:
    """Read manager's live status.json. Falls back to file-based inference."""
    try:
        if STATUS_JSON.exists():
            age = time.time() - STATUS_JSON.stat().st_mtime
            if age < 60:  # fresh enough
                return json.loads(STATUS_JSON.read_text())
    except Exception:
        pass
    return {}

def worker_status_text():
    data    = read_status_json()
    workers = data.get("workers", {})
    mode    = data.get("mode", MODE).upper()
    uptime  = data.get("manager_uptime", "?")
    updated = data.get("updated_iso", ts())
    stale   = not workers

    lines = [
        f"*ANTII [{mode}] Status*",
        f"_Manager uptime: {uptime}_",
        f"_Last update: {updated}_",
        "",
    ]

    if stale:
        lines.append("⚠️ manager status.json not found or stale")
        lines.append("Manager may be down or just started.")
        return "\n".join(lines)

    for name, info in workers.items():
        status   = info.get("status", "UNKNOWN")
        pid      = info.get("pid") or "—"
        up       = info.get("uptime", "—")
        restarts = info.get("restarts", 0)
        last_log = info.get("last_log", "")
        ic       = STATUS_ICON.get(status, "❓")
        rst_txt  = f" ⟳{restarts}" if restarts > 0 else ""
        lines.append(f"{ic} *{name}*{rst_txt}")
        lines.append(f"   `{status}` · pid:{pid} · up:{up}")
        if last_log:
            # trim to fit nicely
            lines.append(f"   _{last_log[:80]}_")
        lines.append("")

    return "\n".join(lines).strip()

# ── Command handlers ──────────────────────────────────────────────
def handle_ping(chat_id):
    send(chat_id, f"✅ antii alive — {ts()}")

def handle_status(chat_id):
    send(chat_id, worker_status_text())

def handle_stats(chat_id):
    s  = read_stats()
    wr = f"{s['correct']/s['closed']*100:.1f}%" if s["closed"] else "—"
    pnl_icon = "📈" if s["pnl"] >= 0 else "📉"
    text = (
        f"*ANTII Stats [{MODE.upper()}]*\n"
        f"_{ts()}_\n\n"
        f"📡 Signals detected: `{s['signals']}`\n"
        f"🔵 Open positions:   `{s['open']}`\n"
        f"✅ Closed positions: `{s['closed']}`\n"
        f"🎯 Win rate:         `{wr}`\n"
        f"{pnl_icon} Paper PnL:        `${s['pnl']:+.2f}`\n"
        f"📋 Postmortems:      `{s['postmortems']}`"
    )
    send(chat_id, text)

def handle_log(chat_id, args):
    if not args:
        names = [s["name"] for s in SCRIPTS]
        send(chat_id,
             f"Usage: `/log <name>`\nAvailable: {', '.join(f'`{n}`' for n in names)}")
        return
    name = args[0].lower()
    valid = [s["name"] for s in SCRIPTS]
    if name not in valid:
        send(chat_id,
             f"Unknown worker `{name}`.\nAvailable: {', '.join(f'`{n}`' for n in valid)}")
        return
    tail = read_log_tail(name, n=10)
    # Telegram max message length
    if len(tail) > 3800:
        tail = "..." + tail[-3800:]
    send(chat_id, f"*{name} (last 10 lines)*\n```\n{tail}\n```")

def handle_help(chat_id):
    send(chat_id,
         "*ANTII Bot Commands*\n\n"
         "/ping — heartbeat check\n"
         "/status — worker status\n"
         "/stats — signals, PnL, win rate\n"
         "/log <name> — last 10 log lines\n"
         "/help — this message")

HANDLERS = {
    "/ping":   lambda cid, _: handle_ping(cid),
    "/status": lambda cid, _: handle_status(cid),
    "/stats":  lambda cid, _: handle_stats(cid),
    "/log":    lambda cid, a: handle_log(cid, a),
    "/help":   lambda cid, _: handle_help(cid),
    "/start":  lambda cid, _: handle_help(cid),
}

# ── Main loop ─────────────────────────────────────────────────────
def main():
    if not ALERT_BOT_TOKEN:
        print("ANTII_BOT_TOKEN not set — exiting", flush=True)
        return
    if not ALERT_CHAT_ID:
        print("ANTII_CHAT_ID not set — exiting", flush=True)
        return

    print(f"[{ts()}] telegram_bot started", flush=True)
    send(ALERT_CHAT_ID, f"🤖 ANTII bot online — {ts()}\nType /help for commands")

    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                chat   = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                text    = msg.get("text", "").strip()

                # Only respond to your own chat
                if chat_id != str(ALERT_CHAT_ID):
                    continue
                if not text.startswith("/"):
                    continue

                parts   = text.split()
                cmd     = parts[0].lower().split("@")[0]  # strip @botname
                args    = parts[1:]

                handler = HANDLERS.get(cmd)
                if handler:
                    try:
                        handler(chat_id, args)
                    except Exception as e:
                        send(chat_id, f"⚠️ Error: {e}")
                else:
                    send(chat_id, f"Unknown command: `{cmd}`\nType /help")

        except KeyboardInterrupt:
            print("stopped", flush=True)
            break
        except Exception as e:
            print(f"error: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
