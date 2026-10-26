"""AP-mode camera test. Runs fully offline and writes everything to ap_test_out/.

    1. Join this PC's WiFi to the camera's "Nax_..." network.
    2. python tools/ap_test.py            (takes ~2-3 minutes)
    3. Re-join your normal WiFi and look at ap_test_out/report.txt

It checks open ports, then opens the live stream three times with a
different keepalive each time ("none", "ping", "heartbeat") to find out which
one keeps this camera's firmware streaming. For each it reports how long the
stream lasted, frames, frame size and visible ArUco markers.
"""

import contextlib
import io
import logging
import os
import sys
import time
import traceback

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from parking.detector import MarkerDetector  # noqa: E402
from parking.probe import probe  # noqa: E402
from parking.sources import (JpegAssembler, a9_ap_handshake, a9_ap_stream,  # noqa: E402
                             decode_jpeg)

HOST, PORT = "192.168.169.1", 6123
SESSION_SECONDS = 40                        # max length of each keepalive trial
MODES = ["none", "ping", "heartbeat"]
A9_SRC = os.path.join(ROOT, "..", "a9-v720", "src")
OUT = os.path.join(ROOT, "ap_test_out")
CONSOLE = sys.__stdout__                    # never redirected

report = []


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, file=CONSOLE, flush=True)
    report.append(line)


class Done(Exception):
    """Ends a trial when the time limit is reached."""


def trial(mode, netcl_tcp, v720_ap, cmd_udp, det):
    """One connection with one keepalive mode. Returns a result dict."""
    res = {"mode": mode, "frames": 0, "secs": 0.0, "shape": None, "markers": {},
           "end": "time limit reached", "other": {}}
    jpg = JpegAssembler()
    t0 = [None]

    def on_packet(cmd, payload):
        now = time.time()
        if cmd == cmd_udp.P2P_UDP_CMD_JPEG:
            for data in jpg.feed(payload):
                frame = decode_jpeg(data)
                if frame is None:
                    continue
                res["frames"] += 1
                if t0[0] is None:
                    t0[0], res["shape"] = now, frame.shape
                    cv2.imwrite(os.path.join(OUT, f"snapshot_{mode}.jpg"), frame)
                if res["frames"] % 5 == 0:
                    for mid, c in det.detect(frame).items():
                        edge = min(((c[i] - c[(i + 1) % 4]) ** 2).sum() ** 0.5 for i in range(4))
                        res["markers"].setdefault(mid, []).append(edge)
        elif cmd != cmd_udp.P2P_UDP_CMD_PCM:
            # control messages (not audio/video) - log them, they may explain a drop
            res["other"][cmd] = res["other"].get(cmd, 0) + 1
            logging.info("[%s] packet cmd=%s payload=%r", mode, cmd, bytes(payload[:2000]))
        if t0[0] is not None:
            res["secs"] = now - t0[0]
            if res["secs"] >= SESSION_SECONDS:
                raise Done

    cam_out = io.StringIO()      # a9-v720 print()s some things - keep them
    try:
        with contextlib.redirect_stdout(cam_out):
            with netcl_tcp(HOST, PORT) as sock:
                cam = v720_ap(sock)
                info = a9_ap_handshake(cam)
                if info.get("version"):
                    res["firmware"] = info["version"]
                a9_ap_stream(cam, on_packet, keepalive=mode)
    except Done:
        pass
    except Exception as e:
        res["end"] = f"{type(e).__name__}: {e}"
        logging.info("[%s] ended:\n%s", mode, traceback.format_exc())
    if cam_out.getvalue().strip():
        logging.info("[%s] a9-v720 printed: %s", mode, cam_out.getvalue().strip())
    return res


def main():
    os.makedirs(OUT, exist_ok=True)
    fh = logging.FileHandler(os.path.join(OUT, "debug.log"), mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.root.addHandler(fh)
    logging.root.setLevel(logging.INFO)     # INFO: no per-packet hex flood

    say("AP-mode keepalive test", time.strftime("%Y-%m-%d %H:%M:%S"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        probe(HOST)
    if "6123 OPEN" not in buf.getvalue():
        say(buf.getvalue())
        say("Camera port 6123 not reachable - is this PC joined to the Nax_ WiFi?")
        return finish()
    say("Camera reachable on port 6123.")

    sys.path.insert(0, os.path.abspath(A9_SRC))
    import cmd_udp
    from log import log as a9_log
    from netcl_tcp import netcl_tcp
    from v720_ap import v720_ap
    a9_log.set_log_lvl(logging.INFO)

    det = MarkerDetector()
    results = []
    for mode in MODES:
        say(f"\nTrial keepalive={mode} (up to {SESSION_SECONDS} s) ...")
        r = trial(mode, netcl_tcp, v720_ap, cmd_udp, det)
        results.append(r)
        fps = r["frames"] / r["secs"] if r["secs"] > 0 else 0
        say(f"  streamed {r['secs']:.1f} s, {r['frames']} frames (~{fps:.1f} fps), "
            f"size {r['shape'][1]}x{r['shape'][0]}" if r["shape"] else "  no frames")
        say(f"  ended: {r['end']}")
        if r["other"]:
            say(f"  control packets seen (cmd: count): {r['other']}")
        for mid, edges in sorted(r["markers"].items()):
            say(f"  marker {mid}: seen {len(edges)}x, smallest edge {min(edges):.0f}px (want >= 25)")
        if r.get("firmware"):
            say(f"  FIRMWARE VERSION: {r['firmware']}")
        time.sleep(3)                    # let the camera settle between trials

    say("\n=== RESULT ===")
    best = max(results, key=lambda r: r["secs"])
    for r in results:
        say(f"  {r['mode']:9s}: {r['secs']:5.1f} s")
    full = [r["mode"] for r in results if r["secs"] >= SESSION_SECONDS - 1]
    if len(full) == len(results):
        say("All modes streamed the full time -> keep ap_keepalive: none.")
    elif full:
        say(f"Modes that kept the stream alive: {', '.join(full)} -> use ap_keepalive: {full[0]}")
    elif best["frames"]:
        say(f"No mode streams continuously; longest = {best['mode']} ({best['secs']:.0f} s). "
            "The monitor can still work by reconnecting automatically.")
    else:
        say("NO VIDEO in any trial. See debug.log.")
    finish()


def finish():
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    say(f"\nReport saved: {os.path.join(OUT, 'report.txt')}")


if __name__ == "__main__":
    main()
