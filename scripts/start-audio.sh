#!/usr/bin/env bash
# Virtual sound card: a fake speaker ("game") that every app plays into.
# The site records its "monitor" (what's playing) and streams it to the Turbo page.
command -v pulseaudio >/dev/null || { echo "  (no pulseaudio: sound off. Fix: sudo apt-get install -y pulseaudio)"; exit 0; }
if ! pactl info >/dev/null 2>&1; then
  pulseaudio --daemonize=yes --exit-idle-time=-1 --log-target=file:/tmp/pulseaudio.log >/dev/null 2>&1
  for i in 1 2 3 4 5 6 7 8 9 10; do pactl info >/dev/null 2>&1 && break; sleep 0.5; done
fi
pactl list short sinks 2>/dev/null | grep -q "^[0-9]*\sgame\s" || \
  pactl load-module module-null-sink sink_name=game sink_properties=device.description=Game-Audio >/dev/null
pactl set-default-sink game
# Keep a tiny low-latency listener on the speaker at all times. PulseAudio sizes the speaker's
# buffer from whoever is listening when sound starts; without this, a game that starts making
# sound first gets a ~320 ms buffer and the streamed audio lags behind (measured).
if ! pgrep -f "client-name=gamerunner-keepalive" >/dev/null; then
  nohup parec -d game.monitor --raw --latency-msec=10 --client-name=gamerunner-keepalive >/dev/null 2>&1 &
fi
echo "Sound ready (virtual speaker 'game')"
