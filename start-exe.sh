#!/usr/bin/env bash
# EXE Runner: UI on port 8080, game screen on port 6080
cd "$(dirname "$0")"
# Smaller screen = less to draw and stream = smoother. e.g.  SCREEN=960x540 bash start-exe.sh
bash scripts/start-display.sh 1 5901 6080 ${SCREEN:-1280x720}
export WINEPREFIX=${WINEPREFIX:-$HOME/.wine}
if [ ! -d "$WINEPREFIX" ]; then
  echo "First run: setting up Wine (takes ~1 minute)..."
  DISPLAY=:1 WINEDEBUG=-all wineboot -i >/tmp/wineboot.log 2>&1
fi
echo ""
echo "  EXE Runner is up. Open the PORTS tab and click the globe on port 8080."
echo ""
exec python3 exe-runner/app.py
