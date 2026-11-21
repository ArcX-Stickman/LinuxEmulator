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
import socket
import shutil
import subprocess
import threading
import time



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


# Encoder threads: on a 2-core Codespace a second thread mostly steals time from the game, but
# one thread can't keep up above 540p (measured: input-delay spikes). So: 1 thread only for small
# streams on small machines.
CORES = os.cpu_count() or 2


def enc_threads(out_h):
    return 1 if CORES <= 2 and out_h <= 540 else 2


class Encoder:
    """One ffmpeg process, shared. Viewers subscribe; frames are pushed to each viewer's queue."""

    def __init__(self, display):
        self.display = display
        self.viewers = set()
        self.lock = threading.Lock()
        self.proc = None
        self.settings = None
        self.size = None
        self.screen = None
        self.stats = {"frames": 0, "bytes": 0}
        self.last_key = None              # newest keyframe, so a new viewer can start instantly

    # ----- viewers -----
    def join(self, viewer, settings):
        with self.lock:
            self.viewers.add(viewer)
            running = self.proc is not None and self.proc.poll() is None
            others = len(self.viewers) > 1
            # Restart only if nobody else is watching. Otherwise two tabs with different
            # settings would keep restarting the stream on each other (reconnect loop).
            if not running or (settings != self.settings and not others):
                self._restart(settings)
            elif self.last_key:
                viewer.offer(self.last_key, True)

    def leave(self, viewer):
        with self.lock:
            self.viewers.discard(viewer)
            if not self.viewers:
                self._stop()              # nobody watching -> stop using CPU

    # ----- game FPS meter -----
    def _start_meter(self):
        if getattr(self, "_meter_on", False):
            return
        self._meter_on = True
        threading.Thread(target=self._meter, daemon=True).start()

    def _meter(self):
        """Count screen redraws per second using the X DAMAGE extension. That's the game's real
        frame rate (anything that draws counts), which tells you if the GAME or the STREAM is slow."""
        try:
            from Xlib import display as xdisplay
            from Xlib.ext import damage
            d = xdisplay.Display(self.display)
            d.damage_query_version()
            dmg = d.screen().root.damage_create(damage.DamageReportNonEmpty)
        except Exception:
            self._meter_on = False
            return
        count, t0 = 0, time.time()
        while self.viewers:
            while d.pending_events():
                if d.next_event().type == d.extension_event.DamageNotify:
                    count += 1
                    d.damage_subtract(dmg)   # re-arm for the next redraw
            d.flush()
            now = time.time()
            if now - t0 >= 1:
                msg = json.dumps({"t": "stats", "game_fps": round(count / (now - t0))})
                for v in list(self.viewers):
                    v.info(msg)
                count, t0 = 0, now
            time.sleep(0.01)
        d.close()
        self._meter_on = False

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
        w, h = self.screen = screen_size(self.display)
        ow, oh = settings.get("w") or w, settings.get("h") or h
        if ow >= w or oh >= h:
            ow, oh = w, h                         # never stream bigger than the real screen
        self.size = (ow, oh)
        scale = ["-vf", f"scale={ow}:{oh}:flags=fast_bilinear"] if (ow, oh) != (w, h) else []
        cmd = [
            shutil.which("ffmpeg") or "ffmpeg", "-loglevel", "error", "-nostdin",
            "-f", "x11grab", "-draw_mouse", "1", "-framerate", str(fps),
            "-video_size", f"{w}x{h}", "-i", self.display,
            *scale,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-threads", str(enc_threads(oh)),
            "-g", str(fps),                       # a keyframe every second: new viewers join fast
            "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps // 2}k",
            "-x264-params", "sliced-threads=1" if enc_threads(oh) > 1 else "sliced-threads=0",
            # FLV labels every frame with its exact size, so we know the instant a frame is complete
            "-flush_packets", "1", "-flvflags", "no_duration_filesize", "-f", "flv", "-",
        ]
        # nice 10: when the CPU is maxed out, the game gets served before the encoder
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                     preexec_fn=lambda: os.nice(10))
        self._start_meter()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        """Read ffmpeg's FLV output tag by tag. Each video tag is exactly one frame, and its size
        is written up front, so a frame is sent the moment its last byte arrives: no waiting for
        the next frame, no guessing. Frames are converted to the plain H.264 format the browser's
        decoder expects, with the stream's settings (SPS/PPS) attached to every keyframe."""
        f = proc.stdout

        def readn(n):
            b = b""
            while len(b) < n:
                chunk = f.read(n - len(b))
                if not chunk:
                    raise EOFError
                b += chunk
            return b

        headers, nal_len = b"", 4
        try:
            readn(9 + 4)                              # FLV file header + first "previous tag size"
            while True:
                tag = readn(11)
                size = int.from_bytes(tag[1:4], "big")
                data = readn(size)
                readn(4)                              # trailing "previous tag size"
                if tag[0] & 0x1F != 9 or size < 5 or data[0] & 0x0F != 7:
                    continue                          # not an H.264 video tag (e.g. metadata)
                body = data[5:]
                if data[1] == 0:                      # stream settings (avcC record)
                    headers, nal_len = _avcc_to_annexb(body)
                elif data[1] == 1:                    # a frame
                    key = (data[0] >> 4) == 1
                    frame = _nals_to_annexb(body, nal_len)
                    self._emit(headers + frame if key else frame, key)
        except EOFError:
            pass

    def _emit(self, frame, key):
        if key:
            self.last_key = frame
        self.stats["frames"] += 1
        self.stats["bytes"] += len(frame)
        for v in list(self.viewers):
            v.offer(frame, key)


START = b"\x00\x00\x00\x01"


def _avcc_to_annexb(rec):
    """avcC record -> (SPS+PPS with start codes, NAL length-field size)."""
    nal_len = (rec[4] & 0x03) + 1
    out, i = b"", 6
    for _ in range(rec[5] & 0x1F):                    # SPS list
        n = int.from_bytes(rec[i:i + 2], "big"); out += START + rec[i + 2:i + 2 + n]; i += 2 + n
    count = rec[i]; i += 1
    for _ in range(count):                            # PPS list
        n = int.from_bytes(rec[i:i + 2], "big"); out += START + rec[i + 2:i + 2 + n]; i += 2 + n
    return out, nal_len


def _nals_to_annexb(body, nal_len):
    """Length-prefixed NAL units -> start-code prefixed (Annex B)."""
    out, i = [], 0
    while i + nal_len <= len(body):
        n = int.from_bytes(body[i:i + nal_len], "big"); i += nal_len
        out.append(START + body[i:i + n]); i += n
    return b"".join(out)


class Viewer:
    """Per-browser send queue. If the browser falls behind, drop frames until the next
    keyframe instead of building up a delay (a delayed game feels worse than a skipped frame)."""

    def __init__(self, ws):
        self.ws = ws
        self.q = queue.Queue(maxsize=16)          # ~0.5 s at 30 FPS before we skip ahead
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

    def info(self, text):
        try:
            self.q.put_nowait(text)
        except queue.Full:
            pass

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
        _no_delay(ws)
        viewer = Viewer(ws)
        try:
            ow = int(request.args.get("w", 0)); ow -= ow % 2
            oh = int(request.args.get("h", 0)); oh -= oh % 2
        except ValueError:
            ow = oh = 0
        enc.join(viewer, {"fps": fps, "kbps": kbps, "w": ow, "h": oh})
        w, h = enc.size
        sw, sh = enc.screen
        # w,h = video size; sw,sh = real screen size (mouse positions are in screen pixels)
        cur = enc.settings
        ws.send(json.dumps({"t": "hello", "w": w, "h": h, "sw": sw, "sh": sh, "fps": cur["fps"], "kbps": cur["kbps"]}))
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


def _no_delay(ws):
    """Turn off Nagle's algorithm: send small packets right away instead of batching them."""
    sock = getattr(ws, "sock", None)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
