#!/usr/bin/env bash
# Starts a virtual screen and streams it to the browser.
#   usage: start-display.sh <display-number> <vnc-port> <novnc-port> <WxH>
# Xvfb (fake monitor) -> fluxbox (window manager) -> x11vnc (VNC server) -> websockify/noVNC (browser viewer)
set -e
N=${1:-1}; VNC=${2:-5901}; WEB=${3:-6080}; GEOM=${4:-1280x720}
LOG=/tmp/display-$N; mkdir -p $LOG
NOVNC_DIR=/usr/share/novnc

if ! pgrep -f "Xvfb :$N " >/dev/null; then
  rm -f /tmp/.X$N-lock /tmp/.X11-unix/X$N
  Xvfb :$N -screen 0 ${GEOM}x24 -ac +extension GLX +render -noreset >$LOG/xvfb.log 2>&1 &
  sleep 1
fi
pgrep -f "fluxbox.*:$N" >/dev/null || (DISPLAY=:$N fluxbox -display :$N >$LOG/fluxbox.log 2>&1 &)
pgrep -f "x11vnc.*-rfbport $VNC" >/dev/null || \
  x11vnc -display :$N -rfbport $VNC -nopw -forever -shared -quiet -xkb -noxdamage -nocursorshape -cursorpos >$LOG/x11vnc.log 2>&1 &
pgrep -f "websockify.*$WEB" >/dev/null || \
  websockify --web $NOVNC_DIR $WEB localhost:$VNC >$LOG/websockify.log 2>&1 &
sleep 1
echo "Display :$N ready -> noVNC on port $WEB"
