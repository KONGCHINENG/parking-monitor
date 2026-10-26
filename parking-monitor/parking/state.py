"""Debounced state machines. Pure logic: no OpenCV, no I/O, time is passed in.

That makes them easy to unit test - the tests just feed in fake timestamps.

Think of each spot like a debounced switch input on a microcontroller:
the raw reading (marker seen / not seen) must stay stable for `confirm_s`
seconds before the output (FREE / OCCUPIED) changes.
"""

from dataclasses import dataclass, field
from typing import Optional

FREE = "free"
OCCUPIED = "occupied"
UNKNOWN = "unknown"     # just started, not confirmed yet

CAM_OK = "ok"
CAM_BLIND = "blind"     # frames arrive but the reference marker is not visible
CAM_OFFLINE = "offline" # no frames at all


@dataclass
class Change:
    """One confirmed state change, handed to the output/notification code."""
    name: str
    old: str
    new: str


@dataclass
class Spot:
    marker_id: int
    name: str
    state: str = UNKNOWN
    since: Optional[float] = None      # time of the last confirmed change
    pending: Optional[str] = None      # state we are currently "counting towards"
    pending_since: Optional[float] = None


class SpotTracker:
    def __init__(self, spots: dict, confirm_s: float):
        """spots: {marker_id: name}"""
        self.confirm_s = confirm_s
        self.spots = {mid: Spot(mid, name) for mid, name in spots.items()}

    def update(self, now: float, seen_ids: set, absence_trusted: bool) -> list:
        """Feed one observation. Returns a list of Change objects.

        seen_ids        - marker IDs detected in this frame
        absence_trusted - True only if a frame arrived AND the reference marker
                          was seen. If False we cannot tell "covered by a car"
                          from "too dark / lens blocked", so a missing marker
                          is NOT evidence of OCCUPIED.
        """
        changes = []
        for spot in self.spots.values():
            if spot.marker_id in seen_ids:
                observed = FREE        # a detected marker is positive proof
            elif absence_trusted:
                observed = OCCUPIED
            else:
                observed = None        # no usable information this time

            if observed is None or observed == spot.state:
                # Nothing new (or confirms the current state): cancel any
                # half-finished transition so the timer restarts from zero.
                spot.pending = None
                spot.pending_since = None
                continue

            if spot.pending != observed:
                # First frame of a possible change - start the timer.
                spot.pending = observed
                spot.pending_since = now
            elif now - spot.pending_since >= self.confirm_s:
                # Held long enough - commit the change.
                changes.append(Change(spot.name, spot.state, observed))
                spot.state = observed
                spot.since = now
                spot.pending = None
                spot.pending_since = None
        return changes

    def free_count(self) -> int:
        return sum(1 for s in self.spots.values() if s.state == FREE)


@dataclass
class CameraHealth:
    """Tracks whether the camera picture is usable, debounced for alerts.

    `status` is the reported (debounced) value used for notifications and JSON.
    The spot tracker does NOT wait for this debounce - it uses the raw
    per-frame value, so a single dark frame can never mark a spot occupied.
    """
    alert_s: float
    status: str = CAM_OK
    pending: Optional[str] = None
    pending_since: Optional[float] = field(default=None)

    def update(self, now: float, frame_ok: bool, ref_seen: bool) -> Optional[Change]:
        if not frame_ok:
            raw = CAM_OFFLINE
        elif not ref_seen:
            raw = CAM_BLIND
        else:
            raw = CAM_OK

        if raw == self.status:
            self.pending = None
            return None

        if self.pending != raw:
            self.pending = raw
            self.pending_since = now
            return None

        # Recovery is reported after the same delay as failure, which stops
        # flapping notifications when the picture is marginal.
        if now - self.pending_since >= self.alert_s:
            change = Change("camera", self.status, raw)
            self.status = raw
            self.pending = None
            return change
        return None
