"""Game mode: relative mouse + keyboard input for 3D games (Minecraft, shooters).

noVNC sends ABSOLUTE mouse positions, so in a 3D game you can only turn until your
cursor hits the edge of the browser. Game mode instead uses the browser's Pointer
Lock API (mouse hidden, unlimited movement) and sends RELATIVE movement over a
WebSocket. Here we inject it into the X display with XTest, exactly as if a real
mouse had moved.

Messages from the browser (JSON):
  {"t":"m","x":dx,"y":dy}     mouse moved by dx,dy pixels
  {"t":"d","b":1} / {"t":"u","b":1}   mouse button down/up (1 left, 2 middle, 3 right)
  {"t":"w","y":1}             wheel (+1 down, -1 up)
  {"t":"kd","k":"KeyW"} / {"t":"ku","k":"KeyW"}   key down/up (KeyboardEvent.code)
"""
import json

from Xlib import X, XK, display as xdisplay
from Xlib.ext import xtest

SPECIAL = {
    "Space": "space", "Enter": "Return", "NumpadEnter": "Return", "Escape": "Escape",
    "Tab": "Tab", "Backspace": "BackSpace", "Delete": "Delete", "Insert": "Insert",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "ShiftLeft": "Shift_L", "ShiftRight": "Shift_R", "ControlLeft": "Control_L",
    "ControlRight": "Control_R", "AltLeft": "Alt_L", "AltRight": "Alt_R",
    "MetaLeft": "Super_L", "MetaRight": "Super_R", "CapsLock": "Caps_Lock",
    "Minus": "minus", "Equal": "equal", "BracketLeft": "bracketleft",
    "BracketRight": "bracketright", "Backslash": "backslash", "Semicolon": "semicolon",
    "Quote": "apostrophe", "Comma": "comma", "Period": "period", "Slash": "slash",
    "Backquote": "grave",
}


def code_to_keysym_name(code: str):
    if code.startswith("Key") and len(code) == 4:
        return code[3].lower()
    if code.startswith("Digit") and len(code) == 6:
        return code[5]
    if code.startswith("Numpad") and code[6:].isdigit():
        return "KP_" + code[6:]
    if code.startswith("F") and code[1:].isdigit():
        return code
    return SPECIAL.get(code)


class Injector:
    def __init__(self, display_name):
        self.d = xdisplay.Display(display_name)
        self.keys_down = set()
        self.buttons_down = set()
        self._kc_cache = {}

    def keycode(self, code):
        if code not in self._kc_cache:
            name = code_to_keysym_name(code)
            ks = XK.string_to_keysym(name) if name else 0
            self._kc_cache[code] = self.d.keysym_to_keycode(ks) if ks else 0
        return self._kc_cache[code]

    def handle(self, msg):
        t = msg.get("t")
        if t == "m":
            dx, dy = int(msg.get("x", 0)), int(msg.get("y", 0))
            if dx or dy:
                # detail=True -> relative motion
                xtest.fake_input(self.d, X.MotionNotify, detail=True, x=dx, y=dy)
        elif t in ("d", "u"):
            b = int(msg.get("b", 1))
            if b in (1, 2, 3, 8, 9):
                xtest.fake_input(self.d, X.ButtonPress if t == "d" else X.ButtonRelease, b)
                (self.buttons_down.add if t == "d" else self.buttons_down.discard)(b)
        elif t == "w":
            b = 5 if msg.get("y", 0) > 0 else 4
            xtest.fake_input(self.d, X.ButtonPress, b)
            xtest.fake_input(self.d, X.ButtonRelease, b)
        elif t in ("kd", "ku"):
            kc = self.keycode(msg.get("k", ""))
            if kc:
                if t == "kd":
                    xtest.fake_input(self.d, X.KeyPress, kc)
                    self.keys_down.add(kc)
                else:
                    xtest.fake_input(self.d, X.KeyRelease, kc)
                    self.keys_down.discard(kc)
        self.d.flush()

    def release_all(self):
        """Never leave W or a mouse button stuck down when the connection drops."""
        for kc in list(self.keys_down):
            xtest.fake_input(self.d, X.KeyRelease, kc)
        for b in list(self.buttons_down):
            xtest.fake_input(self.d, X.ButtonRelease, b)
        self.keys_down.clear()
        self.buttons_down.clear()
        self.d.flush()

    def close(self):
        try:
            self.release_all()
        finally:
            self.d.close()


def register(app, display_name, sock=None):
    if sock is None:
        from flask_sock import Sock
        sock = Sock(app)

    @sock.route("/ws/input")
    def input_ws(ws):
        inj = Injector(display_name)
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                for msg in json.loads(data) if data.startswith("[") else [json.loads(data)]:
                    inj.handle(msg)
        except Exception:
            pass
        finally:
            inj.close()
