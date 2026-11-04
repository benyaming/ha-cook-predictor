"""The delivered-alert ledger: occurrences, latches and the outbox."""

from custom_components.cook_predictor.core import ledger as L

LK = L.latch_key("1", "alert", "2026-01-01T00:00:00.000Z", 65)


def test_a_delivered_alert_holds_its_latch_until_it_rearms() -> None:
    led = L.new_ledger()
    occ = L.note_pending(led, LK, 100, {"title": "x"})
    assert L.holds(led, LK)
    L.note_sent(led, occ)
    assert L.holds(led, LK)
    assert L.note_rearmed(led, LK, 150)
    assert not L.holds(led, LK)
    assert led["occ"] == {}


def test_finishing_the_first_send_does_not_mark_a_second_occurrence_sent() -> None:
    led = L.new_ledger()
    first = L.note_pending(led, LK, 100, {"n": 1})
    L.note_rearmed(led, LK, 150)  # fell under 62 while the first send was still out
    second = L.note_pending(led, LK, 200, {"n": 2})
    L.note_sent(led, first)
    assert led["occ"][second]["state"] == "pending"
    assert L.pending(led, 210) == ([(second, {"n": 2})], [])


def test_a_rearm_from_older_history_does_not_release_a_newer_delivery() -> None:
    led = L.new_ledger()
    occ = L.note_pending(led, LK, 200, {})
    L.note_sent(led, occ)
    assert not L.note_rearmed(led, LK, 150)
    assert L.holds(led, LK)


def test_an_unrearmed_latch_is_kept_however_old() -> None:
    led = L.new_ledger()
    old = L.note_pending(led, LK, 0, {})
    L.note_sent(led, old)
    other = L.latch_key("2", "done", "s", 93)
    L.note_sent(led, L.note_pending(led, other, 30 * 86400, {}))
    assert L.holds(led, LK)
    assert L.release(led, lambda lk: not lk.startswith("1|"))  # channel 1 retired
    assert not L.holds(led, LK)
    assert L.holds(led, other)


def test_a_rearm_voids_an_occurrence_still_waiting_to_go_out() -> None:
    led = L.new_ledger()
    first = L.note_pending(led, LK, 100, {"n": 1})  # its send failed: pending for a retry
    assert L.note_rearmed(led, LK, 110)  # what it announced is no longer so
    second = L.note_pending(led, LK, 120, {"n": 2})  # a new crossing, its own occurrence
    L.note_sent(led, second)
    assert L.pending(led, 131) == ([], [])  # the first is not revived by the second's latch
    assert first not in led["occ"]


def test_pending_occurrences_after_a_restart() -> None:
    led = L.new_ledger()
    fresh = L.note_pending(led, LK, 1000, {"n": 1})
    old_lk = L.latch_key("2", "done", "s", 93)
    old = L.note_pending(led, old_lk, 100, {"n": 2})
    assert L.pending(led, 1300) == ([(fresh, {"n": 1})], [old])
    L.drop(led, old)
    assert L.holds(led, old_lk)  # it may have been delivered: never sent again
    assert L.pending(led, 1300) == ([(fresh, {"n": 1})], [])
