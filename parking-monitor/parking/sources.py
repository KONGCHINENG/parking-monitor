"""Frame sources. All share one interface:

    src.start()           start the background reader thread
    src.latest()          -> (frame or None, monotonic time it was received)
    src.stop()

Each source runs a thread that reads frames as fast as the camera sends them
and keeps only the newest one. The monitor samples that newest frame once per
check interval. (If we only read once per second from an RTSP/MJPEG stream,
OpenCV's buffer would hand us frames that are seconds or minutes old.)

If the stream breaks, the thread waits and reconnects with exponential backoff.
"""

import logging
import os
import sys
import threading
import time

import cv2
import numpy as np
import requests

log = logging.getLogger(__name__)


class FrameSource:
    name = "base"

    def __init__(self, reconnect_min=2.0, reconnect_max=60.0):
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self._frame = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # --- public -----------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def latest(self):
        with self._lock:
            return self._frame, self._frame_time

    # --- for subclasses ---------------------------------------------------
    def _publish(self, frame):
        with self._lock:
            self._frame = frame
            self._frame_time = time.monotonic()

    def _stream(self):
        """Connect and call self._publish(frame) for every frame until the
        stream ends (return) or fails (raise). Implemented by subclasses."""
        raise NotImplementedError

    # --- reconnect loop ---------------------------------------------------
    def _run(self):
        delay = self.reconnect_min
        while not self._stop.is_set():
            got_frames_before = self._frame_time
            try:
                log.info("%s: connecting", self.name)
                self._stream()
                log.warning("%s: stream ended", self.name)
            except Exception as e:  # any failure -> reconnect
                log.warning("%s: stream error: %s", self.name, e)
            if self._frame_time != got_frames_before:
                delay = self.reconnect_min       # it worked for a while: reset backoff
            self._stop.wait(delay)
            delay = min(delay * 2, self.reconnect_max)


class OpenCVSource(FrameSource):
    """RTSP (or anything cv2.VideoCapture understands)."""
    name = "rtsp"

    def __init__(self, url, **kw):
        super().__init__(**kw)
        self.url = url

    def _stream(self):
        # TCP transport is far more reliable than UDP over WiFi.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                raise IOError("cannot open stream")
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    return
                self._publish(frame)
        finally:
            cap.release()


class FileSource(FrameSource):
    """Plays a video file at its real frame rate (optionally looping)."""
    name = "file"

    def __init__(self, path, loop=True, **kw):
        super().__init__(**kw)
        self.path, self.loop = path, loop

    def _stream(self):
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.path)
            if not cap.isOpened():
                raise IOError(f"cannot open {self.path}")
            period = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 10)
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                self._publish(frame)
                time.sleep(period)
            cap.release()
            if not self.loop:
                # End of file: stop producing frames. The monitor will then
                # see the frame go stale and report the camera offline.
                self._stop.wait()
                return


class JpegAssembler:
    """Finds complete JPEG images in a byte stream.

    A JPEG starts with bytes FF D8 (SOI) and ends with FF D9 (EOI).
    MJPEG-over-HTTP is just JPEGs separated by text headers, so scanning
    for these markers works for any MJPEG stream and for the A9's raw
    chunked JPEG packets - no need to parse multipart headers.
    """

    def __init__(self, max_size=4 * 1024 * 1024):
        self.buf = bytearray()
        self.max_size = max_size

    def feed(self, data: bytes) -> list:
        """Add bytes, return a list of complete JPEG byte strings."""
        self.buf.extend(data)
        out = []
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                self.buf.clear()               # no image start: drop junk
                break
            end = self.buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                del self.buf[:start]           # keep partial image only
                if len(self.buf) > self.max_size:
                    self.buf.clear()           # corrupted stream protection
                break
            out.append(bytes(self.buf[start:end + 2]))
            del self.buf[:end + 2]
        return out


def decode_jpeg(data: bytes):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


class MjpegSource(FrameSource):
    """HTTP MJPEG stream (multipart/x-mixed-replace), read with requests."""
    name = "mjpeg"

    def __init__(self, url, **kw):
        super().__init__(**kw)
        self.url = url

    def _resolve_url(self):
        return self.url

    def _stream(self):
        url = self._resolve_url()
        # timeout=(connect, read): read timeout fires if the camera goes silent.
        with requests.get(url, stream=True, timeout=(5, 10)) as r:
            r.raise_for_status()
            jpg = JpegAssembler()
            for chunk in r.iter_content(chunk_size=16384):
                if self._stop.is_set():
                    return
                for data in jpg.feed(chunk):
                    frame = decode_jpeg(data)
                    if frame is not None:
                        self._publish(frame)


class A9FakeServerSource(MjpegSource):
    """A9/V720 camera on home WiFi, via the a9-v720 fake server's MJPEG URL.
    If no camera_id is configured, asks the server's /dev/list for it."""
    name = "a9v720-fakeserver"

    def __init__(self, server_url, camera_id="", **kw):
        super().__init__(url=None, **kw)
        self.server_url = server_url.rstrip("/")
        self.camera_id = camera_id

    def _resolve_url(self):
        cam_id = self.camera_id
        if not cam_id:
            devs = requests.get(f"{self.server_url}/dev/list", timeout=5).json()
            if not devs:
                raise IOError("fake server is up but no camera has registered yet")
            # Format: [{"host": "...", "port": 1234, "uid": "0800c00128F8"}, ...]
            cam_id = devs[0]["uid"]
            log.info("a9v720: using camera %s", cam_id)
        return f"{self.server_url}/dev/{cam_id}/live"


def a9_ap_handshake(cam) -> dict:
    """Open a session with an A9 camera in AP mode.

    Same steps as a9-v720's v720_ap.init_live_motion(), but that function
    crashes if the camera doesn't answer the 'base info' query (seen on some
    firmware). Base info only carries the firmware version - video works
    without it - so here it is optional. Returns the base info dict or {}.
    """
    import cmd_udp
    from prot_udp import prot_udp

    ret = cam._req(prot_udp(cmd=cmd_udp.P2P_UDP_CMD_LIVE_MOTION).req())
    if not ret:
        raise IOError("A9 camera did not answer the LIVE_MOTION request")
    r = cam.connect()
    log.info("a9v720: connect reply: %s", getattr(r, "json", r))
    for attempt in range(3):
        try:
            info = cam.baseinfo()
            if info is not None:
                log.info("a9v720: base info: %s", info.content)
                return info.content
        except Exception as e:                 # unexpected reply format
            log.warning("a9v720: base info attempt %d failed: %s", attempt + 1, e)
    log.warning("a9v720: camera gave no base info (firmware version unknown) - continuing")
    return {}


def a9_ap_send(cam, content: dict):
    """Send an AP command WITHOUT waiting for the reply.

    a9-v720's own methods (cam.ir_led(), cam.flip(), ...) read the next packet
    as "the reply", but this camera interleaves audio and late replies, so
    they get the wrong packet and can crash parsing it. We never need the
    replies, so just send."""
    from prot_ap import prot_ap
    from prot_json_udp import prot_json_udp
    content.setdefault("devTarget", prot_json_udp.DEFAULT_DEV_TARGET)
    cam._socket.send(prot_ap(content=content).req())


def a9_ap_stream(cam, on_packet, keepalive="none", interval=5.0, should_stop=None):
    """Start live video and pass every packet to on_packet(cmd, payload)
    until the camera ends the connection (raises IOError).

    Our own version of a9-v720's v720_ap.cap_live(). The original sends a
    HEARTBEAT (cmd 100) every 5 s; with some firmware the camera drops the
    connection exactly then. `keepalive` picks what we send every `interval`
    seconds: "none", "ping" (cmd 101) or "heartbeat" (cmd 100, original).

    The camera streams PCM audio (cmd 6) and JPEG chunks (cmd 1) mixed with
    replies, so replies are not read "in order" here - every packet just goes
    to on_packet and the caller picks what it needs.
    """
    import socket
    import cmd_udp
    from prot_udp import prot_udp

    a9_ap_send(cam, {"code": cmd_udp.CODE_FORWARD_OPEN_A_OPEN_V})    # start video
    keepalive_cmd = {"ping": cmd_udp.P2P_UDP_CMD_PING,
                     "heartbeat": cmd_udp.P2P_UDP_CMD_HEARTBEAT}.get(keepalive)
    last_keepalive = time.monotonic()

    while not (should_stop and should_stop()):
        if keepalive_cmd is not None and time.monotonic() - last_keepalive >= interval:
            cam._socket.send(prot_udp(cmd=keepalive_cmd).req())
            last_keepalive = time.monotonic()
        try:
            raw = cam._rcv()
        except socket.timeout:
            raise IOError("camera stopped sending (timeout)")
        except ConnectionError:
            raise                               # reset / closed by the camera
        except OSError as e:
            # a9-v720 raises IOError('Skip due to heartbeat') after it has
            # answered a heartbeat sent BY the camera - not an error.
            if "heartbeat" in str(e).lower():
                continue
            raise
        if not raw:
            raise IOError("camera closed the connection")
        pkg = prot_udp.resp(raw)
        if pkg is None:
            continue
        if pkg.cmd == cmd_udp.P2P_UDP_CMD_JSON:
            _log_late_base_info(pkg.payload)
        on_packet(pkg.cmd, pkg.payload)


def _log_late_base_info(payload):
    """The base-info reply (firmware version etc.) usually arrives after the
    handshake gave up waiting for it - catch it here and log it."""
    import json
    import cmd_udp
    try:
        content = json.loads(bytes(payload).decode("utf-8", "replace")).get("content", {})
    except ValueError:
        return
    if content.get("code") == cmd_udp.CODE_FORWARD_DEV_BASE_INFO:
        # The camera answers each of our (up to 3) queries - log it only once.
        version = content.get("version")
        if version and version != _log_late_base_info.last_version:
            _log_late_base_info.last_version = version
            log.info("a9v720: FIRMWARE VERSION %s", version)


_log_late_base_info.last_version = None


class A9APSource(FrameSource):
    """A9/V720 in AP mode: this computer is connected to the camera's 'Nax...'
    WiFi. Uses the a9-v720 project's protocol classes directly (it has no HTTP
    server in this mode)."""
    name = "a9v720-ap"

    def __init__(self, host, port, repo_path, ir_led=False, flip=False,
                 keepalive="none", **kw):
        super().__init__(**kw)
        self.host, self.port = host, port
        self.ir_led, self.flip = ir_led, flip
        self.keepalive = keepalive
        repo_path = os.path.abspath(repo_path)
        if not os.path.isdir(repo_path):
            raise FileNotFoundError(f"a9-v720 src folder not found: {repo_path}")
        sys.path.insert(0, repo_path)

    def _stream(self):
        # Imported here because these modules live in the a9-v720 checkout.
        import cmd_udp
        from log import log as a9_log
        from netcl_tcp import netcl_tcp
        from v720_ap import v720_ap

        # a9-v720 logs every packet in hex at DEBUG by default - that would
        # flood our log file several times per second.
        a9_log.set_log_lvl(logging.WARNING)
        jpg = JpegAssembler()

        def on_data(cmd, payload):
            if cmd == cmd_udp.P2P_UDP_CMD_JPEG:
                for data in jpg.feed(payload):
                    frame = decode_jpeg(data)
                    if frame is not None:
                        self._publish(frame)

        with netcl_tcp(self.host, self.port) as sock:
            cam = v720_ap(sock)
            a9_ap_handshake(cam)
            # Same JSON as a9-v720's cam.ir_led() / cam.flip(), sent without
            # waiting for the (out-of-order) replies. Only sent when switched
            # ON in config: sending "IrLed: 0" could stop the camera's own
            # automatic night vision.
            if self.ir_led:
                a9_ap_send(cam, {"code": cmd_udp.CODE_FORWARD_DEV_IR_LED, "IrLed": 1})
            if self.flip:
                a9_ap_send(cam, {"code": cmd_udp.CODE_FORWARD_DEV_VIDEO_FLIP, "mirrorFlip": 4})
            # blocks until the camera ends the stream; _run() then reconnects
            a9_ap_stream(cam, on_data, self.keepalive, should_stop=self._stop.is_set)


def make_source(cfg: dict, base_dir: str = ".") -> FrameSource:
    """Build the source named in config['source']['type']."""
    s = cfg["source"]
    kw = dict(reconnect_min=s.get("reconnect_min_seconds", 2),
              reconnect_max=s.get("reconnect_max_seconds", 60))
    t = s["type"]
    if t == "mjpeg":
        return MjpegSource(s["mjpeg"]["url"], **kw)
    if t == "rtsp":
        return OpenCVSource(s["rtsp"]["url"], **kw)
    if t == "file":
        return FileSource(os.path.join(base_dir, s["file"]["path"]),
                          loop=s["file"].get("loop", True), **kw)
    if t == "a9v720":
        a = s["a9v720"]
        if a.get("mode", "fakeserver") == "ap":
            return A9APSource(a["ap_host"], a["ap_port"],
                              os.path.join(base_dir, a["repo_path"]),
                              a.get("ir_led", False), a.get("flip", False),
                              a.get("ap_keepalive", "none"), **kw)
        return A9FakeServerSource(a["server_url"], a.get("camera_id", ""), **kw)
    raise ValueError(f"unknown source type: {t}")
