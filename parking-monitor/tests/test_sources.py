"""Frame sources against a tiny local MJPEG server that mimics the a9-v720
fake server (/dev/list + /dev/<id>/live), and a video file."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from parking.sources import A9FakeServerSource, FileSource, MjpegSource

FRAME = np.full((240, 320, 3), 128, np.uint8)
cv2.putText(FRAME, "test", (50, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
JPG = cv2.imencode(".jpg", FRAME)[1].tobytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/dev/list":
            body = json.dumps([{"host": "10.0.0.9", "port": 1234, "uid": "0800c00128F8"}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/dev/0800c00128F8/live", "/video"):
            self.send_response(200)
            self.send_header("Content-Type", 'multipart/x-mixed-replace; boundary="jpgboundary"')
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b"--jpgboundary\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(JPG)}\r\n\r\n".encode() + JPG + b"\r\n")
                    time.sleep(0.05)
            except OSError:
                pass
        else:
            self.send_error(404)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def wait_frame(src, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        frame, _ = src.latest()
        if frame is not None:
            return frame
        time.sleep(0.05)
    return None


def test_mjpeg_source(server):
    src = MjpegSource(server + "/video")
    src.start()
    frame = wait_frame(src)
    src.stop()
    assert frame is not None and frame.shape == FRAME.shape


def test_a9_fakeserver_source_discovers_camera(server):
    src = A9FakeServerSource(server)          # no camera_id -> uses /dev/list
    src.start()
    frame = wait_frame(src)
    src.stop()
    assert frame is not None


def test_file_source(tmp_path):
    path = str(tmp_path / "v.avi")
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 240))
    for _ in range(10):
        w.write(FRAME)
    w.release()
    src = FileSource(path, loop=True)
    src.start()
    frame = wait_frame(src)
    src.stop()
    assert frame is not None and frame.shape == FRAME.shape
