"""Game audio for the Turbo page.

  Codespace: apps play into a virtual speaker ("game", see scripts/start-audio.sh)
             -> parec records it -> ffmpeg -> Opus (low-delay mode, 20 ms packets)
             -> one WebSocket message per packet on /turbo/audio
  Browser:   WebCodecs AudioDecoder -> Web Audio, with a small buffer to absorb Wi-Fi jitter

ffmpeg wraps Opus in Ogg "pages"; we flush a page per packet and unpack them here, because the
browser's decoder wants bare Opus packets. Like the video encoder, this only runs while someone
has sound turned on.
"""
import fcntl
import os
import select
import shutil
import subprocess
import threading

from turbo import _no_delay

SINK_MONITOR = "game.monitor"


class AudioStream:
    def __init__(self):
        self.listeners = set()
        self.lock = threading.Lock()
        self.proc = None

    def join(self, q):
        with self.lock:
            self.listeners.add(q)
            if self.proc is None or self.proc.poll() is not None:
                self._start()

    def leave(self, q):
        with self.lock:
            self.listeners.discard(q)
            if not self.listeners and self.proc and self.proc.poll() is None:
                self.proc.terminate()
                self.rec.terminate()
                self.proc = None

    def _start(self):
        """parec records the virtual speaker with ~4 ms delay (ffmpeg's own recorder added ~330 ms
        whenever sound was already playing). Python sits in between so no backlog can build up."""
        self.rec = subprocess.Popen(["parec", "-d", SINK_MONITOR, "--raw", "--rate=48000", "--channels=2",
                                     "--format=s16le", "--latency-msec=10", "--process-time-msec=5"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        cmd = [shutil.which("ffmpeg") or "ffmpeg", "-loglevel", "error", "-nostdin",
               "-probesize", "32", "-analyzeduration", "0",
               "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
               "-c:a", "libopus", "-application", "lowdelay", "-frame_duration", "20", "-b:a", "96k",
               "-page_duration", "20000",            # one Ogg page per 20 ms packet = no batching delay
               "-flush_packets", "1", "-f", "ogg", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0,
                                     preexec_fn=lambda: os.nice(5))
        try:
            fcntl.fcntl(self.proc.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)   # ~21 ms max in this pipe
        except (OSError, AttributeError):
            pass
        threading.Thread(target=self._feed, args=(self.rec, self.proc), daemon=True).start()
        threading.Thread(target=_resync_sink, daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _feed(self, rec, enc):
        """Copy sound from parec to the encoder WITHOUT EVER WAITING. If parec has to wait even
        once (e.g. while ffmpeg starts up), PulseAudio buffers the recording and that delay stays
        for the whole session (measured: +320 ms). So: if more than 60 ms piled up, keep only the
        newest 20 ms, and if the encoder isn't ready to take it, drop it."""
        fd = rec.stdout.fileno()
        out = enc.stdin.fileno()
        os.set_blocking(out, False)
        KEEP, LIMIT, CHUNK = 3840, 11520, 3840     # 20 ms / 60 ms / 20 ms of 48 kHz stereo 16-bit
        try:
            while True:
                select.select([fd], [], [])
                data = os.read(fd, 1 << 20)
                if not data:
                    break
                if len(data) > LIMIT:
                    data = data[-KEEP:]
                data = data[:len(data) - len(data) % 4]   # whole samples only
                for i in range(0, len(data), CHUNK):     # <= 4096 bytes: each write is all-or-nothing
                    try:
                        os.write(out, data[i:i + CHUNK])
                    except BlockingIOError:
                        break                             # encoder busy: drop the rest
        except (OSError, ValueError):
            pass

    def _pump(self, proc):
        """Unpack Ogg pages into Opus packets and hand each one to every listener."""
        f = proc.stdout

        def readn(n):
            b = b""
            while len(b) < n:
                c = f.read(n - len(b))
                if not c:
                    raise EOFError
                b += c
            return b

        partial, skip = b"", 2                    # first two packets are headers (OpusHead, OpusTags)
        try:
            while True:
                hdr = readn(27)
                if hdr[:4] != b"OggS":
                    return
                segs = readn(hdr[26])
                data = readn(sum(segs))
                i = 0
                for n in segs:
                    partial += data[i:i + n]
                    i += n
                    if n < 255:                   # a segment under 255 bytes ends a packet
                        if skip:
                            skip -= 1
                        elif partial:
                            for q in list(self.listeners):
                                q(partial)
                        partial = b""
        except EOFError:
            pass


def _resync_sink():
    """The virtual speaker picks its buffer size when it starts playing. If a game was already
    playing before we attached, it keeps a ~320 ms buffer (measured), and the whole stream lags.
    Suspending and resuming it makes it re-pick, now honouring our 10 ms recorder: ~5 ms."""
    import time
    for _ in range(40):                            # wait (up to 2 s) until parec is attached
        out = subprocess.run(["pactl", "list", "short", "source-outputs"], capture_output=True, text=True).stdout
        if len([l for l in out.splitlines() if l.strip()]) >= 2:   # keepalive + ours
            break
        time.sleep(0.05)
    for attempt in range(2):                       # second pass is a backstop for slow starts
        for state in ("1", "0"):
            subprocess.run(["pactl", "suspend-sink", "game", state], capture_output=True)
        time.sleep(1.0)


def register(sock):
    import queue as _queue
    stream = AudioStream()

    @sock.route("/turbo/audio")
    def audio_ws(ws):
        _no_delay(ws)
        q = _queue.Queue()
        MAX_BACKLOG = 3     # 60 ms. A queue that never empties is a permanent delay, so if packets
                            # pile up (start-up burst, slow Wi-Fi), drop the OLDEST: stale sound is useless.

        def offer(pkt):
            while q.qsize() >= MAX_BACKLOG:
                try:
                    q.get_nowait()
                except _queue.Empty:
                    break
            q.put_nowait(pkt)

        stream.join(offer)
        try:
            while getattr(ws, "connected", True):
                try:
                    pkt = q.get(timeout=1)
                except _queue.Empty:
                    continue
                ws.send(pkt)
        except Exception:
            pass
        finally:
            stream.leave(offer)

    return stream
