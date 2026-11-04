"""The model around the estimator: groups per cooker, gaps, moves, latches."""

from __future__ import annotations

import math

from custom_components.cook_predictor.core import estimator as E
from custom_components.cook_predictor.core.model import Model

SETTINGS = {"1": (74.0, {"warn_min": 30, "alert_temp": 0, "alert_min": 0})}


def heat(m: Model, t0: int, t1: int, temp: float, pit: float | None, k: float = 2e-4) -> float:
    for t in range(t0, t1 + 1, 30):
        obs = []
        if pit is not None:
            obs.append(("reading", "9", t, pit))
        temp = (pit or 115) + (temp - (pit or 115)) * math.exp(-k * 30)
        obs.append(("reading", "1", t, round(temp * 18) / 18))
        m.ingest(obs)
        m.evaluate(t, SETTINGS)
    return temp


def test_a_gap_left_open_by_a_replay_ends_at_the_first_live_reading() -> None:
    m = Model()
    m.set_membership({"1": None}, [], 0)
    st = m.groups["none"]["st"]
    rows = [["1", 0, 20.0], ["1", 60, E.GAP]]
    E.replay(st, rows, lambda k, t: 74.0, 60, None, 120, None)
    assert st["probes"]["1"]["gap_since"] == 60
    m.ingest([("reading", "1", 180, 21.0)])
    p = st["probes"]["1"]
    assert p["gap_since"] is None
    assert p["gap_at"] == 180
    assert E.evaluate(st, "1", 190, 74.0)["attrs"].get("reason") != "probe unavailable"


def test_the_ambient_gap_is_ended_the_same_way() -> None:
    m = Model()
    m.set_membership({"1": "9"}, ["9"], 0)
    st = m.groups["9"]["st"]
    E.replay(
        st, [["ambient", 0, 110.0], ["ambient", 60, E.GAP]], lambda k, t: 74.0, 60, None, 120, None
    )
    assert st["ambient_gap_since"] == 60
    m.ingest([("reading", "9", 180, 111.0)])
    assert st["ambient_gap_since"] is None
    assert st["ambient_gap_at"] == 180


def test_a_probe_moved_to_another_cooker_keeps_its_session_and_does_not_alert_again() -> None:
    m = Model()
    m.set_membership({"1": None}, [], 0)
    temp = heat(m, 0, 3 * 3600, 40.0, None, k=4e-4)
    p = m.groups["none"]["st"]["probes"]["1"]
    assert p["notified"].get("done")  # reached 74 in the group without a pit
    session = p["session"]["start"]
    m.set_membership({"1": "9"}, ["9"], 3 * 3600 + 60)
    moved = m.groups["9"]["st"]["probes"]["1"]
    assert moved["session"]["start"] == session
    assert moved["notified"].get("done")
    assert moved["moved_at"] == 3 * 3600 + 60  # the fit starts over in the new cooker
    fired = []
    for t in range(3 * 3600 + 60, 4 * 3600, 30):
        m.ingest([("reading", "9", t, 115.0), ("reading", "1", t, round((temp + 0.01) * 18) / 18)])
        fired += [e for _, e, _ in m.evaluate(t, SETTINGS)[1]]
    assert "done" not in fired


def test_a_600_degree_oven_is_a_pit_not_an_unplugged_one() -> None:
    m = Model()
    m.set_membership({"1": "9"}, ["9"], 0)
    heat(m, 0, 2 * 3600, 5.0, 600.0, k=3e-5)
    st = m.groups["9"]["st"]
    assert st["ambient_off_since"] is None
    r = E.evaluate(st, "1", 2 * 3600, 74.0)
    assert r["attrs"]["ambient"] == 600.0
