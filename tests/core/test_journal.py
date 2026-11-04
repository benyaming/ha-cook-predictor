"""The journal's pure logic: the 21 cases of the Node-RED journal's test-journal.mjs, one test
each, same names and assertions (on the normalised inputs: a probe that is out reads
"disconnected", the one pit is channel "4"), then the channels of schema 2, then the
JavaScript semantics the port depends on, with the values V8 gives.
"""

import math

import pytest

from custom_components.cook_predictor.core import journal as J


def P(n):
    """A channel's pseudo id: internals 1 to 3 and the pit 4 in the ported cases."""
    return f"journal.channel_{n}"


def CP(n):
    return f"journal.predictor_{n}"


def TG(n):
    return f"journal.target_{n}"


RESET = "journal.new_cook"
FINISH = "journal.finish"
MEMBERS = "journal.members"
IDS = {
    "probes": {1: P(1), 2: P(2), 3: P(3)},
    "pits": {4: P(4)},
    "predictors": {1: CP(1), 2: CP(2), 3: CP(3)},
    "targets": {1: TG(1), 2: TG(2), 3: TG(3)},
    "reset": RESET,
    "finish": FINISH,
    "members": MEMBERS,
}
KIND = J.kinds(IDS)
iso = J.iso
T0 = 1791680400  # 2026-10-11 04:00 local
UNPLUGGED = "disconnected"


def same(a, b):
    """The JS tests' same(): equal as JSON text (key order and number formatting included)."""
    assert J.to_json(a) == J.to_json(b)


def feed(js, rows):
    events = []
    for row in rows:
        t, entity_id, state = row[:3]
        events.extend(J.observe(js, KIND, entity_id, t, state, row[3] if len(row) > 3 else None))
    return events


def types(events):
    return [e["type"] for e in events]


def heating(t0, t1, *, n=1, start=5, pit=110, k=1e-4, dt=60):
    """A probe heating from `start` C toward `pit` at k, read every `dt` s, plus the pit."""
    rows = []
    t = t0
    while t <= t1:
        v = pit + (start - pit) * math.exp(-k * (t - t0))
        rows.append([t, P(n), J.number_text(J.js_round(v * 18) / 18)])
        rows.append([t + 1, P(4), J.number_text(pit)])
        t += dt
    return rows


def test_a_probe_at_room_temperature_never_becomes_a_cook():
    js = J.new_journal(T0)
    rows = []
    for t in range(T0, T0 + 3 * 3600 + 1, 60):
        rows.append([t, P(1), J.number_text(22 + (0.05 if t % 120 else 0))])
    events = feed(js, rows)
    same(types(events), ["drop"])
    assert js["open"] and js["open"]["provisional"]  # reopened, still provisional


def test_a_heating_probe_confirms_the_cook_which_starts_where_heating_began():
    js = J.new_journal(T0)
    idle = [[t, P(1), "4.2"] for t in range(T0, T0 + 1800, 60)]
    events = feed(js, [*idle, *heating(T0 + 1800, T0 + 3 * 3600, start=4.2)])
    same(types(events), ["open"])
    c = js["open"]
    assert c["provisional"] is False
    assert abs(c["start"] - (T0 + 1800)) <= 120, f"start {c['start'] - T0}"
    assert c["id"] == J.cook_id(c["start"])


def test_every_probe_unplugged_for_30_min_closes_the_cook_at_the_last_unplug():
    js = J.new_journal(T0)
    feed(js, heating(T0, T0 + 4 * 3600))
    events = feed(js, [[T0 + 4 * 3600 + 60, P(1), UNPLUGGED], [T0 + 4 * 3600 + 1900, P(4), "30"]])
    same(types(events), ["close"])
    c = events[0]["cook"]
    assert c["ended_by"] == "unplugged"
    assert c["end"] == T0 + 4 * 3600 + 60


def test_an_unavailable_probe_is_a_gap_not_an_unplug():
    js = J.new_journal(T0)
    feed(js, heating(T0, T0 + 3600))
    events = feed(js, [[T0 + 3700, P(1), "unavailable"], [T0 + 3700 + 3 * 3600, P(4), "100"]])
    same(types(events), [])
    assert js["open"]
    assert js["open"]["probes"]["1"]["series"][-1][1] is None
    # ...until no valid reading for 6 h: closed at the last valid one
    later = feed(js, [[T0 + 3700 + 6 * 3600 + 60, P(4), "100"]])
    same(types(later), ["close"])
    assert later[0]["cook"]["ended_by"] == "device_off"
    assert later[0]["cook"]["end"] == T0 + 3600


def test_finish_keeps_a_still_connected_probe_from_starting_a_cook_until_it_is_replugged():
    js = J.new_journal(T0)
    feed(js, heating(T0, T0 + 3600))
    fin = feed(js, [[T0 + 3700, FINISH, iso(T0 + 3700)]])
    same(types(fin), ["close"])
    assert fin[0]["cook"]["ended_by"] == "finish"
    feed(js, heating(T0 + 3800, T0 + 9000, start=70, pit=20, k=2e-4))  # cooling on the counter
    assert js["open"] is None
    feed(js, [[T0 + 9100, P(1), UNPLUGGED], [T0 + 9200, P(1), "4"]])  # unplugged, into new meat
    assert js["open"] and js["open"]["provisional"]
    same(types(feed(js, heating(T0 + 9300, T0 + 12000, start=4))), ["open"])


def test_two_new_cook_presses_with_probes_connected_make_two_cooks():
    js = J.new_journal(T0)
    ev1 = feed(js, [[T0, P(1), "5"], [T0 + 10, RESET, iso(T0 + 10)], *heating(T0 + 60, T0 + 3600)])
    # the plugged probe's provisional cook gives way to the press
    same(types(ev1), ["drop", "open"])
    ev2 = feed(js, [[T0 + 3700, RESET, iso(T0 + 3700)], *heating(T0 + 3760, T0 + 7200, start=60)])
    same(types(ev2), ["close", "open"])
    assert ev2[0]["cook"]["start"] == T0 + 10
    assert ev2[0]["cook"]["end"] == T0 + 3700
    assert ev2[1]["cook"]["start"] == T0 + 3700
    assert ev2[1]["cook"]["probes"]["1"], "the connected probe joins the new cook at once"


def test_a_button_row_repeating_an_old_press_is_not_a_press():
    js = J.new_journal(T0)
    feed(js, heating(T0, T0 + 3600))
    ev = feed(js, [[T0 + 3605, RESET, iso(T0 - 86400)], [T0 + 3610, FINISH, iso(T0 - 600)]])
    same(types(ev), [])
    assert js["open"]


def test_a_published_forecast_is_kept_on_changes_not_on_every_update():
    js = J.new_journal(T0)
    feed(js, heating(T0, T0 + 600))
    rows = []
    for i in range(120):
        t = T0 + 600 + i * 30
        attrs = {
            "target": 93,
            "done_at": iso(T0 + 20000 + (0 if i < 60 else 600)),
            "model": "newton",
            "confidence": "ok",
            "session_start": iso(T0),
        }
        rows.append([t, CP(1), "predicting", attrs])
    feed(js, rows)
    pred = js["open"]["predictions"]["1"]
    # first, every 10 min while unchanged (3600 s / 600), and once when done_at moved 10 min
    assert 6 <= len(pred) <= 8, f"{len(pred)} stored"
    assert any(i and e["d"] - pred[i - 1]["d"] == 600 for i, e in enumerate(pred))


def test_the_predictor_build_is_recorded_when_it_changes_not_stamped_on_old_forecasts():
    js = J.new_journal(T0)
    feed(js, heating(T0, T0 + 600))

    def a(build):
        attrs = {"target": 93, "done_at": iso(T0 + 20000), "session_start": iso(T0)}
        return attrs if build is None else {**attrs, "build": build}  # None: undefined

    feed(
        js,
        [
            [T0 + 700, CP(1), "predicting", a(None)],
            [T0 + 760, CP(1), "predicting", a("aaa")],
            [T0 + 820, CP(1), "predicting", a("aaa")],
            [T0 + 880, CP(1), "predicting", a("bbb")],
        ],
    )
    stored = js["open"]["predictions"]["1"]
    assert [e.get("b", J.UNDEFINED) for e in stored] == [J.UNDEFINED, "aaa", "bbb"]


def test_a_reading_just_under_the_target_is_not_a_crossing():
    js = J.new_journal(T0)
    feed(
        js,
        [
            [T0, TG(1), "70"],
            [T0 + 60, P(1), "5"],
            [T0 + 600, P(1), "40"],
            [T0 + 1200, P(1), "69.972"],
            [T0 + 1800, P(1), "69.9444444444444"],
        ],
    )
    c = js["open"]
    assert J.reached_at(c, "1", 3600) is None
    assert any(v == 6997 for _, v in c["probes"]["1"]["series"])


def test_pit_average_is_time_weighted():
    js = J.new_journal(T0)
    feed(
        js,
        [[T0, P(1), "5"], [T0, P(4), "100"], [T0 + 3000, P(4), "130"], [T0 + 3600, P(1), "20"]],
    )
    s = J.held_stats(js["open"]["pits"]["4"], 3600)
    assert abs(s["avg"] - 105) < 0.01, f"avg {s['avg']}"


def test_a_close_dated_back_drops_what_came_after_its_end():
    # review repro: pit 100 at 0, unplug at 100, pit 200 at 200, target changed at 300, close
    # at 1900
    js = J.new_journal(T0 - 10)
    feed(
        js,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0, P(4), "100"],
            [T0 + 50, P(1), "30"],
        ],
    )
    ev = feed(
        js,
        [
            [T0 + 100, P(1), UNPLUGGED],
            [T0 + 200, P(4), "200"],
            [T0 + 300, TG(1), "80"],
            [T0 + 1900, P(4), "150"],
        ],
    )
    same(types(ev), ["close"])
    c = ev[0]["cook"]
    end = c["end"] - c["start"]
    assert c["end"] == T0 + 100
    pit = J.held_stats(c["pits"]["4"], end)
    assert pit["max"] == 100
    assert pit["covered"] == end - 5
    assert not any(rel > end for rel, _ in c["targets"].get("1") or [])
    assert all(rel <= end for rel, _ in c["pits"]["4"])


def test_a_flat_temperature_does_not_make_a_finished_cook_look_over_early():
    # review repro: 70 C from t=60, unchanged while connected, unavailable at 10800, Finish at
    # 10860
    js = J.new_journal(T0 - 10)
    rows = [[T0 - 5, RESET, iso(T0 - 5)], [T0, P(1), "20"], [T0 + 60, P(1), "70"]]
    for t in range(T0 + 90, T0 + 10800, 30):
        attrs = {
            "temperature": 70,
            "target": 65,
            "observed_at": iso(t),
            "session_start": iso(T0),
            "reached_at": iso(T0 + 60),
        }
        rows.append([t, CP(1), "done", attrs])
    rows.append([T0 + 10800, P(1), "unavailable"])
    ev = feed(js, [*rows, [T0 + 10860, FINISH, iso(T0 + 10860)]])
    c = next(e["cook"] for e in ev if e["type"] == "close")
    assert c["end"] >= T0 + 10770, f"ended {c['end'] - T0} s in"


def test_a_reached_target_is_paired_with_the_target_in_force_including_a_lowered_one():
    js = J.new_journal(T0 - 10)
    feed(
        js,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, TG(1), "60"],
            [T0, P(1), "20"],
            [T0 + 600, P(1), "61"],
            [T0 + 900, TG(1), "93"],
            [T0 + 1800, P(1), "93.5"],
            [T0 + 2400, P(1), "70"],
            [T0 + 2500, TG(1), "65"],
        ],
    )
    c = js["open"]  # started at the press, 5 s before T0
    same(
        J.hits(c, "1", 3000),
        [{"rel": 605, "target": 60}, {"rel": 1805, "target": 93}, {"rel": 2505, "target": 65}],
    )
    same(J.reached_at(c, "1", 3000), {"rel": 2505, "target": 65})
    s = J.summarize(c, T0 + 3000)
    assert s["probes"][0]["target"] == 65
    assert s["probes"][0]["reached"] == 2505


def test_forecasts_are_compared_only_for_the_same_target_and_probe_session():
    def p(t, g, ss, done):
        attrs = {
            "target": g,
            "done_at": iso(done),
            "session_start": iso(ss),
            "temperature": 50,
            "observed_at": iso(t),
        }
        return [t, CP(1), "predicting", attrs]

    js = J.new_journal(T0 - 10)
    feed(
        js,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, TG(1), "93"],
            [T0, P(1), "20"],
            p(T0 + 600, 80, T0, T0 + 9000),  # another target
            p(T0 + 3600, 93, T0 + 3000, T0 + 7300),  # the session that reaches it
            [T0 + 7200, P(1), "93.2"],
        ],
    )
    c = js["open"]
    hit = J.reached_at(c, "1", 7300)
    # 2 h before, only the forecast for 80 C existed: left out; 1 h and 30 min before, the 93 one
    same([[f["before"], f["off"]] for f in J.forecasts(c, "1", hit)], [[3600, 100], [1800, 100]])
    # the same target from an earlier probe session (moved, New cook) is not this one's forecast
    js2 = J.new_journal(T0 - 10)
    feed(
        js2,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, TG(1), "93"],
            [T0, P(1), "20"],
            p(T0 + 600, 93, T0, T0 + 5000),
            p(T0 + 3 * 3600 + 600, 93, T0 + 3 * 3600, T0 + 4 * 3600 + 100),
            [T0 + 4 * 3600, P(1), "93.2"],
        ],
    )
    c2 = js2["open"]
    same([f["before"] for f in J.forecasts(c2, "1", J.reached_at(c2, "1", 5 * 3600))], [1800])


def test_a_probe_that_read_the_air_before_going_into_cold_meat_starts_the_cook_on_time():
    # plugged in at 28 C in the air, into the meat at 3 C, heating from 30 min on: the cook is
    # confirmed early (5 C above its lowest, not its first, reading) and starts where it heated
    js = J.new_journal(T0)
    rows = [[T0, P(1), "28.1"], [T0 + 60, P(1), "3.1"]]
    rows += [[t, P(1), "3"] for t in range(T0 + 120, T0 + 1800, 60)]
    # by 50 min the meat is about 8-9 C: confirmed, though still far below the 33 C a measure
    # from the first (air) reading would need
    events = feed(js, [*rows, *heating(T0 + 1800, T0 + 3000, start=3)])
    same(types(events), ["open"])
    c = js["open"]
    assert abs(c["start"] - (T0 + 1800)) <= 180, f"start {c['start'] - T0}"
    # where heating began: within 1 C of the lowest reading, nowhere near the 28 C in the air
    s = J.summarize(c, T0 + 3000)["probes"][0]["start_temp"]
    assert 3 <= s <= 4, f"start temp {s}"
    assert J.derive(c, T0 + 3000)["probes"]["1"]["start_temp"] == s
    # and the air reading before it does not count as the start temperature of a cook it opened
    js2 = J.new_journal(T0 - 10)
    feed(
        js2,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "28.1"],
            [T0 + 120, P(1), "3"],
            [T0 + 3600, P(1), "20"],
        ],
    )
    assert J.summarize(js2["open"], T0 + 3600)["probes"][0]["start_temp"] == 3


def test_a_provisional_cook_saved_before_probes_kept_their_lowest_reading_still_confirms_on_time():
    # review repro: air 28.1 at 0, meat 3 at 60, 6 at 3500; the saved state has no min; 8 at 4000
    js = J.new_journal(T0)
    feed(js, [[T0, P(1), "28.1"], [T0 + 60, P(1), "3"], [T0 + 3500, P(1), "6"]])
    legacy = J.clone(js)
    del legacy["open"]["probes"]["1"]["min"]
    same(types(feed(legacy, [[T0 + 4000, P(1), "8"]])), ["open"])
    assert legacy["open"]["start"] == T0 + 60


def _stall_report(t, s, st=None, ss=T0):
    attrs = {"target": 93, "session_start": iso(ss)}
    if st is not None:
        attrs["stall_since"] = iso(st)
    attrs.update({"temperature": 66, "observed_at": iso(t)})
    return [t, CP(1), s, attrs]


def test_a_stall_that_goes_stale_and_never_comes_back_ends_where_the_gap_began():
    # review repro: stall at 1000 (since 900), stale at 2000, nothing after; asked at 10000
    p = _stall_report
    js = J.new_journal(T0 - 10)
    feed(
        js,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            p(T0 + 1000, "stall", T0 + 900),
            p(T0 + 2000, "stale"),
        ],
    )
    same(J.stall_intervals(js["open"], "1", 10005), [[905, 2005]])


def test_a_stall_reported_after_a_gap_by_a_new_probe_session_is_a_new_stall():
    # review repro: stall at 1000 since 900 (session 0), stale at 2000, stall at 10000 since
    # 9900 (session 9000), predicting at 11000: two stalls, not one across the gap
    p = _stall_report
    js = J.new_journal(T0 - 10)
    feed(
        js,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            p(T0 + 1000, "stall", T0 + 900, T0),
            p(T0 + 2000, "stale", None, T0),
            p(T0 + 10000, "stall", T0 + 9900, T0 + 9000),
            p(T0 + 11000, "predicting", None, T0 + 9000),
        ],
    )
    same(J.stall_intervals(js["open"], "1", 12005), [[905, 2005], [9905, 11005]])
    # the same session with an onset after the gap is a new stall too
    js2 = J.new_journal(T0 - 10)
    feed(
        js2,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            p(T0 + 1000, "stall", T0 + 900, T0),
            p(T0 + 2000, "stale", None, T0),
            p(T0 + 10000, "stall", T0 + 9900, T0),
            p(T0 + 11000, "predicting", None, T0),
        ],
    )
    same(J.stall_intervals(js2["open"], "1", 12005), [[905, 2005], [9905, 11005]])


def test_a_stall_stays_one_stall_through_a_restart_of_the_predictor():
    p = _stall_report

    def run(rows):
        js = J.new_journal(T0 - 10)
        feed(js, [[T0 - 5, RESET, iso(T0 - 5)], [T0, P(1), "20"], *rows])
        return J.stall_intervals(js["open"], "1", 5000)

    # stale while the predictor restarted, then the same stall again (its replayed onset is
    # before the gap); the lid open (unstable) in the middle of it
    same(
        run(
            [
                p(T0 + 1000, "stall", T0 + 900),
                p(T0 + 2000, "stale"),
                p(T0 + 2100, "stall", T0 + 930),
                p(T0 + 2500, "unstable"),
                p(T0 + 2600, "stall", T0 + 930),
                p(T0 + 4000, "predicting"),
            ]
        ),
        [[905, 4005]],
    )
    # a stall re-published with a stall_since reaching back into the previous one
    same(
        run(
            [
                p(T0 + 1000, "stall", T0 + 900),
                p(T0 + 2000, "predicting"),
                p(T0 + 2500, "stall", T0 + 1500),
                p(T0 + 4000, "predicting"),
            ]
        ),
        [[905, 4005]],
    )


def test_a_probe_not_in_the_cook_leaves_no_forecasts_in_it():
    js = J.new_journal(T0 - 10)
    feed(
        js,
        [
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0 + 60, CP(3), "stale", {}],
            [T0 + 120, CP(3), "unplugged", {}],
        ],
    )
    assert "3" not in js["open"]["predictions"]


# ---- channels (schema 2) ----

CH_IDS = {
    "probes": {1: P(1), 2: P(2), 3: P(3), 5: P(5)},
    "pits": {4: P(4), "a": P("a"), "b": P("b")},
    "predictors": {1: CP(1), 2: CP(2), 5: CP(5)},
    "targets": {1: TG(1), 2: TG(2), 5: TG(5)},
    "reset": RESET,
    "finish": FINISH,
    "members": MEMBERS,
}
CH_KIND = J.kinds(CH_IDS)


def feed2(js, rows):
    events = []
    for row in rows:
        t, entity_id, state = row[:3]
        attrs = row[3] if len(row) > 3 else None
        events.extend(J.observe(js, CH_KIND, entity_id, t, state, attrs))
    return events


def internal(name, pit="4"):
    return {"name": name, "role": "internal", "ambient": pit}


def ambient(name):
    return {"name": name, "role": "ambient", "ambient": None}


def members(t, active, rev=1):
    """A membership row: its state is ignored, its attributes are the applied channel set."""
    return [t, MEMBERS, str(rev), {"active": active, "rev": rev}]


BRISKET = {"1": internal("Brisket"), "4": ambient("Pit")}


def brisket(js, *rows):
    """Probe 1 (Brisket) in the pit 4, New cook 5 s before T0, 20 C at T0, 60 C at 3000."""
    return feed2(
        js,
        [
            members(T0 - 8, BRISKET),
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0, P(4), "110"],
            [T0 + 3000, P(1), "60"],
            *rows,
        ],
    )


def test_a_520_c_pit_is_a_reading():
    js = J.new_journal(T0 - 10)
    feed2(
        js,
        [
            members(T0 - 8, {"1": internal("Pizza", "b"), "b": ambient("Oven")}),
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0, P("b"), "520"],
            [T0 + 600, P("b"), "3623.05"],  # not a sentinel any more: the adapter says
        ],
    )
    c = js["open"]
    same(c["pits"]["b"], [[5, 52000], [605, 362305]])
    s = J.summarize(c, T0 + 1200)
    same(s["pits"]["b"]["name"], "Oven")
    assert s["pits"]["b"]["max"] == 3623.05


def test_a_disconnected_probe_closes_the_cook_like_an_unplugged_one():
    js = J.new_journal(T0 - 10)
    ev = brisket(js, [T0 + 3100, P(1), "disconnected"], [T0 + 3100 + 1799, P(4), "100"])
    same(types(ev), ["open"])  # 30 min not yet
    ev = feed2(js, [[T0 + 3100 + 1800, P(4), "101"]])
    same(types(ev), ["close"])
    assert ev[0]["cook"]["ended_by"] == "unplugged"
    assert ev[0]["cook"]["end"] == T0 + 3100
    # and an unknown state is only a gap
    js2 = J.new_journal(T0 - 10)
    same(
        types(brisket(js2, [T0 + 3100, P(1), "unknown"], [T0 + 3100 + 3600, P(4), "100"])), ["open"]
    )


def test_a_thermometer_switched_off_after_the_unplug_still_closes_as_unplugged():
    # the user's habit: unplug the probes, then switch the thermometer off. The unknown that
    # follows says nothing about the probe: 30 min after the unplug the cook closes there.
    js = J.new_journal(T0 - 10)
    brisket(js, [T0 + 3100, P(1), "disconnected"], [T0 + 3310, P(1), "unavailable"])
    same(js["open"]["unplugged"], {"1": T0 + 3100})
    events = []
    J.advance(js, T0 + 3100 + 1799, events)
    same(types(events), [])
    J.advance(js, T0 + 3100 + 1800, events)
    same([(e["type"], e["cook"]["ended_by"], e["cook"]["end"]) for e in events],
         [["close", "unplugged", T0 + 3100]])  # fmt: skip


def test_a_probe_known_back_in_ends_a_continuous_disconnection():
    # disconnected at 3100; at 3200 its connection sensor says it is back in, the temperature
    # not yet ("connected"): the disconnection has ended, so 30 min after 3100 is no close
    js = J.new_journal(T0 - 10)
    brisket(js, [T0 + 3100, P(1), "disconnected"], [T0 + 3200, P(1), "connected"])
    same(js["open"]["unplugged"], {})
    events = []
    J.advance(js, T0 + 3100 + 1801, events)
    same(types(events), [])
    # out again from 5000: a new timer, from there
    feed2(js, [[T0 + 5000, P(1), "disconnected"]])
    J.advance(js, T0 + 5000 + 1799, events)
    same(types(events), [])
    J.advance(js, T0 + 5000 + 1800, events)
    same([(e["type"], e["cook"]["ended_by"], e["cook"]["end"]) for e in events],
         [["close", "unplugged", T0 + 5000]])  # fmt: skip


def test_two_pits_in_one_cook_with_probes_on_each():
    js = J.new_journal(T0 - 10)
    active = {
        "1": internal("Brisket", "a"),
        "2": internal("Ribs", "b"),
        "a": ambient("Smoker"),
        "b": ambient("Oven"),
    }
    rows = [members(T0 - 8, active), [T0 - 5, RESET, iso(T0 - 5)]]
    for i, t in enumerate(range(T0, T0 + 3600, 600)):
        rows += [[t, P(1), str(20 + 5 * i)], [t, P(2), str(30 + 8 * i)]]
        rows += [[t + 1, P("a"), str(110 + i)], [t + 1, P("b"), str(180 + 2 * i)]]
    ev = feed2(js, [*rows, [T0 + 3600, FINISH, iso(T0 + 3600)]])
    c = next(e["cook"] for e in ev if e["type"] == "close")
    same(list(c["pits"]), ["a", "b"])
    same(
        c["channels"],
        {
            "1": internal("Brisket", "a"),
            "2": internal("Ribs", "b"),
            "a": ambient("Smoker"),
            "b": ambient("Oven"),
        },
    )
    s = J.summarize(c)
    same([(p["key"], p["name"]) for p in s["probes"]], [["1", "Brisket"], ["2", "Ribs"]])
    assert "n" not in s["probes"][0]
    same({k: (p["name"], p["min"], p["max"]) for k, p in s["pits"].items()},
         {"a": ["Smoker", 110, 115], "b": ["Oven", 180, 190]})  # fmt: skip
    same(list(J.derive(c)["pits"]), ["a", "b"])
    assert J.derive(c)["pits"]["b"]["max"] == 190


def test_a_rename_mid_cook_updates_the_channel_and_an_old_cook_keeps_its_name():
    js = J.new_journal(T0 - 10)
    renamed = {"1": internal("Brisket 6 kg"), "4": ambient("Kamado")}
    ev = brisket(js, members(T0 + 3100, renamed, 2), [T0 + 3200, FINISH, iso(T0 + 3200)])
    c = next(e["cook"] for e in ev if e["type"] == "close")
    same((c["channels"]["1"]["name"], c["channels"]["4"]["name"]), ["Brisket 6 kg", "Kamado"])
    # renamed again after it closed: the finished cook keeps the name it ended with
    feed2(js, [members(T0 + 3300, {"1": internal("Ribs"), "4": ambient("Kamado")}, 3)])
    feed2(js, [[T0 + 3400, RESET, iso(T0 + 3400)], [T0 + 3500, P(1), "21"]])
    assert c["channels"]["1"]["name"] == "Brisket 6 kg"
    assert J.summarize(c)["probes"][0]["name"] == "Brisket 6 kg"
    assert js["open"]["channels"]["1"]["name"] == "Ribs"


def test_a_probe_moved_to_another_pit_mid_cook_records_when():
    js = J.new_journal(T0 - 10)
    moved = {"1": internal("Brisket", "b"), "4": ambient("Pit"), "b": ambient("Oven")}
    brisket(js, members(T0 + 3100, moved, 2), [T0 + 3200, P("b"), "180"])
    c = js["open"]
    same(c["ambients"], {"1": [[5, "4"], [3105, "b"]]})
    assert c["channels"]["1"]["ambient"] == "b"
    same(c["pits"]["b"], [[3205, 18000]])


def test_a_retired_probe_stops_its_statistics_and_leaves_the_cook():
    js = J.new_journal(T0 - 10)
    both = {"1": internal("Brisket"), "2": internal("Ribs"), "4": ambient("Pit")}
    feed2(
        js,
        [
            members(T0 - 8, both),
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0, P(2), "30"],
            [T0 + 600, P(1), "50"],
            members(T0 + 1000, {"2": internal("Ribs"), "4": ambient("Pit")}, 2),
            [T0 + 1100, P(1), "60"],  # retired: not this cook's
            [T0 + 1200, CP(1), "predicting", {"target": 93, "done_at": iso(T0 + 9000)}],
        ],
    )
    c = js["open"]
    same(c["retired"], {"1": 1005})
    same(c["probes"]["1"]["series"], [[5, 2000], [605, 5000], [1005, None]])
    assert "1" not in c["predictions"]
    assert c["pending_close"] is None  # Ribs is still in it
    # the retired probe never unplugged, yet the rest unplugging closes the cook
    ev = feed2(js, [[T0 + 1300, P(2), "disconnected"], [T0 + 1300 + 1800, P(4), "100"]])
    same(types(ev), ["close"])
    closed = ev[0]["cook"]
    assert closed["ended_by"] == "unplugged"
    assert closed["end"] == T0 + 1300
    end = closed["end"] - closed["start"]
    assert J.held_stats(closed["probes"]["1"]["series"], end)["covered"] == 1000
    assert J.derive(closed)["probes"]["1"]["stats"]["covered"] == 1000


def test_a_close_never_cuts_what_a_removed_probe_recorded():
    both = {"1": internal("Brisket"), "2": internal("Ribs"), "4": ambient("Pit")}
    only_1 = {"1": internal("Brisket"), "4": ambient("Pit")}

    def cook(first_state):
        js = J.new_journal(T0 - 10)
        rows = [members(T0 - 8, both), [T0 - 5, RESET, iso(T0 - 5)], [T0, P(1), "20"]]
        rows += [[T0, P(2), "30"], [T0 + 100, P(1), first_state], [T0 + 3000, P(2), "70"]]
        feed2(js, [*rows, members(T0 + 3600, only_1, 2)])
        return js

    # probe 1 out since 100, Ribs cooking until it was removed at 3600: only probe 1 must be
    # quiet, but the quiet counts from 3600, and the cook ends there
    js = cook("disconnected")
    events = []
    J.advance(js, T0 + 3600 + 1799, events)
    same(types(events), [])
    J.advance(js, T0 + 3600 + 1800, events)
    c = events[0]["cook"]
    assert (c["ended_by"], c["end"]) == ("unplugged", T0 + 3600)
    same(c["probes"]["2"]["series"], [[5, 3000], [3005, 7000], [3605, None]])
    # the same with probe 1 unknown: 6 h after the cook's last valid reading (Ribs, 3000)
    js = cook("unavailable")
    events = []
    J.advance(js, T0 + 6 * 3600 + 100, events)
    same(types(events), [])
    J.advance(js, T0 + 3000 + 6 * 3600, events)
    assert (events[0]["cook"]["ended_by"], events[0]["cook"]["end"]) == ("device_off", T0 + 3000)


def test_a_replacement_that_never_reads_keeps_the_cook_until_the_device_off_rule():
    js = J.new_journal(T0 - 10)
    brisket(js, members(T0 + 4000, {"5": internal("Brisket"), "4": ambient("Pit")}, 2))
    feed2(js, [[T0 + 4100, P(5), "unavailable"]])
    events = []
    J.advance(js, T0 + 3000 + 6 * 3600 - 1, events)
    same(types(events), [])
    J.advance(js, T0 + 3000 + 6 * 3600, events)
    assert (events[0]["cook"]["ended_by"], events[0]["cook"]["end"]) == ("device_off", T0 + 3000)


def test_retire_and_replace_within_30_min_keeps_the_cook_and_leaves_the_gap():
    js = J.new_journal(T0 - 10)
    ev = brisket(js, members(T0 + 4000, {"4": ambient("Pit")}, 2))
    cid = js["open"]["id"]
    same(js["open"]["pending_close"], {"at": T0 + 4000, "deadline": T0 + 5800, "rev": 2})
    replaced = {"5": internal("Brisket"), "4": ambient("Pit")}
    ev += feed2(
        js,
        [
            members(T0 + 5000, replaced, 3),
            [T0 + 5100, P(5), "unavailable"],  # a BLE probe not connected yet
            [T0 + 6000, P(4), "112"],  # past the deadline: still open
            [T0 + 6500, P(5), "61"],
            [T0 + 7000, FINISH, iso(T0 + 7000)],
        ],
    )
    same(types(ev), ["open", "close"])
    c = ev[-1]["cook"]
    assert c["id"] == cid
    assert c["ended_by"] == "finish"
    same(c["retired"], {"1": 4005})
    same(c["probes"]["1"]["series"][-1], [4005, None])
    # the replacement waited from its activation: a gap, then its readings
    same(c["probes"]["5"]["series"], [[5005, None], [6505, 6100]])
    assert J.held_stats(c["probes"]["5"]["series"], 7005)["covered"] == 500
    same(c["waiting"], {})
    assert c["pending_close"] is None


def test_no_replacement_closes_at_the_retirement_with_channels_removed():
    js = J.new_journal(T0 - 10)
    brisket(js, members(T0 + 4000, {"4": ambient("Pit")}, 2), [T0 + 4500, P(4), "120"])
    events = []
    J.advance(js, T0 + 5800, events)  # the deadline itself: a replacement may still come
    same(types(events), [])
    J.advance(js, T0 + 5801, events)
    same(types(events), ["close"])
    c = events[0]["cook"]
    assert c["ended_by"] == "channels_removed"
    assert c["end"] == T0 + 4000
    assert all(rel <= 4005 for rel, _ in c["pits"]["4"])  # clipped at the retirement
    assert js["inhibit"] is False
    # no Finish was pressed: a new probe's reading may start the next cook by itself
    feed2(js, [members(T0 + 6000, {"5": internal("Ribs"), "4": ambient("Pit")}, 3)])
    feed2(js, [[T0 + 6100, P(5), "20"]])
    assert js["open"] and js["open"]["provisional"]


def test_an_ambient_activation_does_not_save_a_cook_waiting_to_close():
    js = J.new_journal(T0 - 10)
    brisket(js, members(T0 + 4000, {"4": ambient("Pit")}, 2))
    feed2(js, [members(T0 + 4500, {"4": ambient("Pit"), "b": ambient("Oven")}, 3)])
    assert js["open"]["pending_close"] is not None
    ev = feed2(js, [[T0 + 5801, P(4), "100"]])
    same([(e["type"], e["cook"]["ended_by"]) for e in ev], [["close", "channels_removed"]])


def test_finish_or_new_cook_while_waiting_to_close_follow_their_own_rules():
    js = J.new_journal(T0 - 10)
    ev = brisket(
        js, members(T0 + 4000, {"4": ambient("Pit")}, 2), [T0 + 4300, FINISH, iso(T0 + 4300)]
    )
    c = ev[-1]["cook"]
    assert (c["ended_by"], c["press_at"], c["pending_close"]) == ("finish", T0 + 4300, None)
    assert c["end"] == T0 + 3000  # every participant quiet (there are none): its last reading
    assert js["inhibit"] is True
    js2 = J.new_journal(T0 - 10)
    ev = brisket(
        js2, members(T0 + 4000, {"4": ambient("Pit")}, 2), [T0 + 4300, RESET, iso(T0 + 4300)]
    )
    same([e["type"] for e in ev], ["open", "close", "open"])
    assert ev[1]["cook"]["ended_by"] == "new_cook"
    assert js2["open"]["pending_close"] is None
    assert js2["open"]["start"] == T0 + 4300


def test_a_provisional_cook_whose_probe_leaves_is_dropped_on_its_own_timer():
    js = J.new_journal(T0 - 10)
    feed2(
        js,
        [members(T0 - 8, BRISKET), [T0, P(1), "20"], members(T0 + 600, {"4": ambient("Pit")}, 2)],
    )
    assert js["open"]["provisional"]
    assert js["open"]["pending_close"] is None  # only a real cook waits for a replacement
    events = []
    J.advance(js, T0 + 2 * 3600 - 1, events)
    same(types(events), [])
    J.advance(js, T0 + 2 * 3600, events)
    same(types(events), ["drop"])


def test_an_initially_empty_configuration_does_not_wait_to_close():
    js = J.new_journal(T0 - 10)
    ev = feed2(
        js,
        [
            members(T0 - 8, {"4": ambient("Pit")}),
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(4), "110"],
            members(T0 + 600, {"4": ambient("Smoker")}, 2),
        ],
    )
    assert js["open"]["pending_close"] is None
    ev += feed2(js, [[T0 + 2 * 3600, P(4), "111"]])
    same(types(ev), ["open", "drop"])  # an explicit cook with no meat reading, as before


def test_a_cook_waiting_to_close_is_not_ended_by_an_old_valid_reading():
    # the probe went unknown at 60 and is removed just before 6 h without a valid reading:
    # nothing but the deadline may end the cook, not the device-off rule
    js = J.new_journal(T0 - 10)
    retired_at = T0 + 6 * 3600 - 60
    brisket(js, [T0 + 3060, P(1), "unavailable"])
    feed2(js, [members(retired_at, {"4": ambient("Pit")}, 2)])
    events = []
    J.advance(js, retired_at + 120, events)  # 6 h after the last valid reading
    same(types(events), [])
    J.advance(js, retired_at + 1801, events)
    same([(e["type"], e["cook"]["ended_by"], e["cook"]["end"]) for e in events],
         [["close", "channels_removed", retired_at]])  # fmt: skip


def test_an_activation_at_the_deadline_still_joins_the_cook():
    js = J.new_journal(T0 - 10)
    brisket(js, members(T0 + 4000, {"4": ambient("Pit")}, 2))
    ev = feed2(js, [members(T0 + 5800, {"5": internal("Brisket"), "4": ambient("Pit")}, 3)])
    same(types(ev), [])
    same(js["open"]["waiting"], {"5": 5805})
    events = []
    J.advance(js, T0 + 9000, events)
    same(types(events), [])


def test_a_retired_probe_activated_again_rejoins_after_its_gap():
    js = J.new_journal(T0 - 10)
    both = {"1": internal("Brisket"), "2": internal("Ribs"), "4": ambient("Pit")}
    feed2(
        js,
        [
            members(T0 - 8, both),
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0, P(2), "30"],
            members(T0 + 1000, {"1": internal("Brisket"), "4": ambient("Pit")}, 2),
            [T0 + 1500, P(2), "40"],  # removed: ignored
            members(T0 + 2000, both, 3),
            [T0 + 2100, P(2), "50"],
        ],
    )
    c = js["open"]
    same(c["retired"], {})
    same(c["probes"]["2"]["series"], [[5, 3000], [1005, None], [2105, 5000]])


def test_a_pit_that_leaves_stops_its_statistics():
    js = J.new_journal(T0 - 10)
    both = {"1": internal("Brisket"), "4": ambient("Pit"), "b": ambient("Oven")}
    feed2(
        js,
        [
            members(T0 - 8, both),
            [T0 - 5, RESET, iso(T0 - 5)],
            [T0, P(1), "20"],
            [T0, P("b"), "200"],
            members(T0 + 1000, {"1": internal("Brisket"), "4": ambient("Pit")}, 2),
            [T0 + 1100, P("b"), "210"],
        ],
    )
    same(js["open"]["pits"]["b"], [[5, 20000], [1005, None]])
    assert J.held_stats(js["open"]["pits"]["b"], 3000)["covered"] == 1000


def test_a_channel_not_in_the_membership_takes_no_part():
    js = J.new_journal(T0 - 10)
    feed2(js, [members(T0 - 8, BRISKET), [T0, P(2), "20"], [T0 + 600, P(2), "40"]])
    assert js["open"] is None  # probe 2 is not active: no provisional cook from it
    brisket(js, [T0 + 3100, P(2), "50"], [T0 + 3100, P("b"), "300"])
    assert "2" not in js["open"]["probes"]
    assert "b" not in js["open"]["pits"]


def test_a_schema_1_journal_state_migrates_with_its_open_cook():
    legacy = {
        "schema": 1,
        "last": {"sensor.inkbird_probe_1": {"t": T0 + 600, "v": "40"}},
        "presses": {"new_cook": T0 - 5},
        "since": T0 - 86400,
        "now": T0 + 600,
        "open": {
            "schema": 1, "id": J.cook_id(T0 - 5), "start": T0 - 5, "end": None,
            "status": "open", "provisional": False, "started_by": "new_cook",
            "ended_by": None, "watchdog": False, "name": "", "notes": "",
            "probes": {"1": {"first": 20, "min": 20, "series": [[5, 2000], [605, 4000]]}},
            "pit": [[5, 11000]], "targets": {"1": [[0, 93]]}, "predictions": {}, "fills": [],
            "lastObs": T0 + 600, "lastValidAt": T0 + 600, "unplugged": {}, "off": {},
        },
        "inhibit": False,
    }  # fmt: skip
    js = J.migrate_journal(J.clone(legacy))
    assert js["schema"] == 2
    assert js["members"] is None
    same(js["last"], legacy["last"])
    c = js["open"]
    assert "pit" not in c
    same(c["pits"], {"4": [[5, 11000]]})
    same(c["channels"], {"1": internal("Probe 1"), "4": ambient("Pit")})
    fresh = J.new_journal(T0 - 10)
    feed2(fresh, [[T0 - 5, RESET, iso(T0 - 5)]])
    same(list(c), list(fresh["open"]))  # a schema-2 cook's fields, in its order
    s = J.summarize(c, T0 + 600)
    same(s["pits"]["4"]["name"], "Pit")
    assert s["schema"] == 2
    assert s["probes"][0]["name"] == "Probe 1"


# ---- JavaScript semantics (expected values: what V8 gives) ----


@pytest.mark.parametrize(
    ("x", "expected"),
    [(2.5, 3), (-2.5, -2), (0.5, 1), (-0.5, 0), (0.49999999999999994, 0), (699.72, 700)],
)
def test_js_round_takes_halves_toward_plus_infinity(x, expected):
    assert J.js_round(x) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12.5abc", "12.5"),
        (" \n12", "12"),
        ("\xa012", "12"),
        (".5", "0.5"),
        ("5.", "5"),
        ("1e", "1"),
        ("-.5e3x", "-500"),
        ("Infinityx", "Infinity"),
        ("0x10", "0"),
        ("1_0", "1"),
        ("nan", "NaN"),
        ("inf", "NaN"),
        ("unavailable", "NaN"),
        ("", "NaN"),
    ],
)
def test_parse_float_reads_a_numeric_prefix(text, expected):
    assert J.number_text(J.parse_float(text)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-11T01:19:19.953190+00:00", 1791681559953),
        ("2026-10-11T01:19:19.9999999Z", 1791681559999),
        ("2026-10-11T01:19:19+03:00", 1791670759000),
        ("2026-10-11 01:19:19.953190+00:00", 1791681559953),
        ("2026-10-11T01:19:19.953190+0530", 1791661759953),
        ("2026-10-11", 1791676800000),
        ("2026-02-30T00:00:00Z", 1772409600000),
        ("2026-10-11T24:00Z", 1791763200000),
        ("2026-10-11T24:00:01Z", None),
        ("2026-10-11T01:19:60Z", None),
        ("2026-10-11+03:00", None),
        (" 2026-10-11T01:19:19Z", None),
        ("unknown", None),
        (None, None),
    ],
)
def test_date_parse_reads_iso_times_as_v8_does(text, expected):
    assert J.date_parse(text) == expected


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (93.0, "93"),
        (-0.0, "0"),
        (1e21, "1e+21"),
        (1e-7, "1e-7"),
        (0.000001, "0.000001"),
        (0.00001, "0.00001"),
        (2.0**60, "1152921504606847000"),
        (1791681559.9531896, "1791681559.9531896"),
        (69.97, "69.97"),
    ],
)
def test_numbers_print_as_javascript_prints_them(x, expected):
    assert J.number_text(x) == expected


def test_json_orders_integer_keys_first_and_writes_what_json_stringify_writes():
    separator = chr(0x2028)  # JSON.stringify leaves it as it is
    value = {"b": 1, "2": [math.inf, -0.0, math.nan], "1": separator, "a": {"10": 1, "9": 2}}
    value["x"] = J.UNDEFINED
    expected = '{"1":"' + separator + '","2":[null,0,null],"b":1,"a":{"9":2,"10":1}}'
    assert J.to_json(value) == expected
    assert J.to_json("ab" + chr(0xD83D)) == '"ab\\ud83d"'


def test_iso_truncates_to_the_millisecond():
    assert J.iso(1791681559.9539) == "2026-10-11T01:19:19.953Z"
    assert J.cook_id(1791681559.9531896) == "20261011T011920Z"
