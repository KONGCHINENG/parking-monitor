"""Only one monitor may run: a second process must be refused while the first
holds the lock, and allowed again once the first one has exited."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from parking_monitor import single_instance
print("got" if single_instance(sys.argv[2]) else "refused", flush=True)
time.sleep(float(sys.argv[3]))
"""


def _start(lock, hold_s):
    return subprocess.Popen([sys.executable, "-c", HOLD, ROOT, lock, str(hold_s)],
                            stdout=subprocess.PIPE, text=True)


def test_second_instance_refused(tmp_path):
    lock = str(tmp_path / "monitor.lock")
    first = _start(lock, 5)
    assert first.stdout.readline().strip() == "got"

    second = _start(lock, 0)
    assert second.communicate(timeout=20)[0].strip() == "refused"

    first.kill()
    first.wait()
    third = _start(lock, 0)                    # lock released when the process ends
    assert third.communicate(timeout=20)[0].strip() == "got"
