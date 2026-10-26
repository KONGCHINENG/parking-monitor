"""Notifications are retried in the background when the internet is down
(e.g. right after a restart) and delivered exactly once when it comes back."""

import time

import requests

from parking import output
from parking.output import Notifier


class FlakyPost:
    """Fails the first `fails` calls like a missing network, then succeeds."""

    def __init__(self, fails):
        self.fails, self.calls, self.delivered = fails, 0, []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fails:
            raise requests.ConnectionError("Failed to resolve 'ntfy.sh'")
        self.delivered.append(data.decode())

        class Ok:
            def raise_for_status(self):
                pass
        return Ok()


def test_retries_until_delivered(monkeypatch):
    post = FlakyPost(fails=2)
    monkeypatch.setattr(output.requests, "post", post)
    n = Notifier({"topic": "parking-test", "enabled": True})
    n.RETRY_DELAYS = (0.05, 0.05, 0.05)

    assert n.send("Spot A is FREE") is True          # returns at once, sends in background
    end = time.time() + 5
    while not post.delivered and time.time() < end:
        time.sleep(0.02)
    assert post.delivered == ["Spot A is FREE"] and post.calls == 3


def test_send_now_single_attempt(monkeypatch):
    post = FlakyPost(fails=1)
    monkeypatch.setattr(output.requests, "post", post)
    n = Notifier({"topic": "parking-test", "enabled": True})
    assert n.send_now("x") is False and post.calls == 1   # used by `test-notify`
