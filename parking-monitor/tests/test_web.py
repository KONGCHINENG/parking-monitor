"""The phone app: HTML, live status JSON (with history), annotated frame,
and the files iOS needs for "Add to Home Screen"."""

import cv2
import numpy as np
import requests

from parking.monitor import Monitor
from parking.web import WebServer
from test_synthetic import FakeNotifier, cfg, render  # noqa: F401  (cfg is a fixture)


def test_web_app(cfg):
    mon = Monitor(cfg, FakeNotifier())
    for t in range(12):                              # > confirm_seconds (10): both FREE
        mon.process(render(), t)
    for t in range(12, 24):                          # car parks on B
        mon.process(render(cover=[2]), t)
    web = WebServer(mon, port=0).start()             # port 0 = any free port
    web.jpeg = cv2.imencode(".jpg", mon.draw(render()))[1].tobytes()
    base = f"http://127.0.0.1:{web.port}"
    try:
        html = requests.get(base + "/", timeout=5).text
        assert "apple-touch-icon" in html and "manifest.webmanifest" in html

        s = requests.get(base + "/status.json", timeout=5).json()
        assert s["spots"]["Spot A"]["state"] == "free"
        assert s["spots"]["Spot B"]["state"] == "occupied"
        assert s["spots"]["Spot B"]["since"]          # time of the change is known
        assert s["events"][0] == {"time": s["events"][0]["time"], "spot": "Spot B", "state": "occupied"}

        r = requests.get(base + "/frame.jpg", timeout=5)
        assert r.headers["Content-Type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"

        m = requests.get(base + "/manifest.webmanifest", timeout=5).json()
        assert m["display"] == "standalone"
        icon = requests.get(base + "/icon-180.png", timeout=5)
        assert icon.content[:4] == b"\x89PNG"
        assert cv2.imdecode(np.frombuffer(icon.content, np.uint8), 1).shape == (180, 180, 3)

        # Android "Install app" needs a 192 + 512 icon and a service worker
        sizes = {i["sizes"] for i in m["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for n in (192, 512):
            png = requests.get(f"{base}/icon-{n}.png", timeout=5).content
            assert cv2.imdecode(np.frombuffer(png, np.uint8), 1).shape == (n, n, 3)
        sw = requests.get(base + "/sw.js", timeout=5)
        assert "javascript" in sw.headers["Content-Type"] and "fetch" in sw.text
    finally:
        web.httpd.shutdown()


def test_default_bind_is_this_pc_only(cfg):
    web = WebServer(Monitor(cfg, FakeNotifier()), port=0).start()
    try:
        assert web.httpd.server_address[0] == "127.0.0.1"
    finally:
        web.httpd.shutdown()
