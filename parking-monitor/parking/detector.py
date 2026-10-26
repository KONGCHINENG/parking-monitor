"""ArUco marker detection (OpenCV >= 4.7 API: cv2.aruco.ArucoDetector)."""

import cv2
import numpy as np


def get_dictionary(name: str = "DICT_4X4_50"):
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))


class MarkerDetector:
    def __init__(self, dictionary: str = "DICT_4X4_50", clahe: bool = True,
                 clahe_clip_limit: float = 2.0):
        params = cv2.aruco.DetectorParameters()
        # Sub-pixel corner refinement - only matters for drawing, cheap anyway.
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(get_dictionary(dictionary), params)

        # CLAHE = local contrast equalisation. At night / in IR mode the image
        # is dim and washed out; CLAHE stretches contrast in small tiles so the
        # black/white marker squares separate again.
        self.clahe = (cv2.createCLAHE(clipLimit=clahe_clip_limit, tileGridSize=(8, 8))
                      if clahe else None)

    def preprocess(self, frame: np.ndarray) -> np.ndarray:
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.clahe is not None:
            gray = self.clahe.apply(gray)
        return gray

    def detect(self, frame: np.ndarray) -> dict:
        """Returns {marker_id: corners (4x2 float array)} for every marker found."""
        corners, ids, _rejected = self.detector.detectMarkers(self.preprocess(frame))
        if ids is None:
            return {}
        return {int(i): c.reshape(4, 2) for i, c in zip(ids.flatten(), corners)}
