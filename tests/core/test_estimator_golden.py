"""The estimator against golden files recorded from the JavaScript it was ported from.

Each golden in tests/fixtures/estimator holds the inputs one cook fed to the reference
estimator and what came out: the status, published attributes and events of every
evaluation that had events and of one evaluation every 5 minutes per probe (all of them
for the live hour after a replay), plus snapshots of the whole state. The test feeds the
same inputs to the port and requires the same results, floats exactly.

- real_brisket_*: recorded cooks (timestamps shifted to start at 2026-01-01T00:00:00Z,
  entities renamed) through replay(), then the last hour again an hour later as live
  readings: a step every 30 s with heartbeats, an evaluation per step and a 12-minute
  silence, so the live path (await_live, gaps, heartbeats) is covered too.
- slab_*: the synthetic slab-model cooks of the reference test suite.
- settings: alert settings, heads-ups and targets changed mid-cook, a stall, pit outages,
  a "New cook" press and an unplug.

Live inputs are an op stream: ["i", key, t, v] ingest (key "ambient" for the pit),
["h", key, t, v] heartbeat, ["r", key, t] New cook, ["e", key, t, target, alerts] evaluate.

The goldens run with V8's own exp and log (tests/core/_jsmath.py) in place of the
platform libm, which differs from V8 in the last bit for about 1% of exp arguments;
test_real_cook_with_platform_math runs one on the libm the integration uses.
"""

from __future__ import annotations

import gzip
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from custom_components.cook_predictor.core import estimator
from tests.core import _jsmath

FIXTURES = Path(__file__).parent.parent / "fixtures" / "estimator"
REAL = ["real_brisket_1", "real_brisket_2"]
SYNTHETIC = [
    "slab_brisket",
    "slab_hardstall",
    "slab_roast",
    "slab_chicken",
    "slab_chickenhot",
    "slab_chickenwet",
    "settings",
]


def load(name: str) -> dict[str, Any]:
    with gzip.open(FIXTURES / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def keyed_rows(golden: dict[str, Any]) -> list[list[Any]]:
    """The golden's recorded rows keyed the way replay() takes them."""
    key_of = {entity: key for key, entity in golden["probes"].items()}
    key_of[golden["ambient"]] = "ambient"
    return [[key_of[entity], t, v] for entity, t, v in golden["rows"]]


def run_ops(st: estimator.State, ops: list[list[Any]]) -> dict[int, estimator.Result]:
    """Apply an op stream; the result of every evaluation, by op index."""
    results = {}
    for i, op in enumerate(ops):
        kind, key, t = op[0], op[1], op[2]
        if kind == "i":
            if key == "ambient":
                estimator.ingest_ambient(st, t, op[3])
            else:
                estimator.ingest_probe(st, key, t, op[3])
        elif kind == "h":
            estimator.report(st, key, t, op[3])
        elif kind == "r":
            estimator.reset_probe(st, key, t)
        elif kind == "e":
            results[i] = estimator.evaluate(st, key, t, op[3], op[4])
        else:
            raise ValueError(f"unknown op {op!r}")
    return results


def replay_golden(golden: dict[str, Any], st: estimator.State) -> list[list[Any]]:
    """replay() a real cook's rows; every evaluation as [key, t, result]."""
    seen: list[list[Any]] = []
    estimator.replay(
        st,
        keyed_rows(golden),
        lambda key, t: golden["target"],
        golden["grid_s"],
        lambda key, t, r: seen.append([key, t, r]),
    )
    return seen


def first_difference(actual: Any, expected: Any, where: str = "") -> str | None:
    """Where two JSON-like values first differ, or None. Floats compare exactly."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        if actual.keys() != expected.keys():
            return f"{where}: keys {sorted(actual)} != {sorted(expected)}"
        for k in expected:
            found = first_difference(actual[k], expected[k], f"{where}.{k}")
            if found:
                return found
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            return f"{where}: length {len(actual)} != {len(expected)}"
        for i, (x, y) in enumerate(zip(actual, expected, strict=True)):
            found = first_difference(x, y, f"{where}[{i}]")
            if found:
                return found
        return None
    if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
        return f"{where}: {actual!r} != {expected!r}"
    return None


def assert_same(actual: Any, expected: Any, where: str) -> None:
    found = first_difference(actual, expected, where)
    assert found is None, found


def assert_results(results: dict[int, Any], records: list[list[Any]], what: str) -> None:
    """Stored evaluations come out the same; every evaluation with events was stored."""
    stored = {index: result for index, result in records}
    for index, expected in stored.items():
        assert_same(results[index], expected, f"{what} #{index}")
    for index, result in results.items():
        if index not in stored:
            assert result["events"] == [], f"{what} #{index}: unexpected {result['events']}"


@pytest.fixture
def v8_math(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(estimator, "exp", _jsmath.exp)
    monkeypatch.setattr(estimator, "log", _jsmath.log)


@pytest.mark.usefixtures("v8_math")
@pytest.mark.parametrize("name", REAL)
def test_real_cook_replay_then_live(name: str) -> None:
    golden = load(name)
    st = estimator.new_state(list(golden["probes"]))
    seen = replay_golden(golden, st)
    assert len(seen) == golden["replay"]["count"]
    for index, key, t, _ in golden["replay"]["records"]:
        assert seen[index][:2] == [key, t]
    assert_results(
        {i: r for i, (_, _, r) in enumerate(seen)},
        [[index, result] for index, _, _, result in golden["replay"]["records"]],
        "replay evaluation",
    )
    assert_same(st, golden["state_after_replay"], "state after replay")

    results = run_ops(st, golden["live"]["ops"])
    assert_results(results, golden["live"]["records"], "live op")
    assert_same(st, golden["state"], "state")


@pytest.mark.usefixtures("v8_math")
@pytest.mark.parametrize("name", SYNTHETIC)
def test_synthetic_cook(name: str) -> None:
    golden = load(name)
    st = estimator.new_state(golden["probes"])
    results = run_ops(st, golden["ops"])
    assert_results(results, golden["records"], "op")
    assert_same(st, golden["state"], "state")


# One rounding step of each published number, the most a last-bit difference in exp or log
# moves it. k has three significant digits; finish times move with the minute ETAs.
# Attributes not listed (sample times, strings, flags) must not move at all.
STEP = {
    "temperature": 0.1,
    "ambient": 0.1,
    "start_temp": 0.1,
    "rate": 0.1,
    "stall_temp": 0.1,
    "fit_rmse": 0.01,
    "stall_ratio": 0.01,
    "elapsed_min": 1,
    "window_min": 1,
    "eta_min": 1,
    "eta_newton_min": 1,
    "eta_linear_min": 1,
    "eta_after_stall_min": 1,
    "alert_eta_min": 1,
}
FINISH_TIMES = {"done_at", "alert_at"}


def within_one_step(key: str, actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    if actual is None or expected is None:
        return False
    if key in STEP:
        return abs(actual - expected) <= STEP[key] * (1 + 1e-9)
    if key == "k":
        return abs(actual - expected) <= 10 ** (math.floor(math.log10(expected)) - 2) * (1 + 1e-9)
    if key in FINISH_TIMES:
        moved = datetime.fromisoformat(actual) - datetime.fromisoformat(expected)
        return abs(moved.total_seconds()) <= 60
    return False


def test_real_cook_with_platform_math() -> None:
    """The platform's math.exp and math.log, which Home Assistant runs, instead of V8's.

    Statuses and events must not change, and a published number by at most one rounding
    step. On the goldens nothing published changed at all (macOS arm64, 9578 evaluations);
    only a few raw reference-k entries in the state differ, by one ulp.
    """
    golden = load("real_brisket_2")
    st = estimator.new_state(list(golden["probes"]))
    seen = replay_golden(golden, st)
    live = run_ops(st, golden["live"]["ops"])
    pairs = [(seen[index][2], expected) for index, _, _, expected in golden["replay"]["records"]]
    pairs += [(live[index], expected) for index, expected in golden["live"]["records"]]
    for actual, expected in pairs:
        assert actual["status"] == expected["status"]
        assert actual["events"] == expected["events"]
        assert actual["attrs"].keys() == expected["attrs"].keys()
        for key, value in expected["attrs"].items():
            assert within_one_step(key, actual["attrs"][key], value), (
                key,
                actual["attrs"][key],
                value,
            )
