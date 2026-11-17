"""APK Runner - upload an Android .apk, install it on an Android emulator, launch it.

The emulator window draws onto a virtual X display (:2). x11vnc + noVNC stream it
to your browser on port 6081, which this page embeds.

HARD REQUIREMENT: /dev/kvm. The Android emulator refuses to run x86_64 images
without hardware virtualization, and most cloud containers don't expose it.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APKS_DIR = Path(os.environ.get("APK_GAMES_DIR", Path.home() / "apk-games"))
SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "android-sdk"))
ADB = SDK / "platform-tools" / "adb"
LOG_FILE = APKS_DIR / "apk.log"
VNC_PORT = int(os.environ.get("APK_NOVNC_PORT", 6081))

APKS_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024

state = {"package": None}


def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg.rstrip() + "\n")


def sh(args, timeout=300):
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    log(f"$ {' '.join(map(str, args))}\n{out}")
    return r.returncode, out


def aapt2():
    tools = sorted((SDK / "build-tools").glob("*/aapt2"))
    return tools[-1] if tools else None


def package_name(apk: Path):
    tool = aapt2()
    if not tool:
        return None
    code, out = sh([tool, "dump", "badging", apk], timeout=60)
    m = re.search(r"package: name='([^']+)'", out)
    return m.group(1) if m else None


def native_abis(apk: Path):
    tool = aapt2()
    if not tool:
        return []
    _, out = sh([tool, "dump", "badging", apk], timeout=60)
    m = re.search(r"native-code: (.+)", out)
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


def status():
    kvm = os.path.exists("/dev/kvm")
    kvm_ok = kvm and os.access("/dev/kvm", os.R_OK | os.W_OK)
    sdk = ADB.exists() and (SDK / "emulator" / "emulator").exists()
    device = booted = False
    if sdk:
        r = subprocess.run([ADB, "devices"], capture_output=True, text=True)
        device = "emulator-" in r.stdout and "\tdevice" in r.stdout
        if device:
            r = subprocess.run([ADB, "shell", "getprop", "sys.boot_completed"], capture_output=True, text=True)
            booted = r.stdout.strip() == "1"
    return {"kvm": kvm, "kvm_ok": kvm_ok, "sdk": sdk, "device": device, "booted": booted,
            "running": state["package"]}


@app.route("/")
def index():
    return render_template("index.html", vnc_port=VNC_PORT)


@app.route("/api/status")
def api_status():
    apks = [{"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1)}
            for p in sorted(APKS_DIR.glob("*.apk"))]
    return jsonify(status=status(), apks=apks)


# Uploads arrive in small chunks: the Codespaces port proxy rejects big requests with 413.
INCOMING = APKS_DIR / ".incoming"


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
    if not name.lower().endswith(".apk"):
        return jsonify(error="Only .apk files. (.xapk/.apks bundles are split APKs - see README)"), 400
    INCOMING.mkdir(exist_ok=True)
    part, counter = INCOMING / f"{uid}.part", INCOMING / f"{uid}.next"
    expected = int(counter.read_text()) if counter.exists() else 0
    if index < expected:
        return jsonify(ok=True)
    if index > expected:
        return jsonify(error=f"missing chunk {expected}"), 409
    with open(part, "ab") as out:
        shutil.copyfileobj(chunk.stream, out)
    counter.write_text(str(index + 1))
    if index == total - 1:
        counter.unlink()
        shutil.move(str(part), APKS_DIR / name)
        return jsonify(ok=True, done=True)
    return jsonify(ok=True)


@app.route("/api/boot", methods=["POST"])
def boot():
    s = status()
    if not s["kvm"]:
        return jsonify(error="No /dev/kvm on this machine - the Android emulator can't run here. See README."), 400
    if not s["sdk"]:
        return jsonify(error="Android SDK not installed. Run: bash scripts/setup-android.sh"), 400
    if s["device"]:
        return jsonify(ok=True, msg="Emulator already running")
    subprocess.Popen(["bash", str(ROOT / "scripts" / "start-emulator.sh")],
                     stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT, start_new_session=True)
    return jsonify(ok=True, msg="Booting... (first boot can take 2-5 minutes)")


@app.route("/api/run", methods=["POST"])
def run():
    name = secure_filename((request.json or {}).get("name", ""))
    apk = APKS_DIR / name
    if not apk.is_file():
        return jsonify(error="Not found"), 404
    if not status()["booted"]:
        return jsonify(error="Emulator isn't booted yet. Press 'Boot phone' and wait."), 400
    pkg = package_name(apk)
    abis = native_abis(apk)
    code, out = sh([ADB, "install", "-r", "-g", apk], timeout=600)
    if code != 0 or "Success" not in out:
        hint = ""
        if "NO_MATCHING_ABIS" in out:
            hint = f" This APK only ships native code for {abis}; the emulator couldn't translate it."
        return jsonify(error="Install failed: " + out[-400:] + hint), 400
    if not pkg:
        return jsonify(error="Installed, but couldn't read the package name to launch it."), 400
    sh([ADB, "shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"])
    state["package"] = pkg
    return jsonify(ok=True, package=pkg, abis=abis)


@app.route("/api/stop", methods=["POST"])
def stop():
    if state["package"]:
        sh([ADB, "shell", "am", "force-stop", state["package"]])
    state["package"] = None
    return jsonify(ok=True)


@app.route("/api/key", methods=["POST"])
def key():
    """Phone hardware buttons: back / home / recents."""
    codes = {"back": 4, "home": 3, "recents": 187}
    k = codes.get((request.json or {}).get("key"))
    if k is None:
        return jsonify(error="bad key"), 400
    subprocess.run([ADB, "shell", "input", "keyevent", str(k)], capture_output=True)
    return jsonify(ok=True)


@app.route("/api/delete", methods=["POST"])
def delete():
    name = secure_filename((request.json or {}).get("name", ""))
    p = APKS_DIR / name
    if p.is_file():
        p.unlink()
    return jsonify(ok=True)


@app.route("/api/log")
def api_log():
    return jsonify(log=LOG_FILE.read_text(errors="replace")[-6000:] if LOG_FILE.exists() else "")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("APK_UI_PORT", 8081)), threaded=True)
