"""Regression tests for the estimator, one per case of the JavaScript suite it was ported
with (each case is a failure found in review), under the same names and assertions.

The JavaScript suite also tested the Node-RED glue through vm; those cases have no
counterpart in the estimator and are not ported: "the glue does not stamp the replay with
processing time", "published attributes never contain null and are never empty",
"history arriving after the live fallback is ignored", "a heartbeat with an unseen change
ingests it at its own last_changed", "after a restart, alerts crossed in history stay sent
and a new crossing alerts once", "new code keeps the running cook until its history
replaces it" and "with no history for new code, the running cook carries on".
"""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

from custom_components.cook_predictor.core import estimator as E

FIXTURES = Path(__file__).parent.parent / "fixtures" / "estimator"


def iso(t: float) -> str:
    """new Date(t * 1000).toISOString()"""
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(s: str) -> float:
    """Date.parse(s) / 1000"""
    return datetime.fromisoformat(s).timestamp()


def q18(x: float) -> float:
    """Math.round(x * 18) / 18: the Inkbird's 0.1 °F steps."""
    return E._js_round(x * 18) / 18


class Cooked(NamedTuple):
    temp: float
    r: E.Result | None
    events: list[str]
    fired: list[tuple[float, str, dict[str, Any]]]


def cook(
    st: E.State,
    *,
    from_: int = 0,
    to: int,
    T0: float,
    k: float | Callable[[float], float] | None = None,
    A: float = 115,
    dt: int = 30,
    key: str = "1",
    rate: float | None = None,
    target: float = 93,
    alerts: dict[str, Any] | None = None,
    pit: Callable[[float], float] | None = None,
) -> Cooked:
    """Heating sampled every dt s and quantised like the Inkbird, with heartbeats on repeated
    values and an evaluation per step as the live node does. Exact Newton heating at k, or a
    constant rate in °C/h. Returns the final temperature, last result and all events."""
    temp = T0
    p = st["probes"][key]
    last = p["samples"][-1][1] if p["samples"] else None
    r = None
    events: list[str] = []
    fired: list[tuple[float, str, dict[str, Any]]] = []
    for t in range(from_, to + 1, dt):
        kk = k(t) if callable(k) else k  # k(t): a probe whose heating rate drifts
        if t > from_:
            if rate is not None:
                temp = temp + (rate * dt) / 3600
            else:
                temp = A + (temp - A) * math.exp(-kk * dt)
        q = q18(temp)
        # pit(t): the pit probe's reading, NaN = unplugged
        E.ingest_ambient(st, t, pit(t) if pit else A)
        if q != last:
            E.ingest_probe(st, key, t, q)
            last = q
        else:
            E.report(st, key, t, q)
        r = E.evaluate(st, key, t, target, alerts)
        events.extend(r["events"])
        fired.extend((t, e, r["attrs"]) for e in r["events"])
    return Cooked(temp, r, events, fired)


def test_a_stale_probe_stops_predicting_instead_of_counting_down_an_old_eta() -> None:
    st = E.new_state(["1"])
    cook(st, to=7200, T0=5, k=1e-4)
    live = E.evaluate(st, "1", 7200, 93)
    assert live["status"] == "predicting"
    assert live["attrs"].get("done_at")
    later = E.evaluate(st, "1", 7200 + 24 * 3600, 93)
    assert later["status"] == "stale"
    assert "done_at" not in later["attrs"]


def test_heartbeats_keep_a_flat_reading_fresh_and_move_the_finish_time() -> None:
    st = E.new_state(["1"])
    cook(st, to=7200, T0=5, k=1e-4)
    before = E.evaluate(st, "1", 7200, 93)
    last = st["probes"]["1"]["samples"][-1]
    E.report(st, "1", 7200 + 300, last[1])
    E.ingest_ambient(st, 7200 + 300, 115)
    after = E.evaluate(st, "1", 7200 + 300, 93)
    assert after["status"] != "stale"
    assert after["attrs"]["observed_at"] == iso(7200 + 300)
    assert parse(after["attrs"]["done_at"]) > parse(before["attrs"]["done_at"])


def test_a_long_nearly_flat_stall_stays_a_stall() -> None:
    st = E.new_state(["1"])
    warm = cook(st, to=7200, T0=5, k=1.5e-4)
    assert warm.r["status"] == "predicting"
    flat = cook(st, from_=7260, to=7200 + 4 * 3600, T0=warm.temp, rate=0.072, dt=60)
    assert "stall" in flat.events, f"events: {','.join(flat.events)}"
    assert flat.r["status"] == "stall"
    assert flat.r["attrs"].get("stall_since")


def test_moving_the_probe_relearns_k_no_false_stall_afterwards() -> None:
    st = E.new_state(["1"])
    cook(st, to=3600, T0=5, k=2e-4, A=120)
    E.ingest_probe(st, "1", 3630, 45)  # repositioned into a cooler spot
    after = cook(st, from_=3660, to=3600 + 3 * 3600, T0=45, k=5e-5, A=120)
    assert "stall" not in after.events, f"events: {','.join(after.events)}"
    assert after.r["status"] == "predicting"


def test_repeated_evaluation_of_one_observation_adds_one_reference_k_entry() -> None:
    st = E.new_state(["1"])
    cook(st, to=7200, T0=5, k=1e-4)
    n = len(st["probes"]["1"]["k_hist"])
    assert n > 0
    for _ in range(50):
        E.evaluate(st, "1", 7200, 93)
    assert len(st["probes"]["1"]["k_hist"]) == n


def test_warming_publishes_no_eta() -> None:
    # A cold core lags, then accelerates: the model has estimates, the status must not show them.
    st = E.new_state(["1"])
    for t in range(0, 2401, 30):
        E.ingest_ambient(st, t, 115)
        E.ingest_probe(st, "1", t, q18(3 + 1e-6 * t * t))
        r = E.evaluate(st, "1", t, 93)
    assert r["status"] == "warming"
    a = r["attrs"]
    assert a.get("eta_newton_min") is not None or a.get("eta_linear_min") is not None
    assert "eta_min" not in r["attrs"]
    assert "done_at" not in r["attrs"]


def test_new_cook_starts_a_session_at_the_press_that_waits_for_a_real_reading() -> None:
    st = E.new_state(["1"])
    r = cook(st, to=3600, T0=80, k=3e-4).r
    assert r["status"] == "done"
    v = st["probes"]["1"]["samples"][-1][1]
    E.reset_probe(st, "1", 3700)
    waiting = E.evaluate(st, "1", 3700, 93)
    assert waiting["status"] == "warming"
    assert waiting["attrs"]["session_start"] == iso(3700)
    assert "observed_at" not in waiting["attrs"]
    E.report(st, "1", 3720, v)  # the device's next report, after the press
    assert st["probes"]["1"]["samples"] == [[3720, v]]


def test_new_cook_does_not_manufacture_an_observation_from_a_stale_reading() -> None:
    st = E.new_state(["1"])
    E.ingest_ambient(st, 0, 115)
    E.ingest_probe(st, "1", 0, 94)
    assert E.evaluate(st, "1", 3600, 93)["status"] == "stale"
    E.reset_probe(st, "1", 3600)
    r = E.evaluate(st, "1", 3600, 93)
    assert r["status"] != "done"
    assert r["events"] == []


def test_a_reading_changed_just_before_the_press_and_reported_after_it_starts_the_session() -> None:
    st = E.new_state(["1"])
    E.ingest_probe(st, "1", 0, 50)
    E.reset_probe(st, "1", 100)
    E.ingest_probe(st, "1", 99, 51)  # the heartbeat's last_changed, before the press
    E.report(st, "1", 110, 51)  # and its last_reported, after it
    assert st["probes"]["1"]["samples"] == [[110, 51]]


def test_a_live_silence_is_not_bridged_no_stall_dated_from_before_the_gap() -> None:
    st = E.new_state(["1"])
    warm = cook(st, to=7200, T0=5, k=1.5e-4)
    flat = cook(st, from_=7260, to=8700, T0=warm.temp, rate=0, dt=60)
    assert st["probes"]["1"]["stall_cand"] is not None, "expected a stall candidate before the gap"
    assert "stall" not in flat.events
    assert E.evaluate(st, "1", 8700 + 3600, 93)["status"] == "stale"
    v = st["probes"]["1"]["samples"][-1][1]
    E.ingest_ambient(st, 15660, 115)
    E.report(st, "1", 15660, v)
    r = E.evaluate(st, "1", 15660, 93)
    assert r["events"] == []
    assert r["status"] == "unstable"
    assert r["attrs"]["reason"] == "readings resumed"


def test_a_drop_right_after_a_confirmed_flat_stretch_counts_as_a_move() -> None:
    st = E.new_state(["1"])
    warm = cook(st, to=7200, T0=5, k=1.5e-4)
    cook(st, from_=7230, to=7200 + 1200, T0=warm.temp, rate=0)
    v = st["probes"]["1"]["samples"][-1][1]
    E.ingest_probe(st, "1", 7200 + 1230, v - 10)
    assert st["probes"]["1"]["moved_at"] == 7200 + 1230
    assert st["probes"]["1"]["k_peak"] is None
    assert E.evaluate(st, "1", 7200 + 1230, 93)["status"] == "unstable"


def history(flat_rate: float) -> list[list[Any]]:
    """Changes-only history, as the recorder returns it: one pit row, a Newton warm-up, then
    a meat trace that is either nearly flat or exactly flat to the last row."""
    rows: list[list[Any]] = [["ambient", 0, 115]]
    temp = 5.0
    last = None
    for t in range(0, 7200 + 4 * 3600 + 1, 30):
        if t > 0:
            if t <= 7200:
                temp = 115 + (temp - 115) * math.exp(-1.5e-4 * 30)
            else:
                temp = temp + (flat_rate * 30) / 3600
        q = q18(temp)
        if q != last:
            rows.append(["1", t, q])
            last = q
    rows.append(["ambient", 7200 + 4 * 3600, 115.05])  # the newest recorded row ends the replay
    return sorted(rows, key=lambda row: row[1])


def replay_collecting(rows: list[list[Any]]) -> tuple[E.State, list[str], set[str]]:
    st = E.new_state(["1"])
    events: list[str] = []
    models: set[str] = set()

    def on_eval(k: str, t: float, r: E.Result) -> None:
        events.extend(r["events"])
        if 1800 < t < 7200 and r["status"] == "predicting":
            models.add(r["attrs"]["model"])

    E.replay(st, rows, lambda k, t: 93, 60, on_eval)
    return st, events, models


def test_replay_of_changes_only_history_one_pit_row_keeps_newton_and_finds_a_flat_stall() -> None:
    for flat_rate in (0.072, 0):
        st, events, models = replay_collecting(history(flat_rate))
        assert models == {"newton"}
        assert st["probes"]["1"]["k_peak"] > 0
        assert "stall" in events, f"rate {flat_rate}: events {','.join(events)}"


def test_after_a_replay_nothing_is_fresh_until_a_real_reading_and_an_old_heartbeat_stays_stale():
    st = E.new_state(["1"])
    E.replay(st, [["ambient", 0, 115], ["1", 0, 94]], lambda k, t: 93, 60)
    r = E.evaluate(st, "1", 3600, 93)
    assert r["status"] == "stale"
    assert r["attrs"]["reason"] == "waiting for a reading"
    assert r["events"] == []
    E.report(st, "1", 30, 94)  # a heartbeat whose last_reported is an hour old
    E.report(st, "ambient", 30, 115)
    assert E.evaluate(st, "1", 3600, 93)["status"] == "stale"


def newton_history(end_pit: float) -> list[list[Any]]:
    rows: list[list[Any]] = [["ambient", 0, 120]]
    last = None
    for t in range(0, 7201, 30):
        q = q18(120 - 100 * math.exp(-1e-4 * t))
        if q != last:
            rows.append(["1", t, q])
            last = q
    rows.append(["ambient", end_pit, 120.01])
    return rows


def test_a_gap_beyond_the_replay_coverage_is_not_bridged_by_the_first_live_reading() -> None:
    st = E.new_state(["1"])
    E.replay(st, newton_history(8460), lambda k, t: 110, 60)
    v = st["probes"]["1"]["samples"][-1][1]
    E.report(st, "1", 15660, v)
    E.report(st, "ambient", 15660, 120.01)
    r = E.evaluate(st, "1", 15660, 110)
    assert "stall" not in r["events"], f"events {','.join(r['events'])}"
    assert st["probes"]["1"]["gap_at"] == 15660
    assert st["ambient_gap_at"] == 15660


def test_a_flat_stretch_covered_by_the_history_query_is_not_a_gap() -> None:
    st = E.new_state(["1"])
    E.replay(st, newton_history(8460), lambda k, t: 110, 60, None, 15640)
    v = st["probes"]["1"]["samples"][-1][1]
    E.report(st, "1", 15660, v)
    E.report(st, "ambient", 15660, 120.01)
    E.evaluate(st, "1", 15660, 110)
    assert st["probes"]["1"]["gap_at"] is None
    assert st["ambient_gap_at"] is None


def moved_after_flat(drop_at: int, cover_end: int, live: bool) -> tuple[float | None, E.Result]:
    """A Newton cook, a stall long enough to be detected, then the probe moved to 61 C, all
    in recorded history. Then exact Newton heating from 61 C at a quarter of the old k."""
    st = E.new_state(["1"])
    rows = [row for row in newton_history(7200) if not (row[0] == "ambient" and row[1] == 7200)]
    if not live:
        rows.append(["1", drop_at, 61])
    E.replay(st, rows, lambda k, t: 110, 60, None, cover_end)
    temp = 61.0
    if live:
        E.ingest_ambient(st, drop_at, 120)
        E.ingest_probe(st, "1", drop_at, 61)
    moved_at = st["probes"]["1"]["moved_at"]
    for t in range(drop_at + 30, drop_at + 7201, 30):
        temp = 120 + (temp - 120) * math.exp(-2.5e-5 * 30)
        E.ingest_ambient(st, t, 120)
        E.ingest_probe(st, "1", t, q18(temp))
        r = E.evaluate(st, "1", t, 110)
    return moved_at, r


def test_a_drop_after_a_recorded_flat_stretch_is_a_move_in_replay_and_live_after_it() -> None:
    for c in (
        {"drop_at": 10020, "cover_end": 10020, "live": False},
        {"drop_at": 10020, "cover_end": 10000, "live": True},
    ):
        moved_at, r = moved_after_flat(**c)
        assert moved_at == 10020, json.dumps(c)
        assert r["status"] != "stall", json.dumps(c)


def test_the_held_value_does_not_carry_across_an_unavailable_stretch_as_a_move_baseline() -> None:
    st = E.new_state(["1"])
    E.ingest_ambient(st, 0, 120)
    E.ingest_probe(st, "1", 0, 70)
    E.report(st, "1", 390, 70)  # confirmed held at 390
    E.ingest_probe(st, "1", 400, math.nan)  # then unavailable for a minute
    E.ingest_probe(st, "1", 460, 60)  # no real sample within jump_s; the held 70 must not count
    assert st["probes"]["1"]["moved_at"] is None


def test_a_target_crossing_in_the_last_partial_replay_interval_sends_no_startup_alert() -> None:
    st = E.new_state(["1"])
    E.replay(st, [["ambient", 0, 115], ["1", 0, 90], ["1", 31, 94]], lambda k, t: 93, 60)
    E.report(st, "1", 40, 94)
    E.report(st, "ambient", 40, 115)
    r = E.evaluate(st, "1", 40, 93)
    assert r["status"] == "done"
    assert r["events"] == []


def test_no_newton_fit_spans_a_pit_outage() -> None:
    st = E.new_state(["1"])
    temp = 5.0
    for t in range(0, 6601, 30):
        temp = 120 + (temp - 120) * math.exp(-1e-4 * 30) if t else temp
        if t < 3630 or t >= 6000:
            E.ingest_ambient(st, t, 120)
        elif t == 3630:
            E.ingest_ambient(st, t, math.nan)  # pit unplugged
        E.ingest_probe(st, "1", t, q18(temp))
        r = E.evaluate(st, "1", t, 93)
        if t == 6030:
            assert r["attrs"].get("model") != "newton"
            assert r["attrs"].get("reason") == "pit readings resumed"
            assert r["attrs"].get("confidence") != "ok"
    assert r["attrs"]["model"] == "newton"  # 10 min of pit data after the gap
    assert parse(r["attrs"]["observed_at"]) - 6000 >= 600


# ---- the stall reference on a real cook ----
# The 2026-10-11 brisket (the real_brisket_1 golden: timestamps start at 2026-01-01T00:00Z,
# probes renamed): probe 2 (off-centre) went from k 1.4e-4 at 04:45 to about 4.5e-5 from 07:15
# while heating steadily. Replayed at target 75 the old reference (learned from the start)
# called a stall at 07:09; at 93 it learned nothing early at all.

REAL_COOK_START = 1767225600
# The cook as it ran: 93 until 2 h 58 min 59 s into the recording, then 75. The JavaScript
# read the time from the previous evaluation, as its targetOf had no time argument.
AS_RUN_SWITCH = REAL_COOK_START + 10739


def as_run(t: float) -> float:
    return 93 if t < AS_RUN_SWITCH else 75


@cache
def replay_saved(target: float | Callable[[float], float]) -> tuple[E.State, dict[str, set[str]]]:
    """target: a number, or a function of time."""
    with gzip.open(FIXTURES / "real_brisket_1.json.gz", "rt", encoding="utf-8") as f:
        golden = json.load(f)
    key_of = {entity: key for key, entity in golden["probes"].items()}
    key_of[golden["ambient"]] = "ambient"
    keyed = [[key_of[entity], t, v] for entity, t, v in golden["rows"]]
    st = E.new_state(["1", "2", "3"])
    statuses: dict[str, set[str]] = {"1": set(), "2": set()}

    def target_of(k: str, t: float) -> float:
        return target(t) if callable(target) else target

    def on_eval(k: str, t: float, r: E.Result) -> None:
        if k in statuses:
            statuses[k].add(r["status"])

    E.replay(st, keyed, target_of, 60, on_eval)
    return st, statuses


def test_steady_heating_after_a_fast_start_is_not_a_stall_real_cook() -> None:
    for target in (75, 93, as_run):
        _, statuses = replay_saved(target)
        assert "stall" not in statuses["1"] and "stall" not in statuses["2"], f"target {target}"
        assert "predicting" in statuses["2"]


def test_the_stall_reference_does_not_depend_on_the_target_while_it_is_above_the_readings_real_cook():  # noqa: E501
    a = replay_saved(75)[0]["probes"]
    b = replay_saved(93)[0]["probes"]
    c = replay_saved(as_run)[0]["probes"]
    for k in ("1", "2"):
        assert a[k]["k_peak"] > 0, f"probe {k} learned a reference"
        assert a[k]["k_peak"] == b[k]["k_peak"]
        assert a[k]["k_peak"] == c[k]["k_peak"]


def test_an_early_fast_stretch_does_not_become_the_stall_reference() -> None:
    # k falls from 1.6e-4 to 4.5e-5 over the first 2.5 h, then holds: no stall
    def k(t: float) -> float:
        return 1.6e-4 - ((1.6e-4 - 4.5e-5) * t) / 9000 if t < 9000 else 4.5e-5

    events = cook(E.new_state(["1"]), to=6 * 3600, T0=3, k=k, A=107, target=95).events
    assert events.count("stall") == 0


# ---- configurable alerts ----
# Exact Newton heating from 5 °C at k = 1e-4 in a 115 °C pit: 60 °C at ~6930 s, 93 °C at
# ~16090 s.


def first(fired: list[tuple[float, str, dict[str, Any]]], event: str) -> tuple | None:
    return next((f for f in fired if f[1] == event), None)


def test_the_heads_up_before_the_target_follows_its_setting_and_0_turns_it_off() -> None:
    hour = cook(E.new_state(["1"]), to=18000, T0=5, k=1e-4, alerts={"warn_min": 60})
    ap = [f for f in hour.fired if f[1] == "approach"]
    assert len(ap) == 1
    assert 55 <= ap[0][2]["eta_min"] <= 60, f"heads-up at {ap[0][2]['eta_min']} min"
    assert hour.events.count("done") == 1
    off = cook(E.new_state(["1"]), to=18000, T0=5, k=1e-4, alerts={"warn_min": 0})
    assert off.events.count("approach") == 0
    assert off.events.count("done") == 1
    dflt = cook(E.new_state(["1"]), to=18000, T0=5, k=1e-4)
    d = first(dflt.fired, "approach")
    assert d and 25 <= d[2]["eta_min"] <= 30


def test_the_second_alert_sends_its_heads_up_then_fires_once_when_reached() -> None:
    alerts = {"warn_min": 30, "alert_temp": 60, "alert_min": 20}
    cooked = cook(E.new_state(["1"]), to=18000, T0=5, k=1e-4, alerts=alerts)
    fired, events = cooked.fired, cooked.events
    assert events.count("alert_soon") == 1
    assert events.count("alert") == 1
    soon = first(fired, "alert_soon")
    hit = first(fired, "alert")
    assert soon[0] < hit[0]
    soon_min = soon[2]["alert_eta_min"]
    assert 15 <= soon_min <= 20, f"heads-up at {soon_min} min"
    assert soon[2]["alert_temp"] == 60
    assert abs(hit[0] - 6931) <= 60, f"reached at {hit[0]}"
    assert hit[2]["alert_reached_at"] == iso(hit[0])
    assert events.count("approach") == 1
    assert events.count("done") == 1


def test_a_second_alert_with_no_heads_up_fires_only_when_reached() -> None:
    alerts = {"alert_temp": 60, "alert_min": 0}
    events = cook(E.new_state(["1"]), to=9000, T0=5, k=1e-4, alerts=alerts).events
    assert events.count("alert_soon") == 0
    assert events.count("alert") == 1


def test_changing_a_setting_never_sends_an_alert_by_itself() -> None:
    st = E.new_state(["1"])
    base = {"warn_min": 30}
    run1 = cook(st, to=8000, T0=5, k=1e-4, alerts=base)
    assert run1.temp > 65
    # a second alert below the current temperature: already passed, no alert
    r = E.evaluate(st, "1", 8000, 93, {**base, "alert_temp": 60})
    assert len(r["events"]) == 0
    assert r["attrs"]["alert_temp"] == 60
    assert "alert_reached_at" not in r["attrs"]
    # a target below the current temperature: done, without an alert
    r = E.evaluate(st, "1", 8000, 65, {**base, "alert_temp": 60})
    assert r["status"] == "done"
    assert len(r["events"]) == 0
    # back to 93 with a heads-up longer than the time left: no heads-up now...
    r = E.evaluate(st, "1", 8000, 93, {**base, "alert_temp": 60})
    assert len(r["events"]) == 0
    long = {"warn_min": 180, "alert_temp": 60}
    r = E.evaluate(st, "1", 8000, 93, long)
    assert r["attrs"]["eta_min"] < 180
    assert len(r["events"]) == 0
    # ...and the rest of the cook sends only the target alert
    rest = cook(st, from_=8000, to=18000, T0=run1.temp, k=1e-4, alerts=long)
    assert rest.events == ["done"]


def test_shortening_a_heads_up_after_it_was_sent_arms_the_new_window() -> None:
    # target: 60 min sent at ~12490 s; at 13710 (~40 min left) change to 30 min
    st = E.new_state(["1"])
    alerts = {"warn_min": 60, "alert_temp": 80, "alert_min": 60}
    first_part = cook(st, to=13710, T0=5, k=1e-4, alerts=alerts)
    assert first_part.events.count("approach") == 1
    assert first_part.events.count("alert_soon") == 1
    alerts = {"warn_min": 30, "alert_temp": 80, "alert_min": 60}
    rest = cook(st, from_=13740, to=18000, T0=first_part.temp, k=1e-4, alerts=alerts)
    ap = [f for f in rest.fired if f[1] == "approach"]
    assert len(ap) == 1
    assert 25 <= ap[0][2]["eta_min"] <= 30, f"heads-up at {ap[0][2]['eta_min']} min"
    # the second alert: 60 min sent at ~7850 s; at 9050 (~40 min left) change to 30 min
    st2 = E.new_state(["1"])
    a1 = cook(st2, to=9050, T0=5, k=1e-4, alerts={"alert_temp": 80, "alert_min": 60})
    assert a1.events.count("alert_soon") == 1
    alerts = {"alert_temp": 80, "alert_min": 30}
    a2 = cook(st2, from_=9080, to=12000, T0=a1.temp, k=1e-4, alerts=alerts)
    soon = [f for f in a2.fired if f[1] == "alert_soon"]
    assert len(soon) == 1
    assert 25 <= soon[0][2]["alert_eta_min"] <= 30


def test_a_heads_up_changed_while_no_confident_estimate_exists_is_set_by_the_next_estimate():
    # 90 °C at ~14820 s. At 12000 (~47 min left) the pit probe is out and the lead changes.
    def pit_out(t: float) -> float:
        return math.nan if 12000 <= t < 12060 else 115

    for lead, sends in ((60, 0), (30, 1)):
        st = E.new_state(["1"])
        before = cook(st, to=11970, T0=5, k=1e-4, alerts={"alert_temp": 90, "alert_min": 0})
        alerts = {"alert_temp": 90, "alert_min": lead}
        after = cook(st, from_=12000, to=16000, T0=before.temp, k=1e-4, pit=pit_out, alerts=alerts)
        at_change = after.fired[0][0] if after.fired else None
        assert after.events.count("alert_soon") == sends, (
            f"lead {lead}: {json.dumps(after.events)} first at {at_change}"
        )
        assert after.events.count("alert") == 1


def test_a_second_temperature_at_or_above_the_target_alerts_only_when_reached() -> None:
    alerts = {"warn_min": 0, "alert_temp": 93, "alert_min": 30}
    plain = cook(E.new_state(["1"]), to=17000, T0=5, k=1e-4, target=60, alerts=alerts)
    assert plain.events == ["done", "alert"]

    # review repros: a pit outage after the target, and a move to 70 °C after it
    def pit_out(t: float) -> float:
        return math.nan if 13530 <= t < 14340 else 115

    outage = cook(E.new_state(["1"]), to=17000, T0=5, k=1e-4, target=60, alerts=alerts, pit=pit_out)
    assert outage.events == ["done", "alert"]
    st = E.new_state(["1"])
    first_part = cook(st, to=13500, T0=5, k=1e-4, target=60, alerts=alerts)
    moved = cook(st, from_=13530, to=22000, T0=70, k=1e-4, target=60, alerts=alerts)
    assert [*first_part.events, *moved.events] == ["done", "alert"]


def test_a_new_target_that_puts_the_second_temperature_below_it_sets_its_heads_up_silently():
    # second 80 °C (~11450 s), target 75 (~10120 s): no heads-up; at 10260 (~20 min to 80)
    # target 93
    st = E.new_state(["1"])
    alerts = {"warn_min": 0, "alert_temp": 80, "alert_min": 30}
    first_part = cook(st, to=10260, T0=5, k=1e-4, target=75, alerts=alerts)
    assert first_part.events == ["done"]
    rest = cook(st, from_=10290, to=13000, T0=first_part.temp, k=1e-4, target=93, alerts=alerts)
    assert rest.events == ["alert"]


def test_a_new_target_does_not_re_send_the_second_alert() -> None:
    st = E.new_state(["1"])
    alerts = {"alert_temp": 60}
    run1 = cook(st, to=8000, T0=5, k=1e-4, alerts=alerts)
    assert run1.events.count("alert") == 1
    r = E.evaluate(st, "1", 8000, 95, alerts)
    assert len(r["events"]) == 0
    assert r["attrs"].get("alert_reached_at")


def test_the_second_alert_re_arms_after_the_probe_falls_well_below_it() -> None:
    st = E.new_state(["1"])
    alerts = {"alert_temp": 60}
    cook(st, to=8000, T0=5, k=1e-4, alerts=alerts)
    back = cook(st, from_=8030, to=8100, T0=55, rate=0, alerts=alerts)  # pulled back, re-placed
    assert back.events.count("alert") == 0
    again = cook(st, from_=8130, to=12000, T0=55, k=1e-4, alerts=alerts)
    assert again.events.count("alert") == 1
