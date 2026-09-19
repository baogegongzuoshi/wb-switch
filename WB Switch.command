#!/bin/bash
# ============================================================
#  WB Switch - macOS launcher
#  Kill old panel on port 5276, start fresh with latest code,
#  wait until it answers, then open the browser.
#
#  First run: if blocked by Gatekeeper, run once:
#    xattr -d com.apple.quarantine "./WB Switch.command"
#  or right-click -> Open.
# ============================================================
cd "$(dirname "$0")" || exit 1

# 1) kill any existing panel on 5276 (python only, verified in-script too)
PIDS=$(lsof -t -iTCP:5276 -sTCP:LISTEN 2>/dev/null)
for pid in $PIDS; do
  if ps -p "$pid" -o comm= | grep -q python; then
    kill -9 "$pid" 2>/dev/null
  fi
done

# 2) find python 3 (python3 -> brew python -> python3.11/3.12/3.13)
PY=""
for c in python3 python3.13 python3.12 python3.11 python; do
  if command -v "$c" >/dev/null 2>&1; then
    "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' && PY="$c" && break
  fi
done
if [ -z "$PY" ]; then
  osascript -e 'display alert "WB Switch" message "未找到 Python 3。请先安装：brew install python3 或从 python.org 安装。" as critical'
  exit 1
fi

# 3) start fresh instance (log to wb-switch-panel.log, survive terminal close)
nohup "$PY" "./wb_switch.py" >> "./wb-switch-panel.log" 2>&1 &

# 4) wait until it answers (max 15s)
for i in $(seq 1 15); do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:5276/; then
    break
  fi
  sleep 1
done

# 5) open browser
open http://127.0.0.1:5276
