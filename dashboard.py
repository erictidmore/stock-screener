#!/usr/bin/env python3
"""
dashboard.py — Web Dashboard for Stock Screener
=================================================
Single-command entry point: python3 dashboard.py
Opens FastAPI server on localhost:8050 with CRT-styled dashboard.

Endpoints:
    GET  /           -> Dashboard HTML (embedded)
    POST /api/scan   -> Trigger scan in background thread
    WS   /ws         -> Push live state updates
"""

import asyncio
import json
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List

import pytz
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import config as cfg
from filters import (
    check_news_catalysts,
    filter_china_stocks,
    filter_gainers,
)
from screener import get_top_gainers

ET = pytz.timezone("US/Eastern")


# ============================================================================
# Lifespan
# ============================================================================
@asynccontextmanager
async def lifespan(application: FastAPI):
    asyncio.create_task(_periodic_push())
    if cfg.AUTO_ENABLED:
        asyncio.create_task(_autopilot())
    _log("Dashboard started")
    _log(f"Stock Screener {cfg.VERSION}")
    if cfg.AUTO_ENABLED:
        _log("AUTOPILOT ENABLED — scan @9:20 AM, rescan until open, daily reset @8 AM")
    yield


# ============================================================================
# App state
# ============================================================================
app = FastAPI(title="Stock Screener Dashboard", version=cfg.VERSION, lifespan=lifespan)

state = {
    "status": "idle",
    "pipeline_stage": None,
    "pipeline_progress": None,   # e.g. "3/6"
    "scan_results": [],
    "log_lines": [],
    "scan_time": None,
}

connected_clients: List[WebSocket] = []
_scan_lock = threading.Lock()


def _log(msg: str):
    ts = datetime.now(ET).strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    state["log_lines"].append(entry)
    if len(state["log_lines"]) > 200:
        state["log_lines"] = state["log_lines"][-200:]


# ============================================================================
# WebSocket broadcast
# ============================================================================
async def broadcast(data: dict):
    dead = []
    msg = json.dumps(data, default=str)
    for ws in connected_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in connected_clients:
            connected_clients.remove(ws)


async def push_state():
    await broadcast({
        "type": "state",
        "status": state["status"],
        "pipeline_stage": state["pipeline_stage"],
        "pipeline_progress": state["pipeline_progress"],
        "scan_results": state["scan_results"],
        "log_lines": state["log_lines"][-50:],
        "scan_time": state["scan_time"],
    })


async def _periodic_push():
    while True:
        if connected_clients:
            await push_state()
        await asyncio.sleep(1)


# ============================================================================
# Autopilot scheduler (24/7 autonomous operation)
# ============================================================================
async def _autopilot():
    """
    Autonomous daily cycle:
      8:00 AM ET  — reset daily state (clear previous session)
      9:20 AM ET  — auto-scan (full pipeline with news + China filter)
      9:20–9:29   — rescan every 5 min (price/change only, carry news data)
      9:30 AM ET  — market open, stop rescanning
      4:00 PM ET  — end of day, mark idle
    Skips weekends. Checks every 30 seconds.
    """
    scan_done_today = False
    reset_done_today = False
    eod_done_today = False
    last_trading_date = None
    last_rescan_minute = 0

    while True:
        try:
            now = datetime.now(ET)
            current_minute = now.hour * 60 + now.minute
            weekday = now.weekday()  # 0=Mon ... 6=Sun

            # New day detection
            today_str = now.strftime("%Y-%m-%d")
            if last_trading_date != today_str:
                last_trading_date = today_str
                scan_done_today = False
                reset_done_today = False
                eod_done_today = False
                last_rescan_minute = 0

            # Skip weekends
            if weekday >= 5:
                await asyncio.sleep(30)
                continue

            # 8:00 AM ET: Daily reset
            if current_minute >= cfg.AUTO_RESET_MINUTE and not reset_done_today:
                reset_done_today = True
                _log("AUTOPILOT: Daily reset — clearing previous session")
                state["scan_results"] = []
                state["pipeline_stage"] = None
                state["pipeline_progress"] = None
                state["scan_time"] = None
                if state["status"] != "scanning":
                    state["status"] = "idle"

            # 9:20 AM ET: First auto-scan
            if (current_minute >= cfg.AUTO_SCAN_MINUTE
                    and not scan_done_today
                    and state["status"] != "scanning"):
                scan_done_today = True
                last_rescan_minute = current_minute
                _log("AUTOPILOT: Triggering market scan")
                thread = threading.Thread(target=_run_scan_sync, daemon=True)
                thread.start()

            # 9:20–9:29: Rescan every N minutes (fast refresh)
            if (scan_done_today
                    and current_minute < cfg.MARKET_OPEN_MINUTE
                    and current_minute >= cfg.AUTO_SCAN_MINUTE
                    and state["status"] != "scanning"
                    and current_minute - last_rescan_minute >= cfg.AUTO_RESCAN_INTERVAL):
                last_rescan_minute = current_minute
                _log("AUTOPILOT: Rescanning (pre-market refresh)")
                thread = threading.Thread(target=_run_scan_sync, daemon=True)
                thread.start()

            # 4:00 PM ET: End of day
            if current_minute >= cfg.MARKET_CLOSE_MINUTE and not eod_done_today:
                eod_done_today = True
                _log("AUTOPILOT: Market closed — day complete")

        except Exception as e:
            _log(f"AUTOPILOT ERROR: {e}")
            traceback.print_exc()

        await asyncio.sleep(30)


# ============================================================================
# Scan pipeline (runs in background thread)
# ============================================================================
def _run_scan_sync():
    if not _scan_lock.acquire(blocking=False):
        _log("Scan already in progress")
        return

    try:
        state["status"] = "scanning"
        state["scan_results"] = []
        state["pipeline_stage"] = "PULLING GAINERS"
        state["pipeline_progress"] = None

        # Stage 1: Pull gainers
        _log("Pulling top gainers from Alpaca...")
        try:
            raw_gainers, last_updated = get_top_gainers(cfg.SCREENER_TOP)
        except Exception as e:
            _log(f"ERROR: Failed to pull gainers: {e}")
            state["status"] = "error"
            state["pipeline_stage"] = None
            return
        _log(f"{len(raw_gainers)} results from screener (updated {last_updated})")

        # Stage 2: Filter
        state["pipeline_stage"] = "FILTERING"
        filtered = filter_gainers(
            raw_gainers,
            min_change=cfg.SCREENER_MIN_CHANGE,
            max_price=cfg.SCREENER_MAX_PRICE,
            min_price=cfg.SCREENER_MIN_PRICE,
            exclude_warrants=True,
        )
        _log(f"Filtering: {len(filtered)} passed (${cfg.SCREENER_MIN_PRICE}-${cfg.SCREENER_MAX_PRICE}, {cfg.SCREENER_MIN_CHANGE}%+ change)")

        if not filtered:
            _log("No symbols passed filters")
            state["status"] = "complete"
            state["pipeline_stage"] = "COMPLETE"
            state["scan_time"] = datetime.now(ET).isoformat()
            return

        # Stage 3: China check
        state["pipeline_stage"] = "CHINA CHECK"
        total = len(filtered)
        for i, g in enumerate(filtered):
            state["pipeline_progress"] = f"{i+1}/{total}"
        try:
            pre_count = len(filtered)
            filtered = filter_china_stocks(filtered)
            removed = pre_count - len(filtered)
            if removed:
                _log(f"SEC EDGAR: removed {removed} Chinese/shell stocks")
            else:
                _log("SEC EDGAR: no Chinese stocks detected")
        except Exception as e:
            _log(f"SEC EDGAR check failed: {e}, skipping")

        if not filtered:
            _log("All symbols removed by China filter")
            state["status"] = "complete"
            state["pipeline_stage"] = "COMPLETE"
            state["scan_time"] = datetime.now(ET).isoformat()
            return

        # Stage 4: News check
        state["pipeline_stage"] = "NEWS CHECK"
        total = len(filtered)
        for i, g in enumerate(filtered):
            state["pipeline_progress"] = f"{i+1}/{total}"
        try:
            filtered = check_news_catalysts(filtered, hours=cfg.NEWS_LOOKBACK_HOURS)
            catalysts = sum(1 for g in filtered if g.get("news_catalyst"))
            _log(f"News check: {catalysts}/{len(filtered)} have catalysts")
        except Exception as e:
            _log(f"News check failed: {e}, skipping")

        # Stage 5: Complete
        state["pipeline_stage"] = "COMPLETE"
        state["pipeline_progress"] = None
        state["status"] = "complete"
        state["scan_results"] = filtered
        state["scan_time"] = datetime.now(ET).isoformat()

        symbols = [g["symbol"] for g in filtered]
        _log(f"Watchlist ({len(symbols)}): {', '.join(symbols)}")

    except Exception as e:
        _log(f"Scan error: {e}")
        traceback.print_exc()
        state["status"] = "error"
        state["pipeline_stage"] = None
    finally:
        _scan_lock.release()


# ============================================================================
# Endpoints
# ============================================================================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    try:
        await push_state()
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in connected_clients:
            connected_clients.remove(ws)


# ============================================================================
# Embedded Dashboard HTML
# ============================================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>screener """ + cfg.VERSION + """</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #000000;
  --surface: #0a0a0a;
  --surface2: #111111;
  --surface3: #161616;
  --border: #222222;
  --border-active: #D97757;
  --text: #a0a0a0;
  --text-dim: #606060;
  --text-muted: #383838;
  --text-bright: #d4d4d4;
  --text-white: #f0f0f0;
  --green: #00ff41;
  --green-bright: #33ff66;
  --green-bg: rgba(0,255,65,0.04);
  --green-border: rgba(0,255,65,0.2);
  --red: #E8956F;
  --red-bright: #F0AD8A;
  --red-bg: rgba(232,149,111,0.04);
  --red-border: rgba(232,149,111,0.2);
  --accent: #D97757;
  --accent-bright: #E8956F;
  --accent-dim: #B85C3A;
  --accent-bg: rgba(217,119,87,0.06);
  --accent-border: rgba(217,119,87,0.25);
  --accent-glow: rgba(217,119,87,0.6);
  --accent-alt: #E8A04E;
  --cyan: #00f0ff;
  --glow-text: 0 0 8px rgba(217,119,87,0.5);
  --glow-text-strong: 0 0 10px rgba(217,119,87,0.7), 0 0 20px rgba(217,119,87,0.35);
  --glow-box: 0 0 6px rgba(217,119,87,0.2), inset 0 0 6px rgba(217,119,87,0.05);
}
html { font-size: 13px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
  line-height: 1.55;
  min-height: 100vh;
  overflow-x: hidden;
  text-shadow: var(--glow-text);
  animation: flicker 5s infinite;
}
/* CRT scanline overlay */
body::before {
  content: '';
  position: fixed;
  top: 0; left: 0;
  width: 100%; height: 100%;
  background: repeating-linear-gradient(
    0deg,
    rgba(0,0,0,0.12) 0px,
    rgba(0,0,0,0.12) 1px,
    transparent 1px,
    transparent 3px
  );
  pointer-events: none;
  z-index: 9999;
}
@keyframes flicker {
  0%, 100% { opacity: 1; }
  92% { opacity: 1; }
  93% { opacity: 0.96; }
  94% { opacity: 1; }
  96% { opacity: 0.98; }
  97% { opacity: 1; }
}
/* Morphing gradient orbs */
.morph-bg {
  position: fixed;
  top: 0; left: 0;
  width: 100%; height: 100%;
  z-index: -1;
  overflow: hidden;
}
.morph-bg .orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  opacity: 0.07;
  animation: orb-drift 12s ease-in-out infinite alternate;
}
.morph-bg .orb-1 {
  width: 500px; height: 500px;
  background: radial-gradient(circle, #D97757, transparent 70%);
  top: -10%; left: -5%;
  animation-duration: 14s;
}
.morph-bg .orb-2 {
  width: 400px; height: 400px;
  background: radial-gradient(circle, #E8A04E, transparent 70%);
  bottom: -15%; right: -5%;
  animation-duration: 10s;
  animation-delay: -5s;
}
.morph-bg .orb-3 {
  width: 350px; height: 350px;
  background: radial-gradient(circle, #00f0ff, transparent 70%);
  top: 40%; left: 50%;
  animation-duration: 16s;
  animation-delay: -8s;
}
@keyframes orb-drift {
  0% { transform: translate(0, 0) scale(1); }
  33% { transform: translate(40px, -30px) scale(1.1); }
  66% { transform: translate(-20px, 50px) scale(0.9); }
  100% { transform: translate(30px, 20px) scale(1.05); }
}
/* Blinking cursor */
.cursor-blink {
  animation: blink-cursor 1s step-end infinite;
  color: var(--accent);
  text-shadow: 0 0 10px var(--accent-glow);
}
@keyframes blink-cursor {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--accent-dim); border-radius: 0; }
::-webkit-scrollbar-thumb:hover { background: var(--accent); }

/* ===== TOPBAR ===== */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: sticky;
  top: 0;
  z-index: 100;
  background-image: linear-gradient(to right, rgba(217,119,87,0.05), transparent 30%, transparent 70%, rgba(232,160,78,0.03));
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 14px;
}
.logo {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--accent-bright);
  text-shadow: 0 0 10px var(--accent-glow);
}
.logo .ver { color: var(--text-dim); text-shadow: none; }
.logo .prompt { color: var(--cyan); text-shadow: 0 0 8px rgba(0,240,255,0.5); }
.topbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.clock {
  font-size: 10px;
  color: var(--text-dim);
  letter-spacing: 0.5px;
}
.conn-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--red);
  box-shadow: 0 0 4px var(--red);
  transition: all 0.3s;
}
.conn-dot.connected {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent), 0 0 12px var(--accent), 0 0 20px var(--accent-glow);
  animation: glow-pulse 2s ease-in-out infinite;
}
@keyframes glow-pulse {
  0%, 100% { box-shadow: 0 0 6px var(--accent), 0 0 12px var(--accent), 0 0 20px var(--accent-glow); }
  50% { box-shadow: 0 0 8px var(--accent-bright), 0 0 16px var(--accent), 0 0 30px rgba(217,119,87,0.5); }
}

.next-schedule {
  font-size: 10px;
  color: var(--accent-dim);
  letter-spacing: 0.5px;
}
.next-schedule .sch-label { color: var(--accent-dim); }
.next-schedule .sch-time { color: var(--accent); }
.next-schedule .sch-active { color: var(--accent-bright); text-shadow: 0 0 6px var(--accent-glow); }

/* ===== LAYOUT ===== */
.container {
  width: 100%;
  margin: 0 auto;
  padding: 16px 24px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.full-width { grid-column: 1 / -1; }

/* ===== PANELS ===== */
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 2px;
  overflow: hidden;
  box-shadow: var(--glow-box);
  transition: border-color 0.3s, box-shadow 0.3s;
}
.panel:hover {
  border-color: var(--border-active);
  box-shadow: 0 0 12px rgba(217,119,87,0.15), inset 0 0 8px rgba(217,119,87,0.03);
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(135deg, var(--surface2), rgba(217,119,87,0.03));
}
.panel-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--accent-bright);
  text-shadow: 0 0 10px var(--accent-glow);
}
.panel-body {
  padding: 12px 16px;
}
.panel-body.no-pad { padding: 0; }

/* ===== PIPELINE TRACKER ===== */
.pipeline {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 16px 0 12px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 1px;
}
.pipeline-stage {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  text-shadow: none;
  transition: color 0.3s, text-shadow 0.3s;
}
.pipeline-stage .dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  transition: all 0.3s;
}
.pipeline-stage.active {
  color: var(--accent-bright);
  text-shadow: 0 0 8px var(--accent-glow);
}
.pipeline-stage.active .dot {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent), 0 0 12px var(--accent-glow);
  animation: glow-pulse 1.5s ease-in-out infinite;
}
.pipeline-stage.complete {
  color: var(--green);
  text-shadow: 0 0 6px rgba(0,255,65,0.5);
}
.pipeline-stage.complete .dot {
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
}
.pipeline-arrow {
  color: var(--text-muted);
  font-size: 11px;
  text-shadow: none;
}

/* Progress bar */
.scan-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 20px 4px;
}
.progress-track {
  flex: 1;
  height: 8px;
  background: var(--surface3);
  border: 1px solid var(--border);
  border-radius: 1px;
  position: relative;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent-dim), var(--accent), var(--accent-alt));
  box-shadow: 0 0 10px var(--accent-glow);
  transition: width 0.5s ease;
  background-size: 200% 100%;
  animation: shimmer 3s linear infinite;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
.progress-label {
  font-size: 10px;
  color: var(--text-dim);
  text-shadow: none;
  min-width: 30px;
}

/* ===== TABLES ===== */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
th {
  text-align: left;
  padding: 8px 12px;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--text-muted);
  background: var(--surface2);
  border-bottom: 1px solid var(--border);
  text-shadow: none;
}
th.right, td.right { text-align: right; }
td {
  padding: 7px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(217,119,87,0.04); }
.sym { color: var(--accent-bright); font-weight: 600; text-shadow: 0 0 8px var(--accent-glow); }
.positive { color: var(--green); text-shadow: 0 0 8px rgba(0,255,65,0.5); }
.negative { color: var(--red); text-shadow: 0 0 8px rgba(232,149,111,0.6); }
.tag-catalyst {
  font-size: 9px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 0;
  background: var(--green-bg);
  color: var(--green);
  border: 1px solid var(--green-border);
  text-shadow: 0 0 6px rgba(0,255,65,0.5);
}
.tag-nonews {
  font-size: 9px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 0;
  background: var(--surface3);
  color: var(--text-muted);
  border: 1px solid var(--border);
  text-shadow: none;
}
.empty-msg {
  padding: 30px;
  text-align: center;
  color: var(--text-dim);
  font-size: 11px;
}

/* ===== NEWS PANEL ===== */
.news-list {
  padding: 10px 14px;
  font-size: 10px;
  line-height: 1.8;
  max-height: 300px;
  overflow-y: auto;
}
.news-sym {
  color: var(--accent-bright);
  font-weight: 700;
  text-shadow: 0 0 8px var(--accent-glow);
  margin-top: 8px;
}
.news-sym:first-child { margin-top: 0; }
.news-headline {
  color: var(--text);
  padding-left: 12px;
  text-shadow: none;
}
.news-time {
  color: var(--cyan);
  text-shadow: 0 0 6px rgba(0,240,255,0.4);
}

/* ===== LOG ===== */
.log-feed {
  max-height: 220px;
  overflow-y: auto;
  padding: 10px 14px;
  font-size: 10px;
  line-height: 1.8;
  color: var(--text-dim);
}
.log-feed div { white-space: nowrap; text-shadow: none; }
.log-feed .fresh { color: var(--text-bright); text-shadow: 0 0 6px var(--accent-glow); }

/* ===== RESPONSIVE ===== */
@media (max-width: 900px) {
  .container { grid-template-columns: 1fr; }
  .pipeline { flex-wrap: wrap; }
}
</style>
</head>
<body>

<div class="morph-bg">
  <div class="orb orb-1"></div>
  <div class="orb orb-2"></div>
  <div class="orb orb-3"></div>
</div>

<!-- ===== TOPBAR ===== -->
<div class="topbar">
  <div class="topbar-left">
    <div class="logo"><span class="prompt">~</span> screener <span class="ver">""" + cfg.VERSION + """</span> <span class="cursor-blink">&#x2588;</span></div>
    <span id="nextSchedule" class="next-schedule"></span>
  </div>
  <div class="topbar-right">
    <span id="clock" class="clock"></span>
    <div id="connDot" class="conn-dot" title="WebSocket disconnected"></div>
  </div>
</div>

<div class="container">

  <!-- PIPELINE PANEL -->
  <div class="panel full-width">
    <div class="panel-header">
      <span class="panel-title">&#9484;&#9472;[ PIPELINE ]&#9472;&#9488;</span>
      <span id="scanTime" style="font-size:9px;color:var(--text-dim);text-shadow:none;"></span>
    </div>
    <div class="panel-body">
      <div id="pipeline" class="pipeline">
        <div class="pipeline-stage" data-stage="PULLING GAINERS"><span class="dot"></span>GAINERS</div>
        <span class="pipeline-arrow">&rarr;</span>
        <div class="pipeline-stage" data-stage="FILTERING"><span class="dot"></span>FILTER</div>
        <span class="pipeline-arrow">&rarr;</span>
        <div class="pipeline-stage" data-stage="CHINA CHECK"><span class="dot"></span>CHINA</div>
        <span class="pipeline-arrow">&rarr;</span>
        <div class="pipeline-stage" data-stage="NEWS CHECK"><span class="dot"></span>NEWS</div>
        <span class="pipeline-arrow">&rarr;</span>
        <div class="pipeline-stage" data-stage="COMPLETE"><span class="dot"></span>DONE</div>
      </div>
      <div id="progressBar" class="scan-progress" style="display:none;">
        <div class="progress-track"><div id="progressFill" class="progress-fill" style="width:0%"></div></div>
        <span id="progressLabel" class="progress-label"></span>
      </div>
    </div>
  </div>

  <!-- RESULTS TABLE -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">&#9484;&#9472;[ RESULTS ]&#9472;&#9488;</span>
    </div>
    <div class="panel-body no-pad">
      <div id="resultsTable"></div>
    </div>
  </div>

  <!-- NEWS CATALYSTS -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">&#9484;&#9472;[ NEWS CATALYSTS ]&#9472;&#9488;</span>
    </div>
    <div id="newsPanel" class="news-list">
      <div class="empty-msg">&gt; awaiting scan results..._</div>
    </div>
  </div>

  <!-- LOG FEED -->
  <div class="panel full-width">
    <div class="panel-header">
      <span class="panel-title">&#9484;&#9472;[ LOG ]&#9472;&#9488;</span>
    </div>
    <div id="logFeed" class="log-feed">
      <div class="fresh">&gt; Waiting for connection...</div>
    </div>
  </div>

</div>

<script>
// ===== STATE =====
let ws = null;
let wsConnected = false;
let lastState = {};

const STAGES = ['PULLING GAINERS', 'FILTERING', 'CHINA CHECK', 'NEWS CHECK', 'COMPLETE'];

// ===== CLOCK =====
function updateClock() {
  const now = new Date();
  const et = now.toLocaleString('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:true});
  document.getElementById('clock').textContent = 'ET ' + et;
}
setInterval(updateClock, 1000);
updateClock();

// ===== SCHEDULE DISPLAY =====
const AUTO_ENABLED = """ + ("true" if cfg.AUTO_ENABLED else "false") + """;
const SCAN_MINUTE = """ + str(cfg.AUTO_SCAN_MINUTE) + """;
const MARKET_OPEN_MINUTE = """ + str(cfg.MARKET_OPEN_MINUTE) + """;
const MARKET_CLOSE_MINUTE = """ + str(cfg.MARKET_CLOSE_MINUTE) + """;

function minuteToTime(m) {
  let h = Math.floor(m / 60);
  let mm = m % 60;
  let ampm = h >= 12 ? 'PM' : 'AM';
  if (h > 12) h -= 12;
  if (h === 0) h = 12;
  return h + ':' + (mm < 10 ? '0' : '') + mm + ' ' + ampm;
}

function formatCountdown(diffMs) {
  if (diffMs <= 0) return 'now';
  const totalSec = Math.floor(diffMs / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm';
  return '<1m';
}

function updateSchedule() {
  const el = document.getElementById('nextSchedule');
  if (!AUTO_ENABLED) { el.textContent = ''; return; }

  const status = (lastState && lastState.status) || 'idle';
  const now = new Date();
  const etStr = now.toLocaleString('en-US', {timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit'});
  const etParts = etStr.split(':');
  const currentMin = parseInt(etParts[0]) * 60 + parseInt(etParts[1]);
  const weekday = new Date(now.toLocaleString('en-US', {timeZone: 'America/New_York'})).getDay();

  if (status === 'scanning') {
    el.innerHTML = '<span class="sch-active">scanning market...</span>';
    return;
  }

  // Weekend
  if (weekday === 0 || weekday === 6) {
    const daysUntilMon = weekday === 6 ? 2 : 1;
    el.innerHTML = '<span class="sch-label">next scan</span> <span class="sch-time">Mon ' + minuteToTime(SCAN_MINUTE) + ' ET</span>';
    return;
  }

  let parts = [];

  if (currentMin < SCAN_MINUTE) {
    const scanMs = (SCAN_MINUTE - currentMin) * 60000;
    parts.push('<span class="sch-label">scan</span> <span class="sch-time">' + formatCountdown(scanMs) + '</span>');
  }
  if (currentMin < MARKET_OPEN_MINUTE) {
    const openMs = (MARKET_OPEN_MINUTE - currentMin) * 60000;
    parts.push('<span class="sch-label">open</span> <span class="sch-time">' + formatCountdown(openMs) + '</span>');
  } else if (currentMin < MARKET_CLOSE_MINUTE) {
    const closeMs = (MARKET_CLOSE_MINUTE - currentMin) * 60000;
    parts.push('<span class="sch-label">close</span> <span class="sch-time">' + formatCountdown(closeMs) + '</span>');
  }
  if (currentMin >= MARKET_CLOSE_MINUTE) {
    const isFri = weekday === 5;
    const nextDay = isFri ? 'Mon' : 'tomorrow';
    parts.push('<span class="sch-label">next scan</span> <span class="sch-time">' + nextDay + ' ' + minuteToTime(SCAN_MINUTE) + ' ET</span>');
  }

  el.innerHTML = parts.length ? parts.join(' <span class="sch-label">&middot;</span> ') : '';
}
setInterval(updateSchedule, 5000);
updateSchedule();

// ===== WEBSOCKET =====
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    wsConnected = true;
    document.getElementById('connDot').className = 'conn-dot connected';
    document.getElementById('connDot').title = 'WebSocket connected';
  };
  ws.onclose = () => {
    wsConnected = false;
    document.getElementById('connDot').className = 'conn-dot';
    document.getElementById('connDot').title = 'WebSocket disconnected';
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'state') handleState(data);
    } catch(err) {}
  };
}
connectWS();

// ===== RENDER STATE =====
function handleState(s) {
  lastState = s;
  renderPipeline(s);
  renderResults(s.scan_results || []);
  renderNews(s.scan_results || []);
  renderLog(s.log_lines || []);

  updateSchedule();

  // Scan time
  const scanTimeEl = document.getElementById('scanTime');
  if (s.scan_time) {
    const d = new Date(s.scan_time);
    const t = d.toLocaleString('en-US', {timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:true});
    scanTimeEl.textContent = 'last scan: ' + t;
  }
}

function renderPipeline(s) {
  const stages = document.querySelectorAll('.pipeline-stage');
  const currentStage = s.pipeline_stage;
  const status = s.status;
  const currentIdx = STAGES.indexOf(currentStage);

  stages.forEach((el, i) => {
    const stageName = el.dataset.stage;
    const stageIdx = STAGES.indexOf(stageName);

    el.classList.remove('active', 'complete');

    if (status === 'complete' || status === 'idle' && s.scan_results && s.scan_results.length > 0) {
      // All stages complete if we have results
      if (s.scan_results && s.scan_results.length > 0) {
        el.classList.add('complete');
      }
    } else if (status === 'scanning' && currentIdx >= 0) {
      if (stageIdx < currentIdx) {
        el.classList.add('complete');
      } else if (stageIdx === currentIdx) {
        el.classList.add('active');
      }
    }
  });

  // Progress bar
  const progressBar = document.getElementById('progressBar');
  const progressFill = document.getElementById('progressFill');
  const progressLabel = document.getElementById('progressLabel');

  if (status === 'scanning' && currentStage) {
    progressBar.style.display = 'flex';
    // Calculate overall progress
    const totalStages = STAGES.length - 1; // Exclude COMPLETE
    const stageProgress = currentIdx >= 0 ? currentIdx : 0;
    let pct = (stageProgress / totalStages) * 100;

    // Add sub-progress from pipeline_progress
    if (s.pipeline_progress) {
      const parts = s.pipeline_progress.split('/');
      if (parts.length === 2) {
        const sub = parseInt(parts[0]) / parseInt(parts[1]);
        pct += (sub / totalStages) * 100;
      }
    }
    progressFill.style.width = Math.min(pct, 100) + '%';
    progressLabel.textContent = s.pipeline_progress || (currentIdx + 1) + '/' + totalStages;
  } else if (status === 'complete') {
    progressBar.style.display = 'flex';
    progressFill.style.width = '100%';
    progressLabel.textContent = 'done';
  } else {
    progressBar.style.display = 'none';
  }
}

function renderResults(results) {
  const el = document.getElementById('resultsTable');
  if (!results.length) {
    el.innerHTML = '<div class="empty-msg">&gt; awaiting scan..._</div>';
    return;
  }
  let html = '<table><thead><tr><th>#</th><th>Sym</th><th class="right">Price</th><th class="right">Chg%</th><th>News</th></tr></thead><tbody>';
  results.forEach((g, i) => {
    const pctClass = g.change_pct >= 0 ? 'positive' : 'negative';
    const newsTag = g.news_catalyst === true
      ? '<span class="tag-catalyst">CATALYST</span>'
      : g.news_catalyst === false
        ? '<span class="tag-nonews">NO NEWS</span>'
        : '';
    html += '<tr>'
      + '<td>' + (i+1) + '</td>'
      + '<td class="sym">' + escHtml(g.symbol) + '</td>'
      + '<td class="right">$' + g.price.toFixed(2) + '</td>'
      + '<td class="right ' + pctClass + '">' + (g.change_pct >= 0 ? '+' : '') + g.change_pct.toFixed(1) + '%</td>'
      + '<td>' + newsTag + '</td>'
      + '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderNews(results) {
  const el = document.getElementById('newsPanel');
  const withNews = results.filter(g => g.news_headlines && g.news_headlines.length > 0);
  if (!withNews.length) {
    if (results.length > 0) {
      el.innerHTML = '<div class="empty-msg">&gt; no catalysts found_</div>';
    } else {
      el.innerHTML = '<div class="empty-msg">&gt; awaiting scan results..._</div>';
    }
    return;
  }
  let html = '';
  withNews.forEach(g => {
    html += '<div class="news-sym">' + escHtml(g.symbol) + ':</div>';
    g.news_headlines.slice(0, 3).forEach(h => {
      html += '<div class="news-headline"><span class="news-time">[' + escHtml(h.time) + ']</span> ' + escHtml(h.headline) + '</div>';
    });
  });
  el.innerHTML = html;
}

function renderLog(log) {
  const el = document.getElementById('logFeed');
  if (!log.length) return;
  let html = '';
  log.forEach((line, i) => {
    const cls = i >= log.length - 3 ? 'fresh' : '';
    html += '<div class="' + cls + '">&gt; ' + escHtml(line) + '</div>';
  });
  el.innerHTML = html;
  el.scrollTop = el.scrollHeight;
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""


# ============================================================================
# Entry point
# ============================================================================
if __name__ == "__main__":
    print(f"Stock Screener Dashboard {cfg.VERSION}")
    print(f"http://localhost:8051")
    uvicorn.run(app, host="0.0.0.0", port=8051, log_level="warning")
