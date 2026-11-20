#!/usr/bin/env bash
# Kill everything both runners started.
wineserver -k 2>/dev/null
adb emu kill 2>/dev/null
pkill -f exe-runner/app.py; pkill -f apk-runner/app.py
pkill -f websockify; pkill x11vnc; pkill fluxbox; pkill Xvfb; pkill -f qemu-system
pulseaudio --kill 2>/dev/null
echo stopped
