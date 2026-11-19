"""Turbo stream: real H.264 video instead of VNC screenshots.

  Codespace:  ffmpeg grabs the virtual screen (x11grab) -> x264 (ultrafast, zerolatency)
              -> split into one message per video frame -> WebSocket /turbo/ws
  Browser:    WebCodecs VideoDecoder (uses the computer's video chip when it has one)
              -> draws each frame on a <canvas>

Why it's faster than VNC: VNC re-sends changed screen rectangles as compressed images,
which for a moving 3D game is close to "a new picture every frame". A video codec only
sends what CHANGED between frames, so each frame is much smaller and quicker to make.

The encoder only runs while someone has the Turbo page open, and one encoder is shared
by every viewer.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

AUD = b"\x00\x00\x00\x01\x09"          # x264 "access unit delimiter" = start of each frame


def screen_size(display):
    try:
        from Xlib import display as xdisplay
        d = xdisplay.Display(display)
        s = d.screen()
        w, h = s.width_in_pixels, s.height_in_pixels
        d.close()
        return w - w % 2, h - h % 2       # H.264 needs even sizes
    except Exception:
        w, h = os.environ.get("SCREEN", "1280x720").split("x")
        return int(w), int(h)


class Encoder:
    """One ffmpeg process, shared. Viewers subscribe; frames are pushed to each viewer's queue."""

    def __init__(self, display):
        self.display = display
        self.viewers = set()
        self.lock = threading.Lock()
        self.proc = None
        self.settings = None
        self.size = None
        self.stats = {"frames": 0, "bytes": 0}
        self.last_key = None              # newest keyframe, so a new viewer can start instantly

    # ----- viewers -----
    def join(self, viewer, settings):
        with self.lock:
            self.viewers.add(viewer)
            if self.proc is None or self.proc.poll() is not None or settings != self.settings:
                self._restart(settings)
            elif self.last_key:
                viewer.offer(self.last_key, True)

    def leave(self, viewer):
        with self.lock:
            self.viewers.discard(viewer)
            if not self.viewers:
                self._stop()              # nobody watching -> stop using CPU

    # ----- ffmpeg -----
    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.last_key = None

    def _restart(self, settings):
        self._stop()
        self.settings = settings
        for v in self.viewers:
            v.need_key = True             # new stream: everyone waits for its first keyframe
        fps, kbps = settings["fps"], settings["kbps"]
        w, h = self.size = screen_size(self.display)
        cmd = [
            shutil.which("ffmpeg") or "ffmpeg", "-loglevel", "error", "-nostdin",
            "-f", "x11grab", "-draw_mouse", "1", "-framerate", str(fps),
            "-video_size", f"{w}x{h}", "-i", self.display,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-threads", "2",
            "-g", str(fps),                       # a keyframe every second: new viewers join fast
            "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps // 2}k",
            "-x264-params", "aud=1:repeat-headers=1:sliced-threads=1",
            "-f", "h264", "-",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        buf = b""
        out = proc.stdout
        while True:
            data = out.read(65536)
            if not data:
                break
            buf += data
            # Every frame starts with an access-unit delimiter; emit everything before the next one.
            while True:
                start = buf.find(AUD)
                nxt = buf.find(AUD, start + 5) if start >= 0 else -1
                if nxt < 0:
                    break
                self._emit(buf[start:nxt])
                buf = buf[nxt:]

    def _emit(self, frame):
        key = _is_keyframe(frame)
        if key:
            self.last_key = frame
        self.stats["frames"] += 1
        self.stats["bytes"] += len(frame)
        for v in list(self.viewers):
            v.offer(frame, key)


def _is_keyframe(frame):
    i = 0
    while True:
        i = frame.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(frame):
            return False
        if frame[i + 3] & 0x1F == 5:      # NAL type 5 = IDR slice (keyframe)
            return True
        i += 3


class Viewer:
    """Per-browser send queue. If the browser falls behind, drop frames until the next
    keyframe instead of building up a delay (a delayed game feels worse than a skipped frame)."""

    def __init__(self, ws):
        self.ws = ws
        self.q = queue.Queue(maxsize=8)
        self.need_key = True

    def offer(self, frame, key):
        if self.need_key and not key:
            return
        try:
            self.q.put_nowait((b"\x01" if key else b"\x00") + frame)
            self.need_key = False
        except queue.Full:
            self.need_key = True          # skip ahead to the next keyframe
            with self.q.mutex:
                self.q.queue.clear()

    def run(self):
        while getattr(self.ws, "connected", True):
            try:
                msg = self.q.get(timeout=1)
            except queue.Empty:
                continue                  # re-check that the browser is still there
            if msg is None:
                return
            self.ws.send(msg)


def register(app, sock, display):
    enc = Encoder(display)

    @sock.route("/turbo/ws")
    def turbo_ws(ws):
        from flask import request
        try:
            fps = max(10, min(60, int(request.args.get("fps", 30))))
            kbps = max(300, min(8000, int(request.args.get("kbps", 2500))))
        except ValueError:
            fps, kbps = 30, 2500
        viewer = Viewer(ws)
        enc.join(viewer, {"fps": fps, "kbps": kbps})
        w, h = enc.size
        ws.send(json.dumps({"t": "hello", "w": w, "h": h, "fps": fps, "kbps": kbps}))
        sender = threading.Thread(target=_safe_run, args=(viewer,), daemon=True)
        sender.start()
        try:
            # Leave as soon as the browser is gone, even if it vanished without saying goodbye
            # (closed lid, lost Wi-Fi), so the encoder stops using CPU when nobody's watching.
            while sender.is_alive() and getattr(ws, "connected", True):
                ws.receive(timeout=1)
        except Exception:
            pass
        finally:
            viewer.q.put(None)
            enc.leave(viewer)

    @app.route("/api/turbo/stats")
    def turbo_stats():
        from flask import jsonify
        running = enc.proc is not None and enc.proc.poll() is None
        return jsonify(running=running, viewers=len(enc.viewers), size=enc.size, **enc.stats)

    return enc


def _safe_run(viewer):
    try:
        viewer.run()
    except Exception:
        pass
