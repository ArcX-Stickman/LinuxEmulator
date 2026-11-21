"""EXE Runner - upload a Windows .exe (or a .zip of a game folder) and run it with Wine.

The game draws onto a virtual X display (:1). x11vnc + noVNC stream that display
to your browser on port 6080, which this page embeds.
"""
import os
import re
import shutil
import signal
import subprocess
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

GAMES_DIR = Path(os.environ.get("EXE_GAMES_DIR", Path.home() / "exe-games"))
LOG_FILE = GAMES_DIR / "wine.log"
DISPLAY = os.environ.get("EXE_DISPLAY", ":1")
VNC_PORT = int(os.environ.get("EXE_NOVNC_PORT", 6080))

GAMES_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

current = {"proc": None, "exe": None}

# Game mode (mouse lock for 3D games) needs two extra libraries; the site still works without them.
VNC_PORT_RAW = int(os.environ.get("EXE_VNC_PORT", 5901))
try:
    from flask_sock import Sock
    # Ping every WebSocket every 15 s (the library default is never). Without this, Codespaces'
    # port forwarding can drop a connection that only carries data one way -> random reconnects.
    app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 15}
    SOCK = Sock(app)
    import screenproxy
    screenproxy.register(app, SOCK, VNC_PORT_RAW)   # screen on the same port as the site
    SAME_PORT_SCREEN = True
except ImportError as e:
    print(f"Screen falls back to port {VNC_PORT} ({e}). Fix: pip install flask-sock")
    SOCK, SAME_PORT_SCREEN = None, False
TURBO = False
if SOCK is not None:
    try:
        import turbo
        turbo.register(app, SOCK, DISPLAY)
        TURBO = shutil.which("ffmpeg") is not None
        import audio
        audio.register(SOCK)
        if not TURBO:
            print("Turbo page off: ffmpeg missing. Fix: sudo apt-get install -y ffmpeg")
    except ImportError as e:
        print(f"Turbo page off ({e})")
try:
    import gamemode
    gamemode.register(app, DISPLAY, SOCK)
    GAME_MODE = True
except ImportError as e:
    print(f"Game mode off ({e}). Fix: pip install flask-sock python-xlib")
    GAME_MODE = False


def list_exes():
    """Every .exe under the games folder, skipping obvious installers/uninstallers."""
    out = []
    for p in sorted(GAMES_DIR.rglob("*")):
        rel = p.relative_to(GAMES_DIR).as_posix()
        if rel.startswith("."):          # skip .incoming/ .outbox/ scratch folders
            continue
        if p.suffix.lower() == ".exe" and p.is_file():
            name = p.name.lower()
            hint = "uninstaller" if "unins" in name else ("crash handler" if "crash" in name else "")
            out.append({"path": rel, "size_mb": round(p.stat().st_size / 1e6, 1), "hint": hint})
    return out


def safe_path(rel):
    p = (GAMES_DIR / rel).resolve()
    if GAMES_DIR.resolve() not in p.parents:
        raise ValueError("bad path")
    return p


def safe_extract(zf: zipfile.ZipFile, dest: Path):
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if dest != target and dest not in target.parents:
            raise ValueError(f"unsafe path in zip: {member.filename}")
    zf.extractall(dest)


def stop_game():
    proc = current["proc"]
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # wineserver -k kills every Windows process in the prefix (games often spawn children)
    subprocess.run(["wineserver", "-k"], capture_output=True)
    current["proc"], current["exe"] = None, None


@app.route("/")
def index():
    return render_template("index.html", vnc_port=VNC_PORT, game_mode=GAME_MODE, same_port=SAME_PORT_SCREEN,
                           turbo=False, turbo_ok=TURBO)


@app.route("/turbo")
def turbo_page():
    """Beta page: same site, but the screen is an H.264 video stream instead of VNC."""
    return render_template("index.html", vnc_port=VNC_PORT, game_mode=GAME_MODE, same_port=SAME_PORT_SCREEN,
                           turbo=True, turbo_ok=TURBO)


@app.route("/api/games")
def games():
    running = current["proc"] is not None and current["proc"].poll() is None
    return jsonify(games=list_exes(), running=current["exe"] if running else None)


def install_file(tmp: Path, name: str):
    """Put a fully-uploaded file in place: .exe -> its own folder, .zip -> extracted folder."""
    ext = Path(name).suffix.lower()
    folder = GAMES_DIR / Path(name).stem
    if ext == ".exe":
        folder.mkdir(exist_ok=True)
        shutil.move(str(tmp), folder / name)
    elif ext == ".zip":
        folder.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(tmp) as zf:
                safe_extract(zf, folder)
        except (zipfile.BadZipFile, ValueError) as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError(f"Bad zip: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        # Zip had one top-level folder (MyGame/...)? Flatten so we don't get MyGame/MyGame/
        kids = list(folder.iterdir())
        if len(kids) == 1 and kids[0].is_dir():
            inner = kids[0]
            for item in list(inner.iterdir()):
                shutil.move(str(item), folder / item.name)
            inner.rmdir()
    else:
        tmp.unlink(missing_ok=True)
        raise ValueError("Upload a .exe or a .zip of the game folder")


from library import Library
LIB = Library(GAMES_DIR, install_file)


# Uploads arrive in small chunks: the Codespaces port proxy rejects big requests with 413.
INCOMING = GAMES_DIR / ".incoming"
ALLOWED = {".exe", ".zip"}


@app.route("/api/upload_chunk", methods=["POST"])
def upload_chunk():
    uid = re.sub(r"[^a-zA-Z0-9]", "", request.form.get("id", ""))[:40]
    name = secure_filename(request.form.get("name", ""))
    try:
        index, total = int(request.form["index"]), int(request.form["total"])
    except (KeyError, ValueError):
        return jsonify(error="bad chunk info"), 400
    chunk = request.files.get("chunk")
    if not uid or not name or chunk is None or not 0 <= index < total:
        return jsonify(error="bad chunk"), 400
    if Path(name).suffix.lower() not in ALLOWED:
        return jsonify(error="Upload a .exe or a .zip of the game folder"), 400
    INCOMING.mkdir(exist_ok=True)
    part, counter = INCOMING / f"{uid}.part", INCOMING / f"{uid}.next"
    expected = int(counter.read_text()) if counter.exists() else 0
    if index < expected:          # a retry of a chunk we already have
        return jsonify(ok=True)
    if index > expected:
        return jsonify(error=f"missing chunk {expected}"), 409
    with open(part, "ab") as out:
        shutil.copyfileobj(chunk.stream, out)
    counter.write_text(str(index + 1))
    if index == total - 1:
        counter.unlink()
        if request.form.get("share") == "1":
            LIB.share(part, name)            # staged copy uploads in the background
        try:
            install_file(part, name)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, done=True, games=list_exes())
    return jsonify(ok=True)


@app.route("/api/run", methods=["POST"])
def run():
    rel = (request.json or {}).get("path", "")
    try:
        exe = safe_path(rel)
    except ValueError:
        return jsonify(error="Bad path"), 400
    if not exe.is_file():
        return jsonify(error="Not found"), 404
    stop_game()
    env = dict(os.environ, DISPLAY=DISPLAY, WINEDEBUG="-all")
    log = open(LOG_FILE, "w")
    # Run from the exe's own folder: most games load data files by relative path.
    current["proc"] = subprocess.Popen(
        ["wine", exe.name], cwd=exe.parent, env=env,
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    current["exe"] = rel
    return jsonify(ok=True, running=rel)


@app.route("/api/stop", methods=["POST"])
def stop():
    stop_game()
    return jsonify(ok=True)


@app.route("/api/delete", methods=["POST"])
def delete():
    rel = (request.json or {}).get("path", "")
    try:
        top = safe_path(rel.split("/")[0])
    except ValueError:
        return jsonify(error="Bad path"), 400
    if current["exe"] and current["exe"].split("/")[0] == rel.split("/")[0]:
        stop_game()
    shutil.rmtree(top, ignore_errors=True)
    return jsonify(ok=True, games=list_exes())


# ---------- shared library (GitHub Releases) ----------
@app.route("/api/library")
def library_list():
    return jsonify(LIB.listing(refresh=request.args.get("refresh") == "1"))


@app.route("/api/library/get", methods=["POST"])
def library_get():
    try:
        name = LIB.fetch(int((request.json or {}).get("id", 0)))
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, name=name)


@app.route("/api/library/remove", methods=["POST"])
def library_remove():
    try:
        LIB.remove(int((request.json or {}).get("id", 0)))
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# ---------- Apps panel (Chrome, GeForce NOW, Minecraft, Steam) ----------
from apps import APPS, Apps
APP_MGR = Apps(DISPLAY)


@app.route("/api/apps")
def apps_list():
    return jsonify(APP_MGR.listing())


@app.route("/api/apps/<action>", methods=["POST"])
def apps_action(action):
    app_id = (request.json or {}).get("id", "")
    if app_id not in APPS:
        return jsonify(error="unknown app"), 400
    try:
        if action == "install":
            APP_MGR.install(app_id)
        elif action == "launch":
            return jsonify(ok=True, msg=APP_MGR.launch(app_id))
        elif action == "stop":
            APP_MGR.stop(app_id)
        elif action == "log":
            return jsonify(log=APP_MGR.install_log(app_id))
        else:
            return jsonify(error="bad action"), 400
    except RuntimeError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/ping")
def ping():
    return jsonify(ok=True)


@app.route("/api/log")
def log():
    if not LOG_FILE.exists():
        return jsonify(log="")
    return jsonify(log=LOG_FILE.read_text(errors="replace")[-6000:])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("EXE_UI_PORT", 8080)), threaded=True)
