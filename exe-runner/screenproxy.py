"""Serve the game screen (noVNC) through the SAME port as the website.

Codespaces makes you sign in to every private port separately, and GitHub's sign-in
page refuses to load inside an iframe ("github.com refused to connect"). If the screen
came from its own port (6080), a fresh Codespace would show that error. So instead:
  /screen/vnc.html  -> noVNC's web files
  /screen/ws        -> WebSocket, relayed byte-for-byte to the VNC server (x11vnc)
"""
import os
import socket
import threading

from flask import send_from_directory

NOVNC_DIR = os.environ.get("NOVNC_DIR", "/usr/share/novnc")


def register(app, sock, vnc_port):
    @app.route("/screen/<path:path>")
    def novnc_files(path):
        return send_from_directory(NOVNC_DIR, path)

    @sock.route("/screen/ws")
    def vnc_ws(ws):
        try:
            tcp = socket.create_connection(("127.0.0.1", vnc_port), timeout=10)
        except OSError:
            ws.close(reason=1011, message="VNC server not running")
            return
        tcp.settimeout(None)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        alive = [True]

        def vnc_to_browser():
            try:
                while alive[0]:
                    data = tcp.recv(65536)
                    if not data:
                        break
                    ws.send(data)
            except Exception:
                pass
            finally:
                alive[0] = False
                try:
                    ws.close()
                except Exception:
                    pass

        threading.Thread(target=vnc_to_browser, daemon=True).start()
        try:
            while alive[0]:
                data = ws.receive(timeout=1)
                if data is None:
                    continue
                tcp.sendall(data if isinstance(data, bytes) else data.encode())
        except Exception:
            pass
        finally:
            alive[0] = False
            tcp.close()
