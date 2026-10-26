"""The main loop: frame -> detect markers -> state machines -> JSON + push."""

import logging
import threading
import time
from collections import deque

import cv2
import numpy as np

from .detector import MarkerDetector
from .output import Notifier, now_iso, resolve_json_path, write_json_atomic
from .state import (CAM_BLIND, CAM_OFFLINE, CAM_OK, FREE, OCCUPIED, UNKNOWN,
                    CameraHealth, SpotTracker)

log = logging.getLogger(__name__)

# BGR colours for the debug window
COLORS = {FREE: (0, 200, 0), OCCUPIED: (0, 0, 255), UNKNOWN: (0, 200, 255)}

CAMERA_MESSAGES = {
    CAM_BLIND: ("Parking camera blind", "Reference marker not visible (dark, rain, "
                "blocked lens?). Spot states are frozen.", "high", "warning"),
    CAM_OFFLINE: ("Parking camera offline", "No video from the camera.", "high", "rotating_light"),
    CAM_OK: ("Parking camera OK", "Camera recovered.", "default", "white_check_mark"),
}


class Monitor:
    def __init__(self, cfg: dict, notifier=None):
        self.cfg = cfg
        det = cfg["detection"]
        self.detector = MarkerDetector(det.get("dictionary", "DICT_4X4_50"),
                                       det.get("clahe", True), det.get("clahe_clip_limit", 2.0))
        self.ref_id = int(cfg["reference_marker_id"])
        spots = {int(k): v for k, v in cfg["spots"].items()}
        if self.ref_id in spots:
            raise ValueError("reference_marker_id must not also be a spot marker")
        self.tracker = SpotTracker(spots, cfg.get("confirm_seconds", 10))
        self.health = CameraHealth(cfg.get("camera_alert_seconds", 30))
        self.notifier = notifier or Notifier(cfg["notifications"])
        self.notify_startup = cfg["notifications"].get("notify_on_startup", False)
        self.json_path = resolve_json_path(cfg["output"].get("json_path", "auto"),
                                           cfg["output"].get("fallback_dir", "status"))
        self.heartbeat_s = cfg["output"].get("heartbeat_seconds", 300)
        self._last_write = None
        self.last_markers = {}        # for drawing: {id: corners}
        self.marker_pos = {}          # last place each marker was seen (for labels)
        self.started = now_iso()
        self.spot_since = {}          # spot name -> wall-clock time of last confirmed change
        self.events = deque(maxlen=30)  # recent changes, newest first (shown in the app)
        self._events_lock = threading.Lock()   # web thread reads while main loop writes

    # ------------------------------------------------------------------
    def process(self, frame, now: float):
        """Handle one sample. `frame` is None if the camera has no fresh frame.
        `now` is a monotonic time in seconds (injected so tests can fake time)."""
        frame_ok = frame is not None
        markers = self.detector.detect(frame) if frame_ok else {}
        self.last_markers = markers
        for mid, c in markers.items():
            self.marker_pos[mid] = c.mean(axis=0)
        ref_seen = self.ref_id in markers

        # Absence of a spot marker only counts if the reference marker proves
        # we can actually see the ground right now.
        changes = self.tracker.update(now, set(markers), absence_trusted=frame_ok and ref_seen)
        cam_change = self.health.update(now, frame_ok, ref_seen)

        for ch in changes:
            log.info("%s: %s -> %s", ch.name, ch.old, ch.new)
            self.spot_since[ch.name] = now_iso()
            with self._events_lock:
                self.events.appendleft({"time": now_iso(), "spot": ch.name, "state": ch.new})
            if ch.old != UNKNOWN or self.notify_startup:
                self._notify_spot(ch)
        if cam_change:
            log.warning("camera: %s -> %s", cam_change.old, cam_change.new)
            with self._events_lock:
                self.events.appendleft({"time": now_iso(), "spot": "Camera", "state": cam_change.new})
            title, msg, prio, tags = CAMERA_MESSAGES[cam_change.new]
            self.notifier.send(msg, title=title, priority=prio, tags=tags)

        heartbeat_due = self._last_write is None or now - self._last_write >= self.heartbeat_s
        if changes or cam_change or heartbeat_due:
            self.write_status()
            self._last_write = now
        return changes, cam_change

    # ------------------------------------------------------------------
    def _notify_spot(self, ch):
        free, total = self.tracker.free_count(), len(self.tracker.spots)
        if ch.new == FREE:
            title, tags = f"{ch.name} is FREE", "green_circle"
        else:
            title, tags = f"{ch.name} is taken", "red_circle"
        self.notifier.send(f"{free} of {total} spots free", title=title, tags=tags)

    def status(self) -> dict:
        spots = {}
        for s in self.tracker.spots.values():
            spots[s.name] = {"marker_id": s.marker_id, "state": s.state,
                             "since": self.spot_since.get(s.name)}   # None until first confirmed
        free = self.tracker.free_count()
        free_names = [s.name for s in self.tracker.spots.values() if s.state == FREE]
        return {
            "updated": now_iso(),
            "camera_ok": self.health.status == CAM_OK,
            "camera_status": self.health.status,
            "free_count": free,
            "total": len(spots),
            # Ready-made sentence for the iOS Shortcut to display.
            "summary": (f"{free}/{len(spots)} free: {', '.join(free_names)}" if free_names
                        else f"0/{len(spots)} free")
                       + ("" if self.health.status == CAM_OK else f" (camera {self.health.status})"),
            "spots": spots,
            "started": self.started,
            "events": self._events_copy(),
        }

    def _events_copy(self):
        with self._events_lock:
            return list(self.events)

    def write_status(self):
        try:
            write_json_atomic(self.json_path, self.status())
        except OSError as e:
            log.error("could not write %s: %s", self.json_path, e)

    # ------------------------------------------------------------------
    def draw(self, frame):
        """Debug overlay for --show."""
        img = frame.copy()
        if self.last_markers:
            ids = list(self.last_markers)
            corners = [self.last_markers[i].reshape(1, 4, 2) for i in ids]
            cv2.aruco.drawDetectedMarkers(img, corners, None)
            # Shortest edge in pixels: keep this above ~25 px for reliable
            # detection (see README "marker size vs distance").
            for mid, c in self.last_markers.items():
                edge = min(np.linalg.norm(c[i] - c[(i + 1) % 4]) for i in range(4))
                x, y = c.max(axis=0).astype(int)
                cv2.putText(img, f"id{mid} {edge:.0f}px", (x + 5, y), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (255, 255, 0), 1, cv2.LINE_AA)
        for s in self.tracker.spots.values():
            pos = self.marker_pos.get(s.marker_id)
            if pos is None:
                continue
            x, y = int(pos[0]), int(pos[1])
            text = f"{s.name}: {s.state}" + (f" ({s.pending}?)" if s.pending else "")
            cv2.putText(img, text, (x - 60, y - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        COLORS[s.state], 2, cv2.LINE_AA)
        cam = self.health.status
        ref = "REF OK" if self.ref_id in self.last_markers else "REF MISSING"
        cv2.putText(img, f"camera: {cam}  {ref}  free: {self.tracker.free_count()}/{len(self.tracker.spots)}",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 200, 0) if cam == CAM_OK else (0, 0, 255), 2, cv2.LINE_AA)
        return img


def run(cfg: dict, source, show: bool = False, lan: bool = False):
    """Loop forever: sample the newest frame every check interval.

    lan=False: the app page only listens on this PC (127.0.0.1); Tailscale
               ("tailscale serve") carries it privately to your phone.
    lan=True:  also open it to the local network - for the offline demo where
               the phone joins the camera's WiFi. Don't use on shared WiFi.
    """
    mon = Monitor(cfg)
    log.info("status file: %s", mon.json_path)
    interval = cfg["detection"].get("check_interval_seconds", 1.0)
    stale_s = cfg["source"].get("stale_seconds", 15)

    web = None
    web_cfg = cfg.get("web", {})
    if web_cfg.get("enabled", True):
        from .web import WebServer, local_ips
        bind = web_cfg.get("bind", "127.0.0.1")
        if lan:
            # Only open the page on the camera's own WiFi (192.168.169.x), never
            # on the other network the PC may also be on (e.g. a shared WiFi).
            # If the camera WiFi isn't connected, stay on this PC only - never
            # fall back to "all networks".
            cam_net = [ip for ip in local_ips() if ip.startswith("192.168.169.")]
            if cam_net:
                bind = cam_net[0]
            else:
                log.warning("--lan: camera WiFi not connected - app page on this PC only")
        try:
            web = WebServer(mon, web_cfg.get("port", 8080), bind).start()
            if bind == "0.0.0.0":
                print("\n  Phone: join the same WiFi, then open in Safari:")
                for ip in local_ips():
                    print(f"    http://{ip}:{web.port}")
            elif bind != "127.0.0.1":
                print(f"\n  On this PC open:   http://127.0.0.1:{web.port}")
                print(f"  Phone: join the camera WiFi (Nax_...), then open:  http://{bind}:{web.port}")
                print("  (the phone address can change each time - always use the one shown here)")
            else:
                print(f"\n  App page: http://127.0.0.1:{web.port}  (phone: via Tailscale, see README)")
            print()
        except OSError as e:
            log.error("web page could not start on port %s: %s", web_cfg.get("port"), e)

    source.start()
    try:
        while True:
            t0 = time.monotonic()
            frame, frame_time = source.latest()
            if frame is not None and t0 - frame_time > stale_s:
                frame = None          # old frame = camera stopped sending
            mon.process(frame, t0)
            if web is not None and frame is not None:
                web.jpeg = cv2.imencode(".jpg", mon.draw(frame), [cv2.IMWRITE_JPEG_QUALITY, 75])[1].tobytes()

            if show:
                if frame is not None:
                    cv2.imshow("parking monitor", mon.draw(frame))   # PC window
                wait_ms = max(1, int((interval - (time.monotonic() - t0)) * 1000))
                if cv2.waitKey(wait_ms) & 0xFF in (ord("q"), 27):
                    break
            else:
                time.sleep(max(0.0, interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        if show:
            cv2.destroyAllWindows()
