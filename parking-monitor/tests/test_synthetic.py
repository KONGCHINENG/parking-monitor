"""End-to-end tests on synthetic camera frames:
markers rendered onto an 'asphalt' background, seen at an angle, with a
dark rectangle as the 'car'. Runs the real detector + Monitor."""

import json
import os

import cv2
import numpy as np
import pytest

from parking.markers import generate, marker_image
from parking.monitor import Monitor
from parking.sources import JpegAssembler
from parking.state import CAM_BLIND, CAM_OK, FREE, OCCUPIED

W, H = 1280, 720
# where each marker lies on the ground (top-left x, y in the flat image)
POSITIONS = {1: (250, 400), 2: (750, 400), 9: (1000, 80)}


def render(cover=(), brightness=1.0, seed=0):
    """Render a frame. cover = marker IDs hidden under a 'car'."""
    rng = np.random.default_rng(seed)
    ground = rng.normal(95, 12, (H, W)).clip(0, 255).astype(np.uint8)   # asphalt
    for mid, (x, y) in POSITIONS.items():
        m = marker_image(mid, 150)                       # with quiet zone
        ground[y:y + m.shape[0], x:x + m.shape[1]] = m
    for mid in cover:                                    # the car
        x, y = POSITIONS[mid]
        cv2.rectangle(ground, (x - 80, y - 60), (x + 260, y + 240), 30, -1)

    # Perspective: camera mounted high, looking down at an angle.
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    dst = np.float32([[120, 60], [W - 120, 60], [W, H], [0, H]])
    frame = cv2.warpPerspective(ground, cv2.getPerspectiveTransform(src, dst), (W, H),
                                borderValue=95)
    frame = cv2.GaussianBlur(frame, (3, 3), 0)                          # lens softness
    frame = (frame.astype(np.float32) * brightness
             + rng.normal(0, 3 * brightness + 1, frame.shape))          # sensor noise
    return cv2.cvtColor(frame.clip(0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, title="", priority="default", tags=""):
        self.sent.append((title, message))
        return True


@pytest.fixture
def cfg(tmp_path):
    return {
        "detection": {"dictionary": "DICT_4X4_50", "clahe": True, "check_interval_seconds": 1},
        "spots": {1: "Spot A", 2: "Spot B"},
        "reference_marker_id": 9,
        "confirm_seconds": 10,
        "camera_alert_seconds": 30,
        "output": {"json_path": str(tmp_path / "Parking" / "parking_status.json"),
                   "heartbeat_seconds": 300},
        "notifications": {"enabled": True, "topic": "x"},
    }


def test_detector_sees_all_markers(cfg):
    mon = Monitor(cfg, FakeNotifier())
    assert set(mon.detector.detect(render())) == {1, 2, 9}


def test_car_covers_marker(cfg):
    mon = Monitor(cfg, FakeNotifier())
    assert set(mon.detector.detect(render(cover=[2]))) == {1, 9}


def test_dark_ir_frame_detected_with_clahe(cfg):
    """Night frame at 25 % brightness - CLAHE should still find the markers."""
    mon = Monitor(cfg, FakeNotifier())
    assert set(mon.detector.detect(render(brightness=0.25))) == {1, 2, 9}


def test_full_scenario(cfg):
    note = FakeNotifier()
    mon = Monitor(cfg, note)
    empty, car_b = render(), render(cover=[2])
    black = np.zeros((H, W, 3), np.uint8)       # lens covered / pitch dark

    t = 0
    def step(frame, seconds):
        nonlocal t
        for _ in range(seconds):
            mon.process(frame, t)
            t += 1

    step(empty, 12)                      # startup: both confirmed FREE
    assert mon.tracker.spots[2].state == FREE
    assert note.sent == []               # no push for startup (notify_on_startup off)

    step(car_b, 12)                      # car parks on B
    assert mon.tracker.spots[2].state == OCCUPIED
    assert note.sent[-1][0] == "Spot B is taken"

    status = json.load(open(mon.json_path, encoding="utf-8"))
    assert status["free_count"] == 1 and status["camera_ok"] is True
    assert status["spots"]["Spot B"]["state"] == "occupied"
    assert status["summary"] == "1/2 free: Spot A"

    step(black, 40)                      # camera goes blind
    assert mon.tracker.spots[1].state == FREE        # NOT flipped to occupied
    assert mon.health.status == CAM_BLIND
    assert note.sent[-1][0] == "Parking camera blind"
    assert json.load(open(mon.json_path))["camera_ok"] is False

    step(empty, 35)                      # light back, car has left
    assert mon.health.status == CAM_OK
    assert mon.tracker.spots[2].state == FREE
    titles = [s[0] for s in note.sent]
    assert "Parking camera OK" in titles and titles[-1] in ("Spot B is FREE", "Parking camera OK")
    assert "Spot B is FREE" in titles


def test_offline_camera(cfg):
    note = FakeNotifier()
    mon = Monitor(cfg, note)
    for t in range(12):
        mon.process(render(), t)
    for t in range(12, 60):
        mon.process(None, t)             # stream lost
    assert mon.health.status == "offline"
    assert mon.tracker.free_count() == 2
    assert note.sent[-1][0] == "Parking camera offline"


def test_generated_marker_png_is_detectable(cfg, tmp_path):
    files = generate({1: "Spot A", 9: "REFERENCE"}, str(tmp_path), size_cm=10, dpi=100)
    assert any(f.endswith("markers.pdf") for f in files)
    img = cv2.imread(str(tmp_path / "marker_01.png"))
    mon = Monitor(cfg, FakeNotifier())
    assert set(mon.detector.detect(img)) == {1}


def test_tiled_pdf(tmp_path):
    files = generate({1: ""}, str(tmp_path), size_cm=30, dpi=72, paper="a4")
    assert os.path.getsize(files[-1]) > 0


def test_jpeg_assembler_splits_stream():
    ok, jpg = cv2.imencode(".jpg", render())
    jpg = jpg.tobytes()
    stream = (b"--jpgboundary\r\nContent-Type: image/jpeg\r\n\r\n" + jpg) * 3
    asm, frames = JpegAssembler(), []
    for i in range(0, len(stream), 1000):            # arrive in small chunks
        frames += asm.feed(stream[i:i + 1000])
    assert len(frames) == 3 and frames[0] == jpg
