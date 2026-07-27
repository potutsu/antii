"""
manager.py — antii TUI process manager (standalone)
Overreaction Fading on Polymarket | NoLaptopTrades

Run via:  python manager.py
          python -m antii.manager

TUI hotkeys:
  [1-6]      switch log view to that worker
  [r]        restart currently viewed worker (pipeline only)
  [h]        halt / resume currently viewed worker (pipeline only)
  [Ctrl+A]   start all pipeline workers
  [Ctrl+P]   pause (halt) all pipeline workers
  [Ctrl+K]   kill all + quit (confirm required)
  [q]        quit manager + stop all workers
"""

import curses
import json
import os
import signal
import subprocess
import sys
import time
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from antii_config import (
    SCRIPTS, STARTUP_DELAY_SEC, RESTART_COOLDOWN_SEC,
    MAX_RESTARTS, RESTART_RESET_SEC, ON_CRASH,
    REFRESH_SEC, LOG_LINES, LOG_VIEW_INIT,
    ALERT_BOT_TOKEN, ALERT_CHAT_ID, MODE,
)
from paths import ensure_dirs, SIGNAL_JSONL, PAPER_POSITIONS, TRADE_LOG, POSTMORTEM_JSONL, LOGS

import requests

PYTHON  = sys.executable
VERSION = "1.0.0"

GROUP_PIPELINE   = "pipeline"
GROUP_BACKGROUND = "background"

# ── Globals ────────────────────────────────────────────────────────
state      = {}
state_lock = threading.Lock()

log_view_index = 0

manager_log      = deque(maxlen=50)
manager_log_lock = threading.Lock()

running = True

import re as _re
_TS_PAT = _re.compile(r"\[20\d\d-\d\d-\d\d (\d\d:\d\d:\d\d) UTC\]")

def _strip_ts(line: str) -> str:
    return _TS_PAT.sub(r"[\1]", line)

def now_ts() -> float:
    return time.time()

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def format_uptime(start_ts: float) -> str:
    if start_ts is None:
        return "—"
    sec = int(now_ts() - start_ts)
    if sec < 60:   return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m {sec % 60}s"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h}h {m}m"

_curses_active = False

def mlog(msg: str):
    with manager_log_lock:
        manager_log.append(f"{now_iso()} {msg}")
    if not _curses_active:
        print(f"[manager] {msg}", flush=True)

def send_alert(msg: str):
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage",
            json={"chat_id": ALERT_CHAT_ID, "text": f"🚨 ANTII: {msg}"},
            timeout=5,
        )
    except Exception:
        pass


# ── Stats reader ───────────────────────────────────────────────────

_stats_cache = {
    "signals": 0, "open": 0, "closed": 0,
    "correct": 0, "win_rate": 0.0, "postmortems": 0,
}
_stats_lock = threading.Lock()


def _read_stats() -> dict:
    stats = {"signals": 0, "open": 0, "closed": 0,
             "correct": 0, "win_rate": 0.0, "postmortems": 0}

    if SIGNAL_JSONL.exists():
        try:
            stats["signals"] = sum(1 for _ in open(SIGNAL_JSONL, errors="replace"))
        except Exception:
            pass

    if PAPER_POSITIONS.exists():
        try:
            for line in open(PAPER_POSITIONS, errors="replace"):
                try:
                    p = json.loads(line.strip())
                    if p.get("strategy_type") != "overreaction":
                        continue
                    if p.get("status") == "open":
                        stats["open"] += 1
                    elif p.get("status") == "closed":
                        stats["closed"] += 1
                        if p.get("correct"):
                            stats["correct"] += 1
                except Exception:
                    pass
        except Exception:
            pass

    if stats["closed"] > 0:
        stats["win_rate"] = round(stats["correct"] / stats["closed"] * 100, 1)

    if POSTMORTEM_JSONL.exists():
        try:
            stats["postmortems"] = sum(1 for _ in open(POSTMORTEM_JSONL, errors="replace"))
        except Exception:
            pass

    return stats


def stats_loop():
    global _stats_cache
    while running:
        try:
            s = _read_stats()
            with _stats_lock:
                _stats_cache.update(s)
        except Exception:
            pass
        time.sleep(15)


# ── Log tail threads ───────────────────────────────────────────────

def tail_log(name: str, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open(log_path, "a").close()
    buf = deque(maxlen=LOG_LINES * 3)
    pos = 0
    while running:
        try:
            size = os.path.getsize(log_path)
            if size < pos:
                pos = 0
            with open(log_path, "r", errors="replace") as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped:
                        buf.append(stripped)
                pos = f.tell()
            with state_lock:
                if name in state:
                    state[name]["log_buf"] = list(buf)[-LOG_LINES:]
        except Exception:
            pass
        time.sleep(0.5)


# ── Process management ─────────────────────────────────────────────

def init_state():
    for s in SCRIPTS:
        state[s["name"]] = {
            "proc":          None,
            "pid":           None,
            "status":        "STOPPED",
            "start_ts":      None,
            "restarts":      0,
            "last_crash_ts": None,
            "log_buf":       [],
            "path":          s["path"],
            "log":           s["log"],
            "key":           s["key"],
            "group":         s.get("group", GROUP_PIPELINE),
            "desc":          s.get("desc", ""),
        }


def start_script(name: str):
    with state_lock:
        s        = state[name]
        log_path = s["log"]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    script_path = state[name]["path"]
    if not os.path.exists(script_path):
        mlog(f"ERROR: {name} not found at {script_path}")
        with state_lock:
            state[name]["status"] = "NOT FOUND"
        return
    try:
        log_file = open(log_path, "a")
        proc     = subprocess.Popen(
            [PYTHON, "-u", script_path],
            stdout=log_file,
            stderr=log_file,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        with state_lock:
            state[name]["proc"]     = proc
            state[name]["pid"]      = proc.pid
            state[name]["status"]   = "RUNNING"
            state[name]["start_ts"] = now_ts()
        mlog(f"started {name} (pid={proc.pid})")
    except Exception as e:
        mlog(f"ERROR starting {name}: {e}")
        with state_lock:
            state[name]["status"] = "ERROR"


def stop_script(name: str):
    with state_lock:
        proc = state[name].get("proc")
        state[name]["status"] = "STOPPING"
    if proc and proc.poll() is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
        except Exception:
            pass
    with state_lock:
        state[name]["proc"]     = None
        state[name]["pid"]      = None
        state[name]["status"]   = "STOPPED"
        state[name]["start_ts"] = None
    mlog(f"stopped {name}")


def restart_script(name: str):
    mlog(f"restarting {name}")
    stop_script(name)
    time.sleep(1)
    start_script(name)


def halt_script(name: str):
    mlog(f"halting {name}")
    stop_script(name)
    with state_lock:
        state[name]["status"] = "HALTED"


def stop_all():
    for s in reversed(SCRIPTS):
        name = s["name"]
        with state_lock:
            status = state[name].get("status")
        if status not in ("STOPPED", "HALTED", "NOT FOUND", "FAILED"):
            stop_script(name)


# ── Monitor thread ─────────────────────────────────────────────────

def monitor_loop():
    while running:
        for s in SCRIPTS:
            name  = s["name"]
            group = s.get("group", GROUP_PIPELINE)
            with state_lock:
                proc     = state[name].get("proc")
                status   = state[name].get("status")
                restarts = state[name].get("restarts", 0)
                start_ts = state[name].get("start_ts")

            if status != "RUNNING" or proc is None:
                continue
            if proc.poll() is None:
                continue

            with state_lock:
                state[name]["status"]        = "CRASHED"
                state[name]["proc"]          = None
                state[name]["pid"]           = None
                state[name]["last_crash_ts"] = now_ts()

            mlog(f"CRASH detected: {name}")

            if start_ts and (now_ts() - start_ts) > RESTART_RESET_SEC:
                with state_lock:
                    state[name]["restarts"] = 0
                restarts = 0

            if group == GROUP_BACKGROUND:
                mlog(f"background {name} crashed — restarting immediately")
                if ON_CRASH in ("restart+alert", "alert_only"):
                    send_alert(f"{name} crashed — restarting (always-on)")
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)
                continue

            if restarts >= MAX_RESTARTS:
                mlog(f"gave up restarting {name} after {MAX_RESTARTS} attempts")
                if ON_CRASH in ("restart+alert", "alert_only"):
                    send_alert(f"{name} crashed {MAX_RESTARTS}x — gave up")
                with state_lock:
                    state[name]["status"] = "FAILED"
                continue

            if ON_CRASH in ("restart+alert", "alert_only"):
                send_alert(f"{name} crashed — restarting")
            if ON_CRASH in ("restart+alert", "restart_only"):
                mlog(f"waiting {RESTART_COOLDOWN_SEC}s before restarting {name}")
                time.sleep(RESTART_COOLDOWN_SEC)
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)

        time.sleep(2)


# ── TUI ────────────────────────────────────────────────────────────

def draw(stdscr):
    global log_view_index, running, _curses_active
    _curses_active = True

    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_GREEN,  -1)
    curses.init_pair(2, curses.COLOR_RED,    -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN,   -1)
    curses.init_pair(5, curses.COLOR_WHITE,  -1)
    curses.init_pair(6, -1,                  -1)

    GREEN  = curses.color_pair(1) | curses.A_BOLD
    RED    = curses.color_pair(2) | curses.A_BOLD
    YELLOW = curses.color_pair(3) | curses.A_BOLD
    CYAN   = curses.color_pair(4) | curses.A_BOLD
    NORMAL = curses.color_pair(5)
    DIM    = curses.color_pair(6)

    script_names = [s["name"] for s in SCRIPTS]
    if LOG_VIEW_INIT in script_names:
        log_view_index = script_names.index(LOG_VIEW_INIT)

    key_to_index = {s["key"]: i for i, s in enumerate(SCRIPTS)}
    kill_confirm    = False
    kill_confirm_ts = 0.0

    def put(row, col, text, attr=None):
        h, w = stdscr.getmaxyx()
        if row < 0 or row >= h - 2 or col < 0 or col >= w:
            return
        text = str(text)[:max(0, w - col - 1)]
        if not text:
            return
        try:
            if attr is not None:
                stdscr.addstr(row, col, text, attr)
            else:
                stdscr.addstr(row, col, text)
        except curses.error:
            pass

    def put_footer(row, text, attr=None):
        h, w = stdscr.getmaxyx()
        if row < 0 or row >= h:
            return
        text = str(text)[:max(0, w - 1)]
        try:
            if attr is not None:
                stdscr.addstr(row, 0, text, attr)
            else:
                stdscr.addstr(row, 0, text)
        except curses.error:
            pass

    def status_sym(status):
        if status == "RUNNING":  return GREEN,  "●"
        if status == "HALTED":   return YELLOW, "⏸"
        if status in ("CRASHED", "FAILED", "ERROR", "NOT FOUND"):
            return RED, "✖"
        return YELLOW, "○"

    while running:
        with state_lock:
            snap = {n: dict(v) for n, v in state.items()}
        with _stats_lock:
            sc = dict(_stats_cache)

        h, w   = stdscr.getmaxyx()
        ts_str = datetime.now().strftime("%H:%M:%S")
        vname  = script_names[log_view_index % len(script_names)]
        CONTENT_MAX = h - 3

        try:
            stdscr.erase()

            if h < 10 or w < 20:
                put(0, 0, "Too small", YELLOW)
                stdscr.refresh()
                time.sleep(REFRESH_SEC)
                continue

            row = 0

            # ── Header ────────────────────────────────────────────
            put(row, 0, "═" * (w - 1), CYAN); row += 1
            title = f" ANTII [{MODE.upper()}] — Overreaction Fading — {ts_str}"
            put(row, 0, title[:w - 1], CYAN); row += 1
            put(row, 0, "═" * (w - 1), CYAN); row += 1

            # ── Workers ───────────────────────────────────────────
            bg_scripts = [x for x in SCRIPTS if x.get("group") == GROUP_BACKGROUND]
            for s in SCRIPTS:
                if row > CONTENT_MAX:
                    break
                name   = s["name"]
                key_ch = s["key"]
                group  = s.get("group", GROUP_PIPELINE)
                info   = snap.get(name, {})
                status = info.get("status", "UNKNOWN")
                uptime = format_uptime(info.get("start_ts")) if info.get("start_ts") else "—"
                scol, sym = status_sym(status)
                active = (name == vname)
                label_attr = CYAN if active else NORMAL

                if group == GROUP_BACKGROUND and s == bg_scripts[0]:
                    if row <= CONTENT_MAX:
                        put(row, 0, "┄" * (w - 1), DIM); row += 1

                if row > CONTENT_MAX:
                    break

                tag  = "bg" if group == GROUP_BACKGROUND else "  "
                line = f"[{key_ch}]{tag} {name:<16} {sym} {status:<9} {uptime}"
                put(row, 0, line[:w - 1], label_attr)
                sym_col = line.find(sym)
                if sym_col >= 0:
                    put(row, sym_col, f"{sym} {status:<9}", scol)
                row += 1

            # ── Stats bar ──────────────────────────────────────────
            if row <= CONTENT_MAX:
                put(row, 0, "─" * (w - 1), DIM); row += 1
            if row <= CONTENT_MAX:
                wr  = f"{sc['win_rate']:.0f}%" if sc["closed"] else "—"
                bar = (f" sig:{sc['signals']}  open:{sc['open']}  "
                       f"closed:{sc['closed']}  wr:{wr}  pm:{sc['postmortems']}")
                put(row, 0, bar[:w - 1], CYAN); row += 1

            # ── Log panel ─────────────────────────────────────────
            if row <= CONTENT_MAX:
                put(row, 0, "─" * (w - 1), DIM); row += 1
            if row <= CONTENT_MAX:
                put(row, 0, f" LOG [{vname}]"[:w - 1], CYAN); row += 1

            log_start = row
            max_lines = max(0, CONTENT_MAX - log_start + 1)

            with state_lock:
                log_buf = list(state.get(vname, {}).get("log_buf", []))
            if not log_buf:
                with manager_log_lock:
                    log_buf = list(manager_log)[-LOG_LINES:]

            for line in log_buf[-max_lines:]:
                if row > CONTENT_MAX:
                    break
                put(row, 0, (" " + _strip_ts(line))[:w - 1], NORMAL)
                row += 1

            # ── Kill confirm ──────────────────────────────────────
            if kill_confirm:
                elapsed = now_ts() - kill_confirm_ts
                if elapsed > 5:
                    kill_confirm = False
                else:
                    msg = f" Ctrl+K again to KILL ALL ({int(5 - elapsed)}s) "
                    put_footer(h - 2, msg.center(w - 1)[:w - 1], RED)

            # ── Footer ────────────────────────────────────────────
            footer = "[#]view [r]restart [h]halt [^A]start [^P]pause [^K]kill [q]quit"
            put_footer(h - 1, footer.center(w - 1)[:w - 1], CYAN)

            stdscr.refresh()

        except curses.error:
            try:
                stdscr.clear()
                stdscr.refresh()
                curses.flushinp()
            except Exception:
                pass

        # ── Input ─────────────────────────────────────────────────
        try:
            key = stdscr.getch()
            curses.flushinp()
        except Exception:
            key = -1

        if key == -1:
            time.sleep(REFRESH_SEC)
            continue

        if key == ord("q"):
            running = False
            break

        elif key == 1:   # Ctrl+A
            def _start_all():
                for s in SCRIPTS:
                    if s.get("group") == GROUP_PIPELINE:
                        with state_lock:
                            st = state[s["name"]].get("status")
                        if st not in ("RUNNING",):
                            start_script(s["name"])
                            time.sleep(STARTUP_DELAY_SEC)
            threading.Thread(target=_start_all, daemon=True).start()
            mlog("Ctrl+A — starting all pipeline workers")
            try:
                stdscr.clear()
            except Exception:
                pass

        elif key == 16:  # Ctrl+P
            def _pause_all():
                for s in SCRIPTS:
                    if s.get("group") == GROUP_PIPELINE:
                        with state_lock:
                            st = state[s["name"]].get("status")
                        if st == "RUNNING":
                            halt_script(s["name"])
            threading.Thread(target=_pause_all, daemon=True).start()
            mlog("Ctrl+P — halting all pipeline workers")

        elif key == 11:  # Ctrl+K
            if kill_confirm and (now_ts() - kill_confirm_ts) < 5:
                running = False
                break
            else:
                kill_confirm    = True
                kill_confirm_ts = now_ts()

        elif key == ord("r"):
            name  = vname
            group = next((s.get("group") for s in SCRIPTS if s["name"] == name), GROUP_PIPELINE)
            if group == GROUP_BACKGROUND:
                mlog(f"{name} is always-on — use Ctrl+K to stop all")
            else:
                threading.Thread(target=restart_script, args=(name,), daemon=True).start()

        elif key == ord("h"):
            name  = vname
            group = next((s.get("group") for s in SCRIPTS if s["name"] == name), GROUP_PIPELINE)
            if group == GROUP_BACKGROUND:
                mlog(f"{name} is always-on — cannot halt")
            else:
                with state_lock:
                    cur = state[name].get("status")
                if cur == "HALTED":
                    threading.Thread(target=start_script, args=(name,), daemon=True).start()
                    mlog(f"resuming {name}")
                elif cur == "RUNNING":
                    threading.Thread(target=halt_script, args=(name,), daemon=True).start()

        else:
            char = chr(key) if 0 <= key < 256 else ""
            if char in key_to_index:
                log_view_index = key_to_index[char]

        time.sleep(REFRESH_SEC)


def main():
    global running

    ensure_dirs()

    print(f"ANTII v{VERSION} — Overreaction Fading  [mode={MODE}]")
    print("shadow auto-starts. Pipeline workers start manually in TUI (Ctrl+A).")
    print()

    init_state()

    for s in SCRIPTS:
        threading.Thread(
            target=tail_log,
            args=(s["name"], s["log"]),
            daemon=True,
        ).start()

    threading.Thread(target=stats_loop,   daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    # Auto-start background workers
    for s in SCRIPTS:
        if s.get("group") == GROUP_BACKGROUND:
            threading.Thread(target=start_script, args=(s["name"],), daemon=True).start()
            mlog(f"auto-started background: {s['name']}")
            time.sleep(0.3)

    try:
        curses.wrapper(draw)
    except KeyboardInterrupt:
        pass
    finally:
        _curses_active = False
        running = False
        print("\nShutting down antii workers...")
        stop_all()
        print("Done.")


if __name__ == "__main__":
    main()
