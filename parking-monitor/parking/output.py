"""Outputs: status JSON (atomic write, for iCloud Drive) and ntfy push."""

import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
from datetime import datetime

import requests

log = logging.getLogger(__name__)


def default_icloud_dir() -> str:
    """The iCloud Drive 'Parking' folder for this OS."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs", "Parking")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("USERPROFILE", home), "iCloudDrive", "Parking")
    # Linux / Raspberry Pi: there is no iCloud client. Write locally.
    return os.path.join(home, "parking-status")


def resolve_json_path(setting: str, fallback_dir: str = "status") -> str:
    if not setting or setting == "auto":
        icloud_root = os.path.dirname(default_icloud_dir())
        if sys.platform in ("darwin", "win32") and not os.path.isdir(icloud_root):
            # Don't create a fake "iCloudDrive" folder - it would not sync and
            # can confuse iCloud when it is installed later.
            log.warning("iCloud Drive not found at %s (is iCloud installed and signed in?). "
                        "Writing status to %s instead.", icloud_root, os.path.abspath(fallback_dir))
            return os.path.join(fallback_dir, "parking_status.json")
        return os.path.join(default_icloud_dir(), "parking_status.json")
    return os.path.expandvars(os.path.expanduser(setting))


def write_json_atomic(path: str, data: dict, retries: int = 5):
    """Write to a temp file in the same folder, then os.replace() it over the
    real file. os.replace is atomic, so iCloud (or a Shortcut reading the file)
    never sees a half-written file.

    On Windows the iCloud client sometimes holds the file open for a moment
    while syncing, so os.replace can fail with PermissionError - we retry.
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".parking_", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.5)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Notifier:
    """Push via ntfy (https://ntfy.sh). The iPhone ntfy app subscribes to the topic."""

    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        if "CHANGE-ME" in cfg["topic"]:
            # Never push to the example topic from config.yaml - it's public.
            log.warning("ntfy topic is still the example value - notifications disabled. "
                        "Set notifications.topic in config.yaml.")
            self.enabled = False
        self.url = f"{cfg.get('server', 'https://ntfy.sh').rstrip('/')}/{cfg['topic']}"
        self.token = cfg.get("token") or ""
        self.click_url = cfg.get("click_url") or ""   # tapping the notification opens this
        self._queue = queue.Queue()
        self._worker = None

    # Waits between attempts when ntfy can't be reached (about 4 minutes in
    # total) - e.g. right after a restart, before the network is ready.
    RETRY_DELAYS = (5, 15, 30, 60, 120)

    def send(self, message: str, title: str = "Parking", priority: str = "default",
             tags: str = "") -> bool:
        """Queue a notification; a background thread sends it and retries if
        the internet is down, so the monitor loop never waits for the network."""
        if not self.enabled:
            return False
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="ntfy", daemon=True)
            self._worker.start()
        self._queue.put((message, title, priority, tags))
        return True

    def _run(self):
        while True:
            item = self._queue.get()
            for delay in (0,) + self.RETRY_DELAYS:
                time.sleep(delay)
                if self.send_now(*item):
                    break
            else:
                log.error("notification dropped after retries: %s", item[0])

    def send_now(self, message: str, title: str = "Parking", priority: str = "default",
                 tags: str = "") -> bool:
        """Send immediately (one attempt). Returns True on success."""
        if not self.enabled:
            return False
        # HTTP headers must be latin-1, so only plain-ASCII text goes in the
        # title; the message body is UTF-8 and can contain anything.
        headers = {"Title": title.encode("ascii", "replace").decode(),
                   "Priority": priority}
        if tags:
            headers["Tags"] = tags      # ntfy turns tag names into emoji
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.click_url:
            headers["Click"] = self.click_url
        try:
            r = requests.post(self.url, data=message.encode("utf-8"),
                              headers=headers, timeout=10)
            r.raise_for_status()
            log.info("notification sent: %s", message)
            return True
        except requests.RequestException as e:
            # Never crash the monitor because the internet is down.
            log.warning("notification not sent yet (%s): %s", type(e).__name__, message)
            return False
