"""Homeserver power page.  Settings live in config.json (re-read on every request).

GET  /              the page (index.html)
GET  /api/status    {"state": offline|booting|online|shutting_down, "elapsed": s|null, "warning": str|null}
POST /api/wake      send a Wake-on-LAN packet
POST /api/shutdown  run the shutdown command over SSH
"""
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
lock = threading.Lock()
pending = None  # {"action": "wake" | "shutdown", "since": epoch seconds}


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def load_config():
    try:
        return json.loads((HERE / "config.json").read_text())
    except (OSError, ValueError) as e:
        raise ApiError(f"Cannot read config.json: {e}")


class ApiError(Exception):
    def __init__(self, message, code=500):
        super().__init__(message)
        self.code = code


def is_online(c):
    try:
        with socket.create_connection((c["status_host"], c["status_port"]), timeout=1.5):
            return True
    except OSError:
        return False


def get_status(c):
    """Combine the live port check with what we last asked the server to do."""
    global pending
    online = is_online(c)
    state, warning, elapsed = ("online" if online else "offline"), None, None
    with lock:
        if pending:
            elapsed = int(time.time() - pending["since"])
            if pending["action"] == "wake":
                if online:
                    pending = None
                elif elapsed > c["boot_timeout_s"]:
                    pending = None
                    warning = f"The server did not come online within {c['boot_timeout_s']}s. Check the PC and that Wake-on-LAN is enabled."
                else:
                    state = "booting"
            else:
                if not online:
                    pending = None
                elif elapsed > c["shutdown_timeout_s"]:
                    pending = None
                    warning = f"The server is still online {c['shutdown_timeout_s']}s after the shutdown command."
                else:
                    state = "shutting_down"
    return {"state": state, "elapsed": elapsed if state in ("booting", "shutting_down") else None, "warning": warning}


def wake(c):
    global pending
    if is_online(c):
        raise ApiError("The homeserver is already online.", 409)
    mac = "".join(ch for ch in c["mac"] if ch in "0123456789abcdefABCDEF")
    if len(mac) != 12:
        raise ApiError(f"MAC in config.json must be 12 hex digits, got {c['mac']!r}")
    packet = bytes.fromhex("FF" * 6 + mac * 16)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, (c["broadcast"], 9))
    except OSError as e:
        raise ApiError(f"Could not send the packet to {c['broadcast']}: {e}")
    with lock:
        pending = {"action": "wake", "since": time.time()}
    log("wake sent")


def shutdown(c):
    global pending
    if not is_online(c):
        raise ApiError("The homeserver is already off.", 409)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={c['ssh_timeout_s']}",
           c["ssh_target"], c["shutdown_command"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=c["ssh_timeout_s"] + 10)
    except subprocess.TimeoutExpired:
        raise ApiError("SSH timed out. The server may be busy or unreachable.")
    except FileNotFoundError:
        raise ApiError("ssh is not installed on the phone (run: pkg install openssh).")
    err = r.stderr.strip()
    # The server often drops the connection while shutting down, which ssh reports as 255.
    dropped = r.returncode == 255 and "closed by remote host" in err.lower()
    if r.returncode != 0 and not dropped:
        raise ApiError(f"SSH failed: {err or 'exit code ' + str(r.returncode)}")
    with lock:
        pending = {"action": "shutdown", "since": time.time()}
    log("shutdown sent")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # the page polls often; keep the service log quiet
        pass

    def send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, payload):
        self.send(code, json.dumps(payload).encode(), "application/json")

    def route(self, fn):
        try:
            fn()
        except ApiError as e:
            log("error:", e)
            self.send_json(e.code, {"ok": False, "error": str(e)})
        except Exception as e:
            log("unexpected error:", repr(e))
            self.send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.route(lambda: self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8"))
        elif path == "/api/status":
            self.route(lambda: self.send_json(200, get_status(load_config())))
        else:
            self.send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        actions = {"/api/wake": wake, "/api/shutdown": shutdown}
        if path not in actions:
            return self.send_json(404, {"ok": False, "error": "Not found"})
        # A custom header can't be set by another website, so this blocks cross-site triggering.
        if self.headers.get("X-Requested-With") != "wol":
            return self.send_json(403, {"ok": False, "error": "Missing X-Requested-With header"})

        def run():
            actions[path](load_config())
            self.send_json(200, {"ok": True})
        self.route(run)


if __name__ == "__main__":
    port = load_config().get("port", 8080)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
