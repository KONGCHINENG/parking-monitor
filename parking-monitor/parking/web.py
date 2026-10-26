"""The phone app: a small web app served by the monitor itself.

On the iPhone: open the page in Safari -> Share -> "Add to Home Screen".
It then starts from its own icon, full screen, like a normal app.

    /                      the app (polls status every 2 s while open)
    /status.json           same data as parking_status.json + recent events
    /frame.jpg             latest camera picture with markers drawn
    /manifest.webmanifest  app name/icon/colours for "Add to Home Screen"
    /icon-180.png, /icon-512.png

By default it only listens on this PC (127.0.0.1). The phone reaches it
through Tailscale ("tailscale serve"), so nothing is open to the WiFi
network. For the offline demo, `run --lan` opens it to the local network.
"""

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

log = logging.getLogger(__name__)

APP_NAME = "Parking"
THEME = "#1f9d55"

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Parking</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Parking">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#f4f4f2" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#151515" media="(prefers-color-scheme: dark)">
<style>
  :root { --bg:#f4f4f2; --card:#fff; --text:#1b1b1b; --muted:#6b6b6b; --line:#e4e4e0;
          --free:#1f9d55; --occ:#d64545; --unk:#b7791f; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151515; --card:#222; --text:#f2f2f2; --muted:#9a9a9a; --line:#333; } }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:var(--bg); color:var(--text);
         font: 16px/1.4 -apple-system, system-ui, sans-serif;
         padding: calc(env(safe-area-inset-top) + 16px) 16px calc(env(safe-area-inset-bottom) + 24px); }
  main { max-width: 560px; margin: 0 auto; }
  header { display:flex; align-items:center; justify-content:space-between; }
  h1 { font-size: 20px; margin: 0; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--muted); display:inline-block; }
  .dot.ok { background:var(--free); } .dot.bad { background:var(--occ); }
  #summary { font-size: 44px; font-weight: 750; letter-spacing:-0.02em; margin: 18px 0 2px; }
  #sub { color: var(--muted); margin-bottom: 16px; }
  .banner { border-radius:12px; padding:12px 14px; margin-bottom:14px; font-weight:600; display:none; }
  .banner.show { display:block; }
  #camwarn { background: color-mix(in srgb, var(--occ) 14%, var(--card)); color: var(--occ); }
  #offline { background: var(--card); color: var(--muted); border:1px dashed var(--line); }
  .spots { display:grid; grid-template-columns: repeat(auto-fill, minmax(150px,1fr)); gap:10px; }
  .spot { background:var(--card); border-radius:16px; padding:14px 14px 12px; border-left:8px solid var(--unk); }
  .spot.free { border-color: var(--free); } .spot.occupied { border-color: var(--occ); }
  .name { font-weight:600; }
  .state { font-size:22px; font-weight:750; text-transform:uppercase; margin-top:2px; }
  .free .state { color:var(--free); } .occupied .state { color:var(--occ); } .unknown .state { color:var(--unk); }
  .since { color:var(--muted); font-size:13px; margin-top:2px; min-height:18px; }
  h2 { font-size: 15px; color: var(--muted); font-weight:600; margin: 26px 0 8px; }
  button { font: inherit; font-weight:600; color: var(--text); background: var(--card);
           border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; width:100%; }
  #frame { width:100%; border-radius:12px; margin-top:10px; display:none; background:#000; aspect-ratio: 4/3; }
  ul { list-style:none; margin:0; padding:0; background:var(--card); border-radius:14px; }
  li { display:flex; justify-content:space-between; gap:12px; padding:11px 14px; border-top:1px solid var(--line); }
  li:first-child { border-top:0; }
  li .t { color:var(--muted); font-variant-numeric: tabular-nums; white-space:nowrap; }
  li.free b { color:var(--free); } li.occupied b { color:var(--occ); } li.cam b { color:var(--unk); }
  #empty { color:var(--muted); padding:12px 14px; }
  footer { color: var(--muted); font-size: 13px; margin-top: 18px; text-align:center; }
</style></head>
<body><main>
  <header><h1>Parking</h1><span class="dot" id="dot" title="connection"></span></header>
  <div id="summary">&ndash;</div>
  <div id="sub">Connecting&hellip;</div>
  <div class="banner" id="offline">Can't reach the parking PC. Retrying&hellip;</div>
  <div class="banner" id="camwarn"></div>
  <div class="spots" id="spots"></div>

  <h2>Camera</h2>
  <button id="camBtn">Show camera</button>
  <img id="frame" alt="Live camera view">

  <h2>Recent activity</h2>
  <ul id="events"><li id="empty">No changes yet.</li></ul>
  <footer id="updated"></footer>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let camOn = false, lastOk = 0, timer = null;
try { camOn = localStorage.getItem('camOn') === '1'; } catch (e) {}

function ago(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return m + ' min';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' h ' + (m % 60) + ' min';
  return Math.floor(h / 24) + ' d';
}
function sinceText(iso) {             // "just now" / "for 12 min"
  if (!iso) return '';
  const a = ago(iso);
  return a === 'just now' ? a : 'for ' + a;
}
function clock(iso) {
  return new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}
const LABEL = {free: 'free', occupied: 'taken', blind: 'blind', offline: 'offline', ok: 'OK'};

function render(s) {
  $('summary').textContent = s.free_count + ' of ' + s.total + ' free';
  $('sub').textContent = s.free_count === 0 ? 'Car park full' :
    Object.entries(s.spots).filter(([, v]) => v.state === 'free').map(([n]) => n).join(', ') + ' available';
  const cw = $('camwarn');
  cw.textContent = s.camera_status === 'offline' ? 'Camera offline - showing last known state' :
                   'Camera can\\'t see clearly - showing last known state';
  cw.classList.toggle('show', !s.camera_ok);
  $('spots').innerHTML = Object.entries(s.spots).map(([name, v]) =>
    `<div class="spot ${esc(v.state)}"><div class="name">${esc(name)}</div>
     <div class="state">${v.state === 'occupied' ? 'Taken' : esc(v.state)}</div>
     <div class="since">${sinceText(v.since)}</div></div>`).join('');
  const ev = s.events || [];
  $('events').innerHTML = ev.length ? ev.slice(0, 15).map(e => {
    const cls = e.spot === 'Camera' ? 'cam' : e.state;
    return `<li class="${esc(cls)}"><span>${esc(e.spot)} <b>${esc(LABEL[e.state] || e.state)}</b></span>
            <span class="t">${clock(e.time)}</span></li>`;
  }).join('') : '<li id="empty">No changes yet.</li>';
}

async function tick() {
  try {
    const r = await fetch('status.json', {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
    lastOk = Date.now();
    $('offline').classList.remove('show');
    $('dot').className = 'dot ok';
    $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
    if (camOn) $('frame').src = 'frame.jpg?t=' + Date.now();
  } catch (e) {
    $('offline').classList.add('show');
    $('dot').className = 'dot bad';
    if (lastOk) $('updated').textContent = 'Last update ' + new Date(lastOk).toLocaleTimeString();
  }
}

function setCam(on) {
  camOn = on;
  try { localStorage.setItem('camOn', on ? '1' : '0'); } catch (e) {}
  $('frame').style.display = on ? 'block' : 'none';
  $('camBtn').textContent = on ? 'Hide camera' : 'Show camera';
  if (on) $('frame').src = 'frame.jpg?t=' + Date.now();
}
$('camBtn').onclick = () => setCam(!camOn);

// Poll every 2 s, but only while the app is on screen (saves battery and data).
function start() { if (!timer) { tick(); timer = setInterval(tick, 2000); } }
function stop() { clearInterval(timer); timer = null; }
document.addEventListener('visibilitychange', () => document.hidden ? stop() : start());
setCam(camOn);
start();
// For Android's "Install app". Only works over https (Tailscale), skipped otherwise.
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
</script>
</body></html>
"""

MANIFEST = {
    "name": "Parking Monitor",
    "short_name": APP_NAME,
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#f4f4f2",
    "theme_color": "#f4f4f2",
    "icons": [
        {"src": "icon-180.png", "sizes": "180x180", "type": "image/png"},
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},   # Android needs 192 + 512
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}


# Android Chrome only offers "Install app" if the page has a service worker.
# This one caches nothing - every request still goes to the PC, so the app
# never shows stale parking data.
SERVICE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
"""


def make_icon(size: int) -> bytes:
    """App icon drawn with OpenCV: white 'P' on green (no image files needed).
    Full-bleed square - iOS rounds the corners itself."""
    img = np.zeros((size, size, 3), np.uint8)
    img[:] = (85, 157, 31)                                   # THEME in BGR
    scale = size / 75
    thick = max(2, int(size / 14))
    (w, h), _ = cv2.getTextSize("P", cv2.FONT_HERSHEY_DUPLEX, scale, thick)
    cv2.putText(img, "P", ((size - w) // 2, (size + h) // 2), cv2.FONT_HERSHEY_DUPLEX,
                scale, (255, 255, 255), thick, cv2.LINE_AA)
    return cv2.imencode(".png", img)[1].tobytes()


class WebServer:
    def __init__(self, monitor, port=8080, bind="127.0.0.1"):
        self.monitor = monitor
        self.port = port
        self.bind = bind
        self.jpeg = None            # latest annotated frame, set by the main loop
        self.icons = {n: make_icon(n) for n in (180, 192, 512)}

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass                # keep the log file quiet

            def _send(self, body, ctype, code=200, cache="no-store"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", cache)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    self._send(PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/status.json":
                    self._send(json.dumps(server.monitor.status()).encode(), "application/json")
                elif path == "/frame.jpg" and server.jpeg is not None:
                    self._send(server.jpeg, "image/jpeg")
                elif path == "/manifest.webmanifest":
                    self._send(json.dumps(MANIFEST).encode(), "application/manifest+json")
                elif path in ("/icon-180.png", "/apple-touch-icon.png", "/favicon.ico"):
                    self._send(server.icons[180], "image/png", cache="max-age=86400")
                elif path in ("/icon-192.png", "/icon-512.png"):
                    self._send(server.icons[int(path[6:9])], "image/png", cache="max-age=86400")
                elif path == "/sw.js":
                    self._send(SERVICE_WORKER.encode(), "text/javascript")
                else:
                    self._send(b"not found", "text/plain", 404)

        httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.httpd = httpd
        if self.bind not in ("127.0.0.1", "0.0.0.0"):
            # Also always reachable on this PC at 127.0.0.1 - that address
            # never changes, unlike the camera-WiFi one (DHCP).
            local = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
            threading.Thread(target=local.serve_forever, daemon=True).start()
            self.httpd_local = local
        if self.bind == "0.0.0.0":
            for ip in local_ips():
                log.info("app page: http://%s:%d (open to the local network)", ip, self.port)
        elif self.bind != "127.0.0.1":
            log.info("app page: http://%s:%d (camera WiFi only)", self.bind, self.port)
        else:
            log.info("app page: http://%s:%d (this PC only - use Tailscale for the phone)",
                     self.bind, self.port)
        return self


def local_ips():
    """IPv4 addresses of this PC (what the phone should type)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.")) or ["127.0.0.1"]
