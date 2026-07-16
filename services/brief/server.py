#!/usr/bin/env python3
"""
Cortex brief service: generates the daily brief on a schedule and serves it at a
private URL. Generates once on boot (so there's always something to show), then
regenerates every day at BRIEF_HOUR_UTC (default 12:00 UTC = 6am MT).

The page is rendered from the latest brief stored in the brain, so it always
reflects current state. Served at /<BRIEF_PATH_SECRET>/brief.html.
"""

import http.server
import os
import smtplib
import socketserver
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import brief

SECRET = os.environ["BRIEF_PATH_SECRET"]
PORT = int(os.environ.get("PORT", "8080"))
BRIEF_HOUR_UTC = int(os.environ.get("BRIEF_HOUR_UTC", "12"))
CACHE_S = 300

_cache = {}
_html_at = 0.0
_lock = threading.Lock()

WARMING = (b"<!doctype html><body style='margin:0;height:100vh;display:flex;align-items:center;"
           b"justify-content:center;background:#07070d;color:#ff8a5c;font-family:system-ui'>"
           b"<div style='text-align:center'><h2>CortexHost</h2>"
           b"<p style='color:#8a8aa2'>Composing your first brief\xe2\x80\xa6 refresh in ~1 minute.</p></div></body>")


def render_now():
    hist = brief.history(30)
    latest = hist[0] if hist else {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "brief": {}}
    fore = brief.foresight_items(12)
    return {
        "brief": brief.render_page(latest, hist, embed=False),
        "brief_embed": brief.render_page(latest, hist, embed=True),
        "fore": brief.render_foresight(fore, embed=False),
        "fore_embed": brief.render_foresight(fore, embed=True),
    }


def get_html(key="brief", force=False):
    global _cache, _html_at
    with _lock:
        if force or not _cache or (time.time() - _html_at) > CACHE_S:
            try:
                _cache = render_now()
                _html_at = time.time()
            except Exception as e:
                print(f"render error: {type(e).__name__}: {e}", flush=True)
    return _cache.get(key)


def _seconds_until(hour):
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def send_brief_email(date_str, b):
    """Push the brief by email via Gmail SMTP. No-op unless GMAIL_USER + GMAIL_APP_PASSWORD are set."""
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("BRIEF_EMAIL_TO", user)
    if not (user and pw and to):
        return
    link = os.environ.get("BRIEF_URL", "")
    e = brief._esc

    def ul(title, items):
        items = [x for x in (items or []) if x]
        if not items:
            return ""
        lis = "".join('<li style="margin:6px 0">' + e(x) + "</li>" for x in items)
        return ('<h3 style="font-size:13px;color:#ff8a5c;text-transform:uppercase;letter-spacing:.08em;'
                'margin:16px 0 6px">' + title + '</h3><ul style="padding-left:18px;color:#222;margin:0">' + lis + "</ul>")

    link_html = ('<p style="margin-top:18px"><a href="' + link + '" style="color:#7c5cff">Open in CortexHost →</a></p>') if link else ""
    html = (
        '<div style="font-family:-apple-system,Segoe UI,system-ui,sans-serif;max-width:620px;margin:0 auto;color:#111">'
        '<div style="color:#ff8a5c;font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700">Daily brief · '
        + e(date_str) + "</div>"
        '<h1 style="font-size:21px;line-height:1.3;margin:6px 0 10px">' + e(b.get("headline", "Daily brief")) + "</h1>"
        '<p style="font-size:15px;line-height:1.55;color:#333">' + e(b.get("narrative", "")) + "</p>"
        + ul("Patterns", b.get("patterns")) + ul("Open loops", b.get("open_loops")) + ul("Today's focus", b.get("focus"))
        + link_html + "</div>"
    )
    msg = EmailMessage()
    msg["From"] = "CortexHost <" + user + ">"
    msg["To"] = to
    msg["Subject"] = "CortexHost brief · " + date_str
    msg.set_content("Your CortexHost daily brief for " + date_str + " (open in an HTML-capable client).")
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=25) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        print("brief email sent to " + to, flush=True)
    except Exception as ex:
        print("email error: " + type(ex).__name__ + ": " + str(ex), flush=True)


def daily_loop():
    try:
        d, b = brief.generate()
        print(f"brief generated on boot: {d} :: {b.get('headline','')[:90]}", flush=True)
        get_html(force=True)
        if not brief.has_recent_foresight():
            brief.generate_foresight()
            get_html(force=True)
    except Exception as e:
        print(f"boot generate error: {type(e).__name__}: {e}", flush=True)
    while True:
        time.sleep(_seconds_until(BRIEF_HOUR_UTC))
        try:
            d, b = brief.generate()
            print(f"brief generated: {d} :: {b.get('headline','')[:90]}", flush=True)
            get_html(force=True)
            send_brief_email(d, b)
            if datetime.now(timezone.utc).weekday() == 0:  # Monday: weekly foresight pass
                brief.generate_foresight()
                get_html(force=True)
        except Exception as e:
            print(f"scheduled generate error: {type(e).__name__}: {e}", flush=True)
        time.sleep(120)  # avoid double-fire within the trigger minute


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        routes = {
            f"/{SECRET}/brief.html": "brief", f"/{SECRET}": "brief",
            f"/{SECRET}/embed.html": "brief_embed",
            f"/{SECRET}/foresight.html": "fore",
            f"/{SECRET}/foresight-embed.html": "fore_embed",
        }
        if path in routes:
            body = get_html(routes[path])
            if body:
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(503)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(WARMING)))
                self.end_headers()
                self.wfile.write(WARMING)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not SECRET or len(SECRET) < 24:
        raise SystemExit("BRIEF_PATH_SECRET missing or too short")
    threading.Thread(target=daily_loop, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), H) as srv:
        print(f"brief server on :{PORT}  path /{SECRET}/brief.html  daily at {BRIEF_HOUR_UTC}:00 UTC", flush=True)
        srv.serve_forever()
