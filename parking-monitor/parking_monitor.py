#!/usr/bin/env python3
"""Parking spot occupancy monitor.

    python parking_monitor.py run [--show]        start monitoring
    python parking_monitor.py gen [--size-cm 30]  make printable markers
    python parking_monitor.py probe 192.168.1.23  look for a local stream on a camera
    python parking_monitor.py test-notify         send a test push to your phone
"""

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_path, verbose=False):
    if not os.path.isabs(log_path):
        log_path = os.path.join(HERE, log_path)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # 1 MB per file, keep 5 old ones -> the log can never fill the disk.
    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    handlers = [fh]
    if sys.stderr is not None:        # pythonw.exe (Windows service) has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        handlers.append(sh)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


_lock_file = None   # kept open for the whole run - closing it releases the lock


def single_instance(path):
    """Return True if no other monitor is running, else False.

    The camera only accepts one connection, so two monitors would keep
    kicking each other off. We hold an OS lock on a small file; the OS
    releases it automatically when the process ends, even after a crash."""
    global _lock_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _lock_file = open(path, "a+")
    _lock_file.seek(0)          # every process must lock the same byte
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=os.path.join(HERE, "config.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the monitor")
    r.add_argument("--show", action="store_true", help="debug window (press q to quit)")
    r.add_argument("--lan", action="store_true",
                   help="open the app page to the local network (offline demo on the camera WiFi)")
    r.add_argument("--source", choices=["mjpeg", "rtsp", "a9v720", "file"],
                   help="override source.type from config (file = sample video)")
    r.add_argument("--no-notify", action="store_true", help="don't send push notifications")

    g = sub.add_parser("gen", help="generate marker PNGs + PDF")
    g.add_argument("--size-cm", type=float, default=30.0, help="black square edge length")
    g.add_argument("--dpi", type=int, default=150)
    g.add_argument("--paper", default="fit", choices=["fit", "a4", "letter", "a3"],
                   help="fit = one page per marker at full size; a4/letter/a3 = tiles to tape together")
    g.add_argument("--ids", type=int, nargs="*", help="marker IDs (default: spots + reference from config)")
    g.add_argument("--out", default=os.path.join(HERE, "markers"))

    pr = sub.add_parser("probe", help="scan one camera IP for local streams")
    pr.add_argument("ip")

    sub.add_parser("test-notify", help="send a test notification")

    args = p.parse_args()

    if args.cmd == "probe":                    # needs no config
        from parking.probe import probe
        probe(args.ip)
        return

    cfg = load_config(args.config)
    # where the status JSON goes if iCloud Drive is not installed
    cfg["output"].setdefault("fallback_dir", os.path.join(HERE, "status"))

    if args.cmd == "gen":
        from parking.markers import generate
        if args.ids:
            labels = {i: "" for i in args.ids}
        else:
            labels = {int(k): v for k, v in cfg["spots"].items()}
            labels[int(cfg["reference_marker_id"])] = "REFERENCE"
        for f in generate(labels, args.out, args.size_cm, args.dpi, args.paper,
                          cfg["detection"].get("dictionary", "DICT_4X4_50")):
            print("wrote", f)
        return

    setup_logging(cfg["output"].get("log_path", "logs/parking.log"), args.verbose)

    if args.cmd == "test-notify":
        from parking.output import Notifier
        ok = Notifier(cfg["notifications"]).send_now("Test from parking monitor", tags="tada")
        print("sent" if ok else "FAILED - see log")
        return

    if not single_instance(os.path.join(HERE, "logs", "monitor.lock")):
        msg = ("Another parking monitor is already running (probably the background task).\n"
               "Only one can use the camera. Stop the other one first, e.g.:\n"
               "    Stop-ScheduledTask ParkingMonitor")
        logging.getLogger(__name__).error(msg.replace("\n", " "))
        print("\n" + msg)
        sys.exit(1)

    if args.source:
        cfg["source"]["type"] = args.source
    if args.no_notify:
        cfg["notifications"]["enabled"] = False

    from parking.monitor import run
    from parking.sources import make_source
    logging.getLogger(__name__).info("starting, source=%s", cfg["source"]["type"])
    run(cfg, make_source(cfg, HERE), show=args.show, lan=args.lan)


if __name__ == "__main__":
    main()
