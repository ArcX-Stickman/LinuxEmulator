"""Apps panel: one-click install + launch for Chrome, GeForce NOW, Minecraft and Steam.

Everything opens on the same virtual screen as the Windows games (DISPLAY :1),
so it shows up in the site's game screen.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path("/tmp/app-installs")
LOG_DIR.mkdir(exist_ok=True)

APPS = {
    "geforcenow": {"name": "GeForce NOW", "icon": "🟩", "install": "chrome",
                   "note": "Cloud gaming in Chrome. Streams a stream, so expect lag."},
    "chrome":     {"name": "Chrome", "icon": "🌐", "install": "chrome",
                   "note": "Web browser on the game screen."},
    "minecraft":  {"name": "Minecraft", "icon": "⛏", "install": "minecraft",
                   "note": "Official launcher. Use Game mode to look around."},
    "steam":      {"name": "Steam", "icon": "🎮", "install": "steam",
                   "note": "May not start in Codespaces; games will be slow without a GPU."},
}


def _first(*paths):
    for p in paths:
        p = os.path.expanduser(p)
        found = shutil.which(p) or (p if os.path.isfile(p) and os.access(p, os.X_OK) else None)
        if found:
            return found
    return None


def binary(app_id):
    if app_id in ("chrome", "geforcenow"):
        return _first("google-chrome", "google-chrome-stable")
    if app_id == "minecraft":
        return _first("minecraft-launcher", "~/.minecraft/launcher/minecraft-launcher",
                      "~/minecraft-launcher/minecraft-launcher")
    if app_id == "steam":
        return _first("/usr/games/steam", "steam")
    return None


def _chrome_ua(chrome):
    """GeForce NOW officially supports Chrome on ChromeOS/Windows/Mac, not Linux.
    Present as ChromeOS with the real Chrome version so it doesn't refuse to load."""
    try:
        ver = re.search(r"(\d+)\.", subprocess.run([chrome, "--version"], capture_output=True,
                                                   text=True, timeout=10).stdout).group(1)
    except Exception:
        ver = "130"
    return (f"Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36")


def command(app_id, no_sandbox=False):
    exe = binary(app_id)
    if not exe:
        return None
    if app_id in ("chrome", "geforcenow"):
        cmd = [exe, "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
               "--start-maximized", f"--user-data-dir={HOME / '.chrome-gamerunner'}"]
        if no_sandbox:
            cmd.append("--no-sandbox")
        if app_id == "geforcenow":
            cmd += [f"--user-agent={_chrome_ua(exe)}", "--app=https://play.geforcenow.com"]
        else:
            cmd.append("https://www.google.com")
        return cmd
    return [exe]


class Apps:
    def __init__(self, display):
        self.display = display
        self.procs = {}          # app_id -> Popen
        self.notes = {}          # app_id -> status line for the page
        self.installing = None   # install key currently running (apt can only do one at a time)
        self.lock = threading.Lock()

    def listing(self):
        out = []
        for app_id, a in APPS.items():
            p = self.procs.get(app_id)
            out.append({"id": app_id, **a, "installed": binary(app_id) is not None,
                        "running": bool(p and p.poll() is None),
                        "installing": self.installing == a["install"],
                        "status": self.notes.get(app_id, "")})
        return {"apps": out, "installing": self.installing}

    # ---------- install ----------
    def install(self, app_id):
        key = APPS[app_id]["install"]
        with self.lock:
            if self.installing:
                raise RuntimeError(f"Wait: already installing {self.installing}")
            self.installing = key
        log = LOG_DIR / f"{key}.log"
        for i, a in APPS.items():
            if a["install"] == key:
                self.notes[i] = "⏳ installing… (1–5 min)"
        threading.Thread(target=self._install, args=(key, log), daemon=True).start()

    def _install(self, key, log):
        try:
            with open(log, "w") as f:
                rc = subprocess.run(["bash", str(ROOT / "scripts" / "install-apps.sh"), key],
                                    stdout=f, stderr=subprocess.STDOUT).returncode
            msg = "✅ installed" if rc == 0 else f"⚠ install failed. Log: {log}"
        except Exception as e:
            msg = f"⚠ install failed: {e}"
        for i, a in APPS.items():
            if a["install"] == key:
                self.notes[i] = msg
        self.installing = None

    def install_log(self, app_id):
        log = LOG_DIR / f"{APPS[app_id]['install']}.log"
        return log.read_text(errors="replace")[-4000:] if log.exists() else ""

    # ---------- launch / stop ----------
    def launch(self, app_id):
        if app_id in self.procs and self.procs[app_id].poll() is None:
            return "already running"
        cmd = command(app_id)
        if not cmd:
            raise RuntimeError("Not installed yet: press Install first")
        env = dict(os.environ, DISPLAY=self.display)
        log = open(LOG_DIR / f"run-{app_id}.log", "w")
        p = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        if app_id in ("chrome", "geforcenow"):
            # Chrome's sandbox can be blocked inside containers. If it dies right away, retry without it.
            time.sleep(3)
            if p.poll() not in (None, 0):   # exit code 0 = handed off to an already-open Chrome
                p = subprocess.Popen(command(app_id, no_sandbox=True), env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
                self.notes[app_id] = "▶ running (sandbox off: Codespace blocked it)"
            else:
                self.notes[app_id] = "▶ running"
        else:
            self.notes[app_id] = "▶ starting… (first launch can take a few minutes)"
        self.procs[app_id] = p
        return "launched"

    def stop(self, app_id):
        p = self.procs.get(app_id)
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, 15)
            except ProcessLookupError:
                pass
        # Minecraft and Steam spawn child processes outside our process group
        patterns = {"minecraft": "minecraft", "steam": "steam"}
        if app_id in patterns:
            subprocess.run(["pkill", "-f", patterns[app_id]], capture_output=True)
        self.notes[app_id] = "stopped"
