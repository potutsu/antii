"""
manager.py — antii headless process manager + HTTP dashboard
No curses. Survives Termux terminal drops, screen rotations, session crashes.

Run:
    python manager.py

Dashboard:
    Open http://localhost:7070 in your phone browser
    Auto-refreshes every 5 seconds

CLI controls (stdin, type + Enter):
    start   — start all pipeline workers
    stop    — stop all pipeline workers
    restart <name> — restart one worker
    halt <name>    — halt one worker
    status  — print worker status
    quit    — stop all + exit
"""

import json, os, signal, subprocess, sys, time, threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from antii_config import (
    SCRIPTS, STARTUP_DELAY_SEC, RESTART_COOLDOWN_SEC,
    MAX_RESTARTS, RESTART_RESET_SEC, ON_CRASH,
    ALERT_BOT_TOKEN, ALERT_CHAT_ID, MODE,
)
from paths import ensure_dirs, SIGNAL_JSONL, PAPER_POSITIONS, TRADE_LOG, POSTMORTEM_JSONL

import requests as _requests

PYTHON  = sys.executable
VERSION = "2.0.0"
PORT    = 7070

GROUP_PIPELINE   = "pipeline"
GROUP_BACKGROUND = "background"

# ── Globals ───────────────────────────────────────────────────────
state      = {}
state_lock = threading.Lock()
running    = True
start_time = time.time()

manager_log      = deque(maxlen=200)
manager_log_lock = threading.Lock()


def now_ts() -> float:
    return time.time()

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")

def format_uptime(start_ts):
    if start_ts is None:
        return "—"
    sec = int(now_ts() - start_ts)
    if sec < 60:   return f"{sec}s"
    if sec < 3600: return f"{sec//60}m {sec%60}s"
    h = sec // 3600; m = (sec % 3600) // 60
    return f"{h}h {m}m"

def mlog(msg: str):
    line = f"{now_iso()} [manager] {msg}"
    with manager_log_lock:
        manager_log.append(line)
    print(line, flush=True)

def send_alert(msg: str):
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage",
            json={"chat_id": ALERT_CHAT_ID, "text": f"🚨 ANTII: {msg}"},
            timeout=5,
        )
    except Exception:
        pass


# ── Stats ─────────────────────────────────────────────────────────
_stats = {"signals": 0, "open": 0, "closed": 0, "correct": 0,
          "win_rate": 0.0, "postmortems": 0, "total_pnl": 0.0}
_stats_lock = threading.Lock()

def _read_stats():
    s = {"signals": 0, "open": 0, "closed": 0, "correct": 0,
         "win_rate": 0.0, "postmortems": 0, "total_pnl": 0.0}
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
                    if p.get("status") == "open":
                        s["open"] += 1
                    elif p.get("status") == "closed":
                        s["closed"] += 1
                        if p.get("correct"):
                            s["correct"] += 1
                        s["total_pnl"] += float(p.get("pnl_usd") or 0)
                except Exception:
                    pass
    except Exception:
        pass
    if s["closed"] > 0:
        s["win_rate"] = round(s["correct"] / s["closed"] * 100, 1)
    try:
        if POSTMORTEM_JSONL.exists():
            s["postmortems"] = sum(1 for _ in open(POSTMORTEM_JSONL, errors="replace"))
    except Exception:
        pass
    return s

def stats_loop():
    while running:
        try:
            s = _read_stats()
            with _stats_lock:
                _stats.update(s)
        except Exception:
            pass
        time.sleep(15)


# ── Log tailing ───────────────────────────────────────────────────
def tail_log(name: str, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open(log_path, "a").close()
    buf = deque(maxlen=300)
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
                    state[name]["log_buf"] = list(buf)[-50:]
        except Exception:
            pass
        time.sleep(1)


# ── Process management ────────────────────────────────────────────
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
            # No preexec_fn / setsid — safer on Termux
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
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
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

def stop_all():
    for s in reversed(SCRIPTS):
        name = s["name"]
        with state_lock:
            status = state[name].get("status")
        if status not in ("STOPPED", "HALTED", "NOT FOUND", "FAILED"):
            stop_script(name)

def start_all_pipeline():
    for s in SCRIPTS:
        if s.get("group") == GROUP_PIPELINE:
            with state_lock:
                st = state[s["name"]].get("status")
            if st not in ("RUNNING",):
                start_script(s["name"])
                time.sleep(STARTUP_DELAY_SEC)


# ── Monitor loop ──────────────────────────────────────────────────
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

            mlog(f"CRASH: {name}")

            if start_ts and (now_ts() - start_ts) > RESTART_RESET_SEC:
                with state_lock:
                    state[name]["restarts"] = 0
                restarts = 0

            if group == GROUP_BACKGROUND:
                send_alert(f"{name} crashed — restarting")
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)
                continue

            if restarts >= MAX_RESTARTS:
                mlog(f"gave up restarting {name} after {MAX_RESTARTS} attempts")
                send_alert(f"{name} gave up after {MAX_RESTARTS} crashes")
                with state_lock:
                    state[name]["status"] = "FAILED"
                continue

            if ON_CRASH in ("restart+alert", "alert_only"):
                send_alert(f"{name} crashed — restarting")
            if ON_CRASH in ("restart+alert", "restart_only"):
                time.sleep(RESTART_COOLDOWN_SEC)
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)

        time.sleep(2)


# ── HTTP dashboard ────────────────────────────────────────────────
def _build_html() -> str:
    with state_lock:
        snap = {n: dict(v) for n, v in state.items()}
    with _stats_lock:
        sc = dict(_stats)
    with manager_log_lock:
        mlogs = list(manager_log)[-30:]

    manager_uptime = format_uptime(start_time)
    now_str = now_iso()

    status_color = {
        "RUNNING":   "#00e676",
        "STOPPED":   "#90a4ae",
        "HALTED":    "#ffb300",
        "CRASHED":   "#ff5252",
        "FAILED":    "#ff5252",
        "ERROR":     "#ff5252",
        "NOT FOUND": "#ff5252",
        "STOPPING":  "#ffb300",
    }
    status_icon = {
        "RUNNING":   "●",
        "STOPPED":   "○",
        "HALTED":    "⏸",
        "CRASHED":   "✖",
        "FAILED":    "✖",
        "ERROR":     "✖",
        "NOT FOUND": "✖",
        "STOPPING":  "◌",
    }

    workers_html = ""
    for s in SCRIPTS:
        name   = s["name"]
        info   = snap.get(name, {})
        status = info.get("status", "UNKNOWN")
        uptime = format_uptime(info.get("start_ts"))
        pid    = info.get("pid") or "—"
        rst    = info.get("restarts", 0)
        group  = info.get("group", GROUP_PIPELINE)
        color  = status_color.get(status, "#90a4ae")
        icon   = status_icon.get(status, "?")
        logs   = info.get("log_buf", [])[-8:]
        log_html = "".join(
            f'<div class="log-line">{l[:120]}</div>' for l in logs
        ) or '<div class="log-line dim">no output yet</div>'

        group_badge = '<span class="badge bg">bg</span>' if group == GROUP_BACKGROUND else ""

        workers_html += f"""
        <div class="worker">
          <div class="worker-header">
            <span class="dot" style="color:{color}">{icon}</span>
            <span class="wname">{name}</span>
            {group_badge}
            <span class="wstatus" style="color:{color}">{status}</span>
            <span class="dim">up:{uptime} pid:{pid} rst:{rst}</span>
          </div>
          <div class="log-box">{log_html}</div>
        </div>"""

    pnl_color = "#00e676" if sc["total_pnl"] >= 0 else "#ff5252"
    wr_str    = f"{sc['win_rate']:.0f}%" if sc["closed"] else "—"
    pnl_str   = f"${sc['total_pnl']:+.2f}"

    mlog_html = "".join(
        f'<div class="log-line">{l[-140:]}</div>' for l in reversed(mlogs)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>ANTII Dashboard</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#0d1117; color:#c9d1d9; font-family:monospace; font-size:13px; padding:10px; }}
  h1 {{ color:#58a6ff; font-size:16px; margin-bottom:4px; }}
  .subtitle {{ color:#8b949e; font-size:11px; margin-bottom:12px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
  .stat {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px 12px; }}
  .stat-val {{ font-size:20px; font-weight:bold; color:#58a6ff; }}
  .stat-lbl {{ font-size:10px; color:#8b949e; }}
  .worker {{ background:#161b22; border:1px solid #30363d; border-radius:6px; margin-bottom:8px; padding:8px; }}
  .worker-header {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:6px; }}
  .wname {{ font-weight:bold; color:#e6edf3; }}
  .wstatus {{ font-weight:bold; }}
  .dot {{ font-size:16px; }}
  .dim {{ color:#8b949e; font-size:11px; }}
  .badge {{ font-size:10px; padding:1px 5px; border-radius:3px; }}
  .badge.bg {{ background:#1f2a3c; color:#58a6ff; border:1px solid #1f6feb; }}
  .log-box {{ background:#0d1117; border-radius:4px; padding:6px; max-height:120px; overflow:hidden; }}
  .log-line {{ font-size:11px; color:#8b949e; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; line-height:1.5; }}
  .section-title {{ color:#58a6ff; font-size:12px; margin:12px 0 6px; border-bottom:1px solid #30363d; padding-bottom:4px; }}
  .mlog-box {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px; max-height:200px; overflow:hidden; }}
  .heartbeat {{ font-size:10px; color:#8b949e; margin-top:10px; text-align:right; }}
  .pnl-pos {{ color:#00e676; }}
  .pnl-neg {{ color:#ff5252; }}
</style>
</head>
<body>
<h1>⚡ ANTII [{MODE.upper()}] v{VERSION}</h1>
<div class="subtitle">uptime: {manager_uptime} &nbsp;|&nbsp; {now_str} &nbsp;|&nbsp; auto-refresh 5s</div>

<div class="stats">
  <div class="stat"><div class="stat-val">{sc['signals']}</div><div class="stat-lbl">signals</div></div>
  <div class="stat"><div class="stat-val">{sc['open']}</div><div class="stat-lbl">open</div></div>
  <div class="stat"><div class="stat-val">{sc['closed']}</div><div class="stat-lbl">closed</div></div>
  <div class="stat"><div class="stat-val">{wr_str}</div><div class="stat-lbl">win rate</div></div>
  <div class="stat"><div class="stat-val {'pnl-pos' if sc['total_pnl']>=0 else 'pnl-neg'}">{pnl_str}</div><div class="stat-lbl">paper PnL</div></div>
  <div class="stat"><div class="stat-val">{sc['postmortems']}</div><div class="stat-lbl">postmortems</div></div>
</div>

<div class="section-title">WORKERS</div>
{workers_html}

<div class="section-title">MANAGER LOG</div>
<div class="mlog-box">{mlog_html}</div>

<div class="heartbeat">last render: {now_str}</div>
</body>
</html>"""


class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = _build_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, fmt, *args):
        pass  # silence HTTP access logs


def http_server_loop():
    from socketserver import TCPServer
    TCPServer.allow_reuse_address = True
    # Try PORT, then fallbacks if still in use
    for port in [PORT, PORT+1, PORT+2, 8080, 8888]:
        try:
            server = HTTPServer(("0.0.0.0", port), DashHandler)
            mlog(f"dashboard → http://localhost:{port}")
            server.serve_forever()
            return
        except OSError:
            mlog(f"port {port} in use, trying next...")
    mlog("ERROR: could not bind dashboard to any port")


# ── CLI input ─────────────────────────────────────────────────────
def cli_loop():
    global running
    print(f"Commands: start | stop | restart <name> | halt <name> | status | quit")
    while running:
        try:
            line = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            running = False
            break
        if not line:
            continue
        parts = line.split()
        cmd   = parts[0]

        if cmd == "quit":
            running = False

        elif cmd == "start":
            threading.Thread(target=start_all_pipeline, daemon=True).start()
            mlog("starting all pipeline workers")

        elif cmd == "stop":
            threading.Thread(target=stop_all, daemon=True).start()
            mlog("stopping all workers")

        elif cmd == "restart" and len(parts) > 1:
            name = parts[1]
            if name in state:
                threading.Thread(target=restart_script, args=(name,), daemon=True).start()
            else:
                print(f"unknown worker: {name}")

        elif cmd == "halt" and len(parts) > 1:
            name = parts[1]
            if name in state:
                threading.Thread(target=stop_script, args=(name,), daemon=True).start()
            else:
                print(f"unknown worker: {name}")

        elif cmd == "status":
            with state_lock:
                for name, info in state.items():
                    print(f"  {name:<20} {info['status']:<10} up:{format_uptime(info.get('start_ts'))}")

        else:
            print(f"unknown command: {line}")


# ── Main ──────────────────────────────────────────────────────────
def main():
    global running
    ensure_dirs()
    init_state()

    mlog(f"ANTII v{VERSION} starting [mode={MODE}]")

    # Tail logs
    for s in SCRIPTS:
        threading.Thread(target=tail_log, args=(s["name"], s["log"]), daemon=True).start()

    # Background threads
    threading.Thread(target=stats_loop,   daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=http_server_loop, daemon=True).start()

    # Auto-start background workers
    for s in SCRIPTS:
        if s.get("group") == GROUP_BACKGROUND:
            threading.Thread(target=start_script, args=(s["name"],), daemon=True).start()
            mlog(f"auto-started: {s['name']}")
            time.sleep(0.3)

    mlog("ready — type 'start' to launch pipeline, open http://localhost:7070 for dashboard")

    try:
        cli_loop()
    except Exception as e:
        mlog(f"cli error: {e}")
    finally:
        running = False
        mlog("shutting down...")
        stop_all()
        mlog("done.")


if __name__ == "__main__":
    main()
