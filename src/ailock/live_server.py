"""Local server for the /live brief (`ailock live-serve`, started on demand by `ailock live`).

Serves data/live/brief.html on http://localhost:8765 and pushes two kinds of server-sent events to open pages:
`status` (data/live/status.json: where the pipeline is, written by `ailock live` and `ailock live-page`) and `page`
(a revision that changes whenever brief.html is rewritten; the page then swaps itself in place, keeping scroll
position and the selected build tab). Stdlib only; exits after a few idle hours.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import live_page, paths

PORT = int(os.environ.get("AILOCK_LIVE_PORT", 8765))
URL = f"http://localhost:{PORT}/"
IDLE_EXIT_S = 4 * 3600


def _mtime(p) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


class State:
    clients = 0
    last_seen = time.time()
    lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        State.last_seen = time.time()
        route = self.path.split("?")[0]
        if route in ("/", "/brief.html"):
            try:
                body = live_page.PAGE.read_bytes()
            except OSError:
                body = live_page.placeholder().encode()
            self._send(body, "text/html; charset=utf-8")
        elif route == "/status":
            self._send(json.dumps({**_status(), "clients": State.clients, "data": str(paths.DATA)}).encode(),
                       "application/json")
        elif route == "/events":
            self._events()
        elif route == "/quit":  # ensure_running() replaces a server that serves another data directory
            self._send(b"bye", "text/plain")
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)), daemon=True).start()
        else:
            self._send(b"not found", "text/plain", 404)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with State.lock:
            State.clients += 1
        seen_status = seen_page = None
        ping = time.time()
        try:
            while True:
                st, pg = _mtime(live_page.STATUS), _mtime(live_page.PAGE)
                out = ""
                if st != seen_status:
                    seen_status = st
                    out += f"event: status\ndata: {json.dumps(_status())}\n\n"
                if pg != seen_page:
                    seen_page = pg
                    out += f"event: page\ndata: {pg}\n\n"
                if not out and time.time() - ping > 15:
                    out = ": ping\n\n"
                if out:
                    self.wfile.write(out.encode())
                    self.wfile.flush()
                    ping = time.time()
                State.last_seen = time.time()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with State.lock:
                State.clients -= 1


def _status() -> dict:
    try:
        return json.loads(live_page.STATUS.read_text())
    except (OSError, ValueError):
        return {"stage": "idle", "message": "waiting for ailock live", "started": time.time(), "t": time.time()}


def serve(host: str = "127.0.0.1", port: int = PORT) -> None:
    live_page.LIVE_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"serving {live_page.PAGE} on http://localhost:{port}/", flush=True)
    while State.clients or time.time() - State.last_seen < IDLE_EXIT_S:
        time.sleep(30)
    httpd.shutdown()


def running() -> dict | None:
    try:
        with urllib.request.urlopen(URL + "status", timeout=0.5) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def ensure_running() -> dict | None:
    """The server's status, starting it in the background first if needed (replacing one that serves another data
    directory). None when it can't be reached."""
    s = running()
    if s and s.get("data") != str(paths.DATA):
        try:
            urllib.request.urlopen(URL + "quit", timeout=1).read()
        except OSError:
            pass
        for _ in range(20):
            time.sleep(0.1)
            if running() is None:
                break
        s = running()
    if s is None:
        live_page.LIVE_DIR.mkdir(parents=True, exist_ok=True)
        with open(live_page.LIVE_DIR / "server.log", "ab") as log:
            subprocess.Popen([sys.executable, "-m", "ailock.cli", "live-serve"], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            time.sleep(0.1)
            if s := running():
                break
    return s if s and s.get("data") == str(paths.DATA) else None


def show(open_it: bool = True) -> str:
    """Make sure the brief is being served and open it unless a tab already watches it; falls back to the file."""
    s = ensure_running()
    if s:
        if open_it and not s.get("clients"):
            live_page.open_page(URL)
        return URL
    if open_it and live_page.PAGE.exists():
        live_page.open_page(str(live_page.PAGE))
    return str(live_page.PAGE)
