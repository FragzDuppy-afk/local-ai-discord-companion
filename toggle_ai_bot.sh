#!/usr/bin/env bash
# ==============================================================================
# toggle_ai_bot.sh — Smart Start/Stop toggle for AI Bot, Offloader, & Overseer
# ==============================================================================

cd "$(dirname "$(realpath "$0")")" || exit 1

# Auto-detect local virtual environment (matches README's `venv` or `.venv`)
PY_EXE="python3"
if [ -x "./venv/bin/python" ]; then
    PY_EXE="./venv/bin/python"
elif [ -x "./.venv/bin/python" ]; then
    PY_EXE="./.venv/bin/python"
fi

PATTERN="(offloader\.py|Overseer\.py|AI_Bot\.py)"

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
    echo "[TOGGLE] Active processes detected. Shutting down AI Bot suite..."
    pkill -TERM -f "$PATTERN" 2>/dev/null
    sleep 1
    # Force-kill any stubborn process still holding resources
    pkill -KILL -f "$PATTERN" 2>/dev/null

    rm -f offloader_state.json lms_cli.lock
    echo "[TOGGLE] AI Bot stopped cleanly."
else
    echo "[TOGGLE] Starting AI Bot, Offloader, and Overseer in the background..."
    nohup "$PY_EXE" -u offloader.py >/dev/null 2>&1 &
    sleep 1
    nohup "$PY_EXE" -u Overseer.py >/dev/null 2>&1 &
    sleep 1
    nohup "$PY_EXE" -u AI_Bot.py >/dev/null 2>&1 &
    echo "[TOGGLE] AI Bot suite launched using $PY_EXE."
fi
