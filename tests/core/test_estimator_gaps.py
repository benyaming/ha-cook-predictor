"""Explicit gaps: an unavailable source is not an unplugged probe (not in the JavaScript)."""

from __future__ import annotations

import math

from custom_components.cook_predictor.core import estimator as E
from tests.core.test_estimator import cook


def predicting_state() -> E.State:
    st = E.new_state(["1"])
    cook(st, to=7200, T0=5, k=1e-4)
    assert E.evaluate(st, "1", 7200, 93)["status"] == "predicting"
    return st


def test_a_gap_is_stale_at_once_and_keeps_the_session() -> None:
    st = predicting_state()
    session = st["probes"]["1"]["session"]["start"]
    E.mark_gap(st, "1", 7230)
    r = E.evaluate(st, "1", 7240, 93)
    assert r["status"] == "stale"
    assert r["attrs"]["reason"] == "probe unavailable"
    assert "done_at" not in r["attrs"]
    assert r["events"] == []
    # well past unplug_end_s: an unplug would have ended the session by now
    r = E.evaluate(st, "1", 7230 + 600, 93)
    assert r["status"] == "stale"
    E.ingest_ambient(st, 7830, 115)
    E.end_gap(st, "1", 7840)
    E.ingest_probe(st, "1", 7840, st["probes"]["1"]["samples"][-1][1] + 0.5)
    assert st["probes"]["1"]["session"]["start"] == session
    r = E.evaluate(st, "1", 7840, 93)
    assert (r["status"], r["attrs"]["reason"]) == ("unstable", "readings resumed")
    assert st["probes"]["1"]["fit_from"] == 7840


def test_an_unplug_still_ends_the_session() -> None:
    st = predicting_state()
    session = st["probes"]["1"]["session"]["start"]
    E.ingest_probe(st, "1", 7230, math.nan)
    assert E.evaluate(st, "1", 7240, 93)["status"] == "unplugged"
    E.ingest_probe(st, "1", 7230 + E.CFG["unplug_end_s"] + 10, 60.0)
    assert st["probes"]["1"]["session"]["start"] != session


def test_nothing_is_held_across_a_gap_in_replay() -> None:
    rows = [["ambient", t, 115.0] for t in range(0, 7201, 60)]
    rows += [["1", t, 5 + t / 200] for t in range(0, 3601, 60)]
    rows += [["1", 3700, E.GAP], ["1", 6000, 40.0]]
    rows.sort(key=lambda r: r[1])
    seen: dict[float, E.Result] = {}
    st = E.new_state(["1"])
    E.replay(st, rows, lambda k, t: 93, 60, lambda k, t, r: seen.__setitem__(t, r), 7200)
    during = seen[4800]
    assert during["status"] == "stale"
    assert during["attrs"]["observed_at"] == E._iso(3700)  # not the replay cursor
    after = seen[6000]
    assert after["status"] != "stale"
    assert st["probes"]["1"]["gap_at"] == 6000


def test_an_ambient_gap_stops_the_newton_model_and_no_fit_spans_it() -> None:
    st = predicting_state()
    E.mark_ambient_gap(st, 7230)
    E.report(st, "1", 7240, st["probes"]["1"]["samples"][-1][1])
    r = E.evaluate(st, "1", 7240, 93)
    assert r["attrs"]["ambient"] is None
    assert r["attrs"].get("model") != "newton"
    E.end_ambient_gap(st, 7500)
    E.ingest_ambient(st, 7500, 115)
    assert st["ambient_gap_at"] == 7500
