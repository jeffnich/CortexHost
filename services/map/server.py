#!/usr/bin/env python3
"""
Cortex brain-map service: hosts the constellation at a private URL and
regenerates it on a schedule so it stays fresh as memories flow in.

The always-on process is a thin HTTP server (~40MB). All heavy work (Qdrant
pull, UMAP, clustering, classify, optional RETIRE_SOURCES) runs in
regen_child.py via subprocess so its RAM is returned to the OS between
refreshes; holding numpy/UMAP memory 24/7 was the RAM bill.

Routes:
  /<DEMO_SECRET>/demo.html   -> redacted demo (open): personal memories grayed
                                + text-stripped. Safe to share.
  /<DEMO_SECRET>/login.html  -> styled gate; POST with MAP_PASSWORD redirects
                                to the private map.
  /<MAP_SECRET>/map.html     -> full private map (secret revealed post-login).
  /how.html under either secret -> the how-it-works explainer.

Env: MAP_PATH_SECRET, DEMO_PATH_SECRET, MAP_PASSWORD (login gate; unset =
login disabled), MAP_TOPICS=50, MAP_REFRESH_HOURS=6, MAP_SAMPLE=0, PORT,
RETIRE_SOURCES (one-shot permanent delete, run by the child), plus the
Qdrant vars read by mapgen.
"""

import hashlib
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

SECRET = os.environ["MAP_PATH_SECRET"]
DEMO_SECRET = os.environ.get("DEMO_PATH_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))
REFRESH_H = float(os.environ.get("MAP_REFRESH_HOURS", "6"))
PASSWORD = os.environ.get("MAP_PASSWORD", "")
AUTH_TOKEN = hashlib.sha256(("cortex:" + PASSWORD).encode()).hexdigest() if PASSWORD else ""
OUT = "/tmp/map.html"
DEMO_OUT = "/tmp/demo.html"
CHILD = str(Path(__file__).resolve().parent / "regen_child.py")
try:
    HOW_HTML = (Path(__file__).resolve().parent / "how.html").read_text()
except Exception:
    HOW_HTML = ""

HTML = None
DEMO_HTML = None
LAST = None


def regenerate():
    """Run the heavy pipeline in a child process; read its output files."""
    global HTML, DEMO_HTML, LAST
    try:
        r = subprocess.run([sys.executable, CHILD], timeout=5400)
        if r.returncode != 0:
            print(f"regen child exited {r.returncode}", flush=True)
            return
        HTML = Path(OUT).read_text()
        DEMO_HTML = Path(DEMO_OUT).read_text()
        LAST = time.time()
    except Exception as e:
        print(f"regen error: {type(e).__name__}: {e}", flush=True)


def loop():
    while True:
        regenerate()
        time.sleep(max(0.25, REFRESH_H) * 3600)


WARMING = (b"<!doctype html><body style='margin:0;height:100vh;display:flex;align-items:center;"
           b"justify-content:center;background:#07070d;color:#ff8a5c;font-family:system-ui'>"
           b"<div style='text-align:center'><h2>CortexHost</h2>"
           b"<p style='color:#8a8aa2'>Warming up the constellation\xe2\x80\xa6 refresh in ~2 minutes.</p></div></body>")

_LOGIN_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CortexHost · full access</title>
<style>
 :root{--bg:#07070d;--accent:#ff8a5c;--accent2:#7c5cff;--cyan:#5cc8ff;--txt:#eef;--mut:#8a8aa2}
 *{box-sizing:border-box} html,body{margin:0;height:100%}
 body{background:radial-gradient(1200px 800px at 50% -10%,#15102a 0%,var(--bg) 60%);color:var(--txt);
   font:15px/1.6 -apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
 .wrap{max-width:460px;width:100%;text-align:center;background:rgba(12,12,22,0.55);backdrop-filter:blur(18px) saturate(1.2);
   border:1px solid #23233a;border-radius:20px;padding:42px 34px;box-shadow:0 30px 80px rgba(0,0,0,.5)}
 .brain{margin-bottom:10px}
 .brain svg{width:52px;height:52px;stroke:url(#g) #b28bff;filter:drop-shadow(0 0 18px rgba(124,92,255,.6))}
 h1{font-size:22px;margin:8px 0 6px;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
 p{color:var(--mut);font-size:14px;margin:0 0 22px}
 form{display:flex;gap:10px;flex-direction:column}
 input{width:100%;padding:13px 15px;border-radius:12px;border:1px solid #2c2c44;background:#0b0b14;color:var(--txt);font-size:15px;outline:none}
 input:focus{border-color:var(--accent2);box-shadow:0 0 0 3px rgba(124,92,255,.18)}
 button{width:100%;padding:13px;border:0;border-radius:12px;cursor:pointer;font-size:15px;font-weight:600;color:#fff;
   background:linear-gradient(135deg,var(--accent),var(--accent2));transition:transform .08s ease,filter .15s ease}
 button:hover{filter:brightness(1.08)} button:active{transform:translateY(1px)}
 .err{min-height:20px;color:#ff7a7a;font-size:13px;margin-top:12px}
 .back{display:inline-block;margin-top:18px;color:var(--cyan);text-decoration:none;font-size:13px;opacity:.85}
 .back:hover{opacity:1}
</style></head><body>
 <div class="wrap">
  <div class="brain"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#b28bff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.599 6.5a3 3 0 0 0 .399-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/></svg></div>
  <h1>You've reached the edge of a mind.</h1>
  <p>Past here is the full Cortex: the private constellation — personal thoughts,
     raw captures, the unfiltered stream. The demo you came from keeps all of that grayed out.
     Enter the passphrase to dive in.</p>
  <form method="POST" action="__ACTION__" autocomplete="off">
   <input type="password" name="password" placeholder="passphrase" autofocus autocomplete="current-password">
   <button type="submit">dive into the brain →</button>
  </form>
  <div class="err">__ERR__</div>
  <a class="back" href="demo.html">← back to the demo</a>
 </div>
</body></html>"""


def login_page(err=""):
    base = DEMO_SECRET or SECRET
    return (_LOGIN_TMPL
            .replace("__ACTION__", f"/{base}/login")
            .replace("__ERR__", err))


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, code=200, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not PASSWORD:
            return True  # no gate configured -> full map stays open
        return ("cx_auth=" + AUTH_TOKEN) in self.headers.get("Cookie", "")

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        seg = path.strip("/").split("/")
        secret = seg[0] if seg else ""
        page = seg[1] if len(seg) > 1 else ""
        if secret == SECRET and page in ("map.html", ""):
            if not self._authed():
                self._send(login_page())
            else:
                self._send(HTML if HTML else WARMING, 200 if HTML else 503)
        elif DEMO_SECRET and secret == DEMO_SECRET and page in ("demo.html", ""):
            self._send(DEMO_HTML if DEMO_HTML else WARMING, 200 if DEMO_HTML else 503)
        elif DEMO_SECRET and secret == DEMO_SECRET and page in ("login", "login.html"):
            self._send(login_page())
        elif secret in (SECRET, DEMO_SECRET) and page == "how.html" and HOW_HTML:
            self._send(HOW_HTML)
        elif secret == SECRET and page == "demo.html":
            # legacy demo path under the map secret -> gone; demo lives on its own secret
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if DEMO_SECRET and path == f"/{DEMO_SECRET}/login":
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            pw = urllib.parse.parse_qs(body).get("password", [""])[0]
            if PASSWORD and pw == PASSWORD:
                cookie = f"cx_auth={AUTH_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=2592000"
                self.send_response(303)
                self.send_header("Location", f"/{SECRET}/map.html")
                self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(login_page("That passphrase didn't match. Try again."), 401)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not SECRET or len(SECRET) < 24:
        raise SystemExit("MAP_PATH_SECRET missing or too short")
    threading.Thread(target=loop, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), H) as srv:
        gate = "password-gated" if PASSWORD else "OPEN (set MAP_PASSWORD to gate)"
        print(f"map server on :{PORT}  map [{gate}]  demo={'on' if DEMO_SECRET else 'off'}  "
              f"refresh {REFRESH_H}h  (regen in child proc)", flush=True)
        srv.serve_forever()
