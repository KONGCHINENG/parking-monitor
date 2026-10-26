"""AP-mode code against a simulated A9 camera.

The fake camera behaves like the real one in ap_test logs (firmware with
unknown version, 2026-10): it mixes PCM audio packets (cmd 6) in front of
replies, streams JPEG in chunks (cmd 1), and drops the connection when it
receives a HEARTBEAT (cmd 100)."""

import json
import os
import socket
import struct
import sys
import threading
import time

import cv2
import numpy as np
import pytest

A9_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "a9-v720", "src")
pytestmark = pytest.mark.skipif(not os.path.isdir(A9_SRC), reason="a9-v720 not next to project")

from parking.sources import A9APSource, a9_ap_handshake, a9_ap_stream  # noqa: E402

HDR = "<LHBB8sL"   # len, cmd, msg_flag, deal_flag, forward_id, pkg_id  (20 bytes)
JPG = cv2.imencode(".jpg", np.full((480, 640, 3), 90, np.uint8))[1].tobytes()


def pkt(cmd, payload=b""):
    return struct.pack(HDR, len(payload), cmd, 0, 0, b"0" * 8, 0) + payload


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


class FakeA9:
    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.heartbeats = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        lock, streaming = threading.Lock(), threading.Event()

        def send(data):
            with lock:
                conn.sendall(data)

        def stream():
            try:
                while True:
                    send(pkt(6, os.urandom(960)))                    # audio
                    for i in range(0, len(JPG), 1000):               # JPEG chunks
                        send(pkt(1, JPG[i:i + 1000]))
                    time.sleep(0.05)
            except OSError:
                pass

        try:
            while True:
                n, cmd = struct.unpack(HDR, recv_exact(conn, 20))[:2]
                body = recv_exact(conn, n) if n else b""
                if cmd == 115:                                       # LIVE_MOTION
                    send(pkt(115))
                elif cmd == 0:                                       # JSON request
                    req = json.loads(body)
                    send(pkt(6, os.urandom(960)))                    # audio first!
                    send(pkt(0, json.dumps({"code": req["code"], "status": 200}).encode()))
                    if req.get("content", {}).get("code") == 3:     # open video
                        streaming.set()
                        threading.Thread(target=stream, daemon=True).start()
                elif cmd == 100:                                     # HEARTBEAT -> drop
                    self.heartbeats += 1
                    conn.close()
                    return
        except (OSError, ConnectionError, ValueError):
            conn.close()


@pytest.fixture
def a9():
    sys.path.insert(0, os.path.abspath(A9_SRC))
    import cmd_udp
    assert cmd_udp.CODE_FORWARD_OPEN_A_OPEN_V == 3, "fake camera assumes code 3"
    cam = FakeA9()
    yield cam
    cam.srv.close()


def _session(port, keepalive, seconds):
    from netcl_tcp import netcl_tcp
    from v720_ap import v720_ap
    frames, start = [0], time.monotonic()

    def on_packet(cmd, payload):
        if cmd == 1 and payload[:2] == b"\xff\xd8":
            frames[0] += 1

    with netcl_tcp("127.0.0.1", port) as sock:
        cam = v720_ap(sock)
        a9_ap_handshake(cam)                   # must not crash despite audio
        try:
            a9_ap_stream(cam, on_packet, keepalive, interval=1.0,
                         should_stop=lambda: time.monotonic() - start > seconds)
            ended = None
        except OSError as e:
            ended = e
    return frames[0], ended


def test_no_keepalive_streams_continuously(a9):
    frames, ended = _session(a9.port, "none", 3)
    assert ended is None and frames > 20


def test_heartbeat_keepalive_gets_dropped(a9):
    frames, ended = _session(a9.port, "heartbeat", 3)
    assert isinstance(ended, OSError) and a9.heartbeats == 1


def test_a9_ap_source_delivers_frames(a9):
    src = A9APSource("127.0.0.1", a9.port, A9_SRC, keepalive="none")
    src.start()
    end = time.time() + 5
    frame = None
    while time.time() < end and frame is None:
        frame, _ = src.latest()
        time.sleep(0.05)
    src.stop()
    assert frame is not None and frame.shape == (480, 640, 3)
