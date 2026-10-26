"""State machine tests with simulated detections and fake time."""

from parking.state import (CAM_BLIND, CAM_OFFLINE, CAM_OK, FREE, OCCUPIED, UNKNOWN,
                           CameraHealth, SpotTracker)

SPOTS = {1: "A", 2: "B"}


def feed(tracker, seen, t_from, t_to, trusted=True, step=1.0):
    """Feed the same observation every `step` s from t_from to t_to (inclusive)."""
    changes, t = [], t_from
    while t <= t_to + 1e-9:
        changes += tracker.update(t, set(seen), trusted)
        t += step
    return changes


def test_starts_unknown_then_confirms_after_confirm_time():
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1, 2}, 0, 9)
    assert tr.spots[1].state == UNKNOWN              # 9 s < 10 s
    ch = feed(tr, {1, 2}, 10, 10)
    assert tr.spots[1].state == FREE and tr.spots[2].state == FREE
    assert {(c.name, c.old, c.new) for c in ch} == {("A", UNKNOWN, FREE), ("B", UNKNOWN, FREE)}


def test_car_arrives_needs_continuous_absence():
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1, 2}, 0, 10)
    # Marker 2 disappears at t=20 ...
    feed(tr, {1}, 20, 29)
    assert tr.spots[2].state == FREE
    ch = feed(tr, {1}, 30, 30)
    assert tr.spots[2].state == OCCUPIED
    assert [(c.name, c.new) for c in ch] == [("B", OCCUPIED)]
    assert tr.spots[1].state == FREE
    assert tr.free_count() == 1


def test_brief_occlusion_is_ignored():
    """A person walking over the marker for a few seconds must not flip it."""
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1, 2}, 0, 10)
    feed(tr, {1}, 11, 18)            # hidden 8 s
    feed(tr, {1, 2}, 19, 19)         # visible again -> timer reset
    feed(tr, {1}, 20, 28)            # hidden another 9 s (total > 10 but not continuous)
    assert tr.spots[2].state == FREE


def test_car_leaves():
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1}, 0, 10)
    assert tr.spots[2].state == OCCUPIED
    ch = feed(tr, {1, 2}, 11, 21)
    assert tr.spots[2].state == FREE
    assert [(c.old, c.new) for c in ch] == [(OCCUPIED, FREE)]


def test_camera_blind_never_flips_to_occupied():
    """Night/rain/blocked lens: no markers seen at all and no reference marker."""
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1, 2}, 0, 10)
    ch = feed(tr, set(), 11, 500, trusted=False)      # blind for ~8 minutes
    assert ch == []
    assert tr.spots[1].state == FREE and tr.spots[2].state == FREE


def test_blind_period_does_not_count_towards_confirm_time():
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1, 2}, 0, 10)
    feed(tr, {1}, 11, 18)                       # 7 s of real absence of B
    feed(tr, set(), 19, 60, trusted=False)      # camera blind - resets timer
    feed(tr, {1}, 61, 69)                       # only 8 s since camera recovered
    assert tr.spots[2].state == FREE
    feed(tr, {1}, 70, 71)
    assert tr.spots[2].state == OCCUPIED


def test_visible_marker_can_free_a_spot_even_when_reference_missing():
    """Seeing a spot marker is positive proof the spot is free."""
    tr = SpotTracker(SPOTS, confirm_s=10)
    feed(tr, {1}, 0, 10)
    assert tr.spots[2].state == OCCUPIED
    feed(tr, {2}, 11, 21, trusted=False)
    assert tr.spots[2].state == FREE
    assert tr.spots[1].state == FREE


def test_camera_health_debounce_and_recovery():
    h = CameraHealth(alert_s=30)
    assert h.update(0, True, True) is None
    # blind for 29 s: no alert yet
    for t in range(1, 30):
        assert h.update(t, True, False) is None
    ch = h.update(31, True, False)
    assert ch and (ch.old, ch.new) == (CAM_OK, CAM_BLIND)
    # goes offline
    h.update(40, False, False)
    ch = h.update(71, False, False)
    assert ch and ch.new == CAM_OFFLINE
    # one good frame does not count as recovered
    assert h.update(72, True, True) is None
    h.update(80, False, False)
    assert h.status == CAM_OFFLINE
    # stable recovery
    h.update(90, True, True)
    ch = h.update(121, True, True)
    assert ch and (ch.old, ch.new) == (CAM_OFFLINE, CAM_OK)
