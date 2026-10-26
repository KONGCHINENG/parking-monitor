"""Make test_video.avi: 15 s empty -> 20 s car on Spot B -> 15 s lens covered -> 20 s empty.
Use it with source.type: file to try the whole monitor without a camera.

    python tools/make_test_video.py
"""

import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.test_synthetic import H, W, render  # noqa: E402

FPS = 5
scenes = [(render(), 15), (render(cover=[2]), 20),
          (np.zeros((H, W, 3), np.uint8), 15), (render(), 20)]

out = os.path.join(ROOT, sys.argv[1] if len(sys.argv) > 1 else "test_video.avi")
w = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
for frame, seconds in scenes:
    for _ in range(seconds * FPS):
        w.write(frame)
w.release()
print("wrote", out)
