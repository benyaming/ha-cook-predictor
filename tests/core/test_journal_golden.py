"""Two real cooks through the Python journal, against what the reference journal.js made of
them.

The fixtures in tests/fixtures/journal/ come from a Node harness around the reference (kept
outside this repository):
  replay-N.json         a cook's input, rebuilt from the recorder's probe readings plus the
                        targets, predictor reports and presses synthesised from the journal's
                        own record of the cook, and everything journal.js returned for it:
                        each event with its cook as it was then, and at the last reading and
                        after the close, the journal state and summarize()/derive()
  cook-N.json           the real cook file, byte for byte as the journal wrote it
  cook-N.expected.json  derive() and the list entry (summarize()) the reference gave for it
Cook 1 started from the probe and ends with a Finish press 60 s after its last reading;
cook 2 started with New cook and ends unplugged. Everything is sanitised: times shifted so
the first reading is at 2026-01-01 00:00 UTC, entities renamed, names and notes empty.

The reference is schema 1 and read raw readings. The replays go in as the runtime
normalises them (the Inkbird's no-probe value is "disconnected", the one pit is channel
"4", a membership row makes probes 1 to 3 and the pit active), and the schema-2 records,
states and summaries that come out are translated back to schema 1 (pits["4"] is pit; the
channels, membership and their bookkeeping go) before they are compared. The real cook
files are schema 1 and are read as they are: their summaries and derivations are the
reference's in the shape both schemas share. Equal means equal as JSON text, so key order
and the way each number prints count too.
"""

import hashlib
import json
from pathlib import Path

import pytest

from custom_components.cook_predictor.core import journal as J

FIXTURES = Path(__file__).parent.parent / "fixtures" / "journal"
MEMBERS = "journal.members"
SCHEMA_2_ONLY = ("channels", "ambients", "retired", "waiting", "pending_close")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def assert_same(actual, expected):
    assert json.loads(J.to_json(actual)) == expected  # the values, with pytest's diff
    assert J.to_json(actual) == J.to_json(expected)  # and the text: key order, number forms


# ---- the replays in, as the runtime gives them ----


def ids_for(reference_ids):
    ids = {k: v for k, v in reference_ids.items() if k != "ambient"}
    return {**ids, "pits": {"4": reference_ids["ambient"]}, "members": MEMBERS}


def membership(ids):
    active = {
        key: {"name": f"Probe {key}", "role": J.INTERNAL, "ambient": "4"} for key in ids["probes"]
    }
    active["4"] = {"name": "Pit", "role": J.AMBIENT, "ambient": None}
    return {"active": active, "rev": 1}


def normalised(row, channels):
    t, entity_id, state = row[:3]
    if entity_id in channels and (J.num(state) or 0) >= 500:
        state = J.DISCONNECTED  # the Inkbird reads 3623 C with no probe in the socket
    return [t, entity_id, state, row[3] if len(row) > 3 else None]


# ---- what comes out, as the reference has it ----


def cook_v1(c):
    out = {}
    for field, value in c.items():
        if field == "schema":
            out[field] = 1
        elif field == "pits":
            out["pit"] = value.get("4", [])
        elif field not in SCHEMA_2_ONLY:
            out[field] = value
    return out


def state_v1(js, raw):
    """`raw`: (entity, t) -> the state as the reference read it, for `last`, which keeps
    each entity's state as given (normalised here).
    """
    out = {}
    for field, value in js.items():
        if field == "schema":
            out[field] = 1
        elif field == "last":
            out[field] = {
                k: {**v, "v": raw.get((k, v["t"]), v["v"])}
                for k, v in value.items()
                if k != MEMBERS
            }
        elif field == "open":
            out[field] = None if value is None else cook_v1(value)
        elif field != "members":
            out[field] = value
    return out


def summary_v1(s):
    out = {}
    for field, value in s.items():
        if field == "schema":
            out[field] = 1
        elif field == "probes":
            out[field] = [
                {"n": int(p["key"]), **{f: x for f, x in p.items() if f not in ("key", "name")}}
                for p in value
            ]
        elif field == "pits":
            out["pit"] = {f: x for f, x in value["4"].items() if f != "name"}
        else:
            out[field] = value
    return out


def derived_v1(d):
    return {
        ("pit" if field == "pits" else field): (value["4"] if field == "pits" else value)
        for field, value in d.items()
    }


def check(js, events, expected, now, raw, unplugged=None):
    """`unplugged`: the open cook's unplug timers as the reference has them, where the
    journal now differs on purpose (UNKNOWN_AFTER_UNPLUG).
    """
    state = state_v1(js, raw)
    if unplugged is not None:
        state["open"]["unplugged"] = unplugged
    if "state" in expected:
        assert_same(state, expected["state"])
    else:
        digest = hashlib.sha256(J.to_json(state).encode("utf-8")).hexdigest()
        assert digest == expected["state_sha256"]
    assert expected["now"] == now
    if expected["open"] is None:
        assert js["open"] is None
    else:
        assert_same(summary_v1(J.summarize(js["open"], now)), expected["open"]["summary"])
        assert_same(derived_v1(J.derive(js["open"], now)), expected["open"]["derived"])
    closed = [e["cook"] for e in events if e["type"] == "close"]
    assert_same(
        [
            {"summary": summary_v1(J.summarize(c, now)), "derived": derived_v1(J.derive(c, now))}
            for c in closed
        ],
        expected["closed"],
    )


@pytest.mark.parametrize("case", [1, 2])
def test_a_real_cook_replays_to_the_reference_record(case):
    fx = load(f"replay-{case}.json")
    ids = ids_for(fx["ids"])
    kind = J.kinds(ids)
    channels = {*ids["probes"].values(), *ids["pits"].values()}
    js = J.new_journal(fx["since"])
    rows = fx["rows"]
    raw = {(row[1], row[0]): row[2] for row in rows}
    assert J.observe(js, kind, MEMBERS, rows[0][0], "1", membership(ids)) == []
    events = []

    def record(step, out):
        events.extend({"step": step, "type": e["type"], "cook": J.clone(e["cook"])} for e in out)

    for i, row in enumerate(rows):
        t, entity_id, state, attrs = normalised(row, channels)
        record(i, J.observe(js, kind, entity_id, t, state, attrs))
    first, final = fx["checks"]
    # cook 2 is the user's habit, the probes unplugged and then the thermometer switched
    # off: it closes "unplugged" 30 min after the unplug, as the reference did
    check(js, events, first, rows[-1][0], raw)
    out = []
    if "observe" in fx["close"]:
        t, entity_id, state = fx["close"]["observe"]
        out = J.observe(js, kind, entity_id, t, state)
        now = t
    else:
        now = fx["close"]["advance"]
        J.advance(js, now, out)
    record(len(rows), out)
    check(js, events, final, now, raw)
    assert_same([{**e, "cook": cook_v1(e["cook"])} for e in events], fx["events"])
    assert [e["type"] for e in events][-1] == "close"
    assert events[-1]["cook"]["schema"] == J.SCHEMA
    assert events[-1]["cook"]["channels"]["4"]["name"] == "Pit"


# ---- the real cook files, schema 1 ----


def expected_summary(reference):
    """The reference's list entry in the shape both schemas share."""
    out = {}
    for field, value in reference.items():
        if field == "probes":
            out[field] = [
                {
                    "n": p["n"],
                    "key": str(p["n"]),
                    "name": f"Probe {p['n']}",
                    **{f: x for f, x in p.items() if f != "n"},
                }
                for p in value
            ]
        elif field == "pit":
            out["pits"] = {"pit": {"name": "Pit", **value}}
        else:
            out[field] = value
    return out


def expected_derived(reference):
    return {
        ("pits" if field == "pit" else field): ({"pit": value} if field == "pit" else value)
        for field, value in reference.items()
    }


@pytest.mark.parametrize("case", [1, 2])
def test_a_real_cook_file_reads_back_unchanged(case):
    text = (FIXTURES / f"cook-{case}.json").read_text(encoding="utf-8")
    assert J.to_json(J.from_json(text)) == text


@pytest.mark.parametrize("case", [1, 2])
def test_a_real_cook_file_derives_and_summarizes_as_the_reference(case):
    cook = load(f"cook-{case}.json")
    expected = load(f"cook-{case}.expected.json")
    assert_same(J.derive(cook, expected["now"]), expected_derived(expected["derived"]))
    assert_same(J.summarize(cook, expected["now"]), expected_summary(expected["summary"]))
    # closed: the time asked does not matter
    assert_same(J.summarize(cook), expected_summary(expected["summary"]))


@pytest.mark.parametrize("case", [1, 2])
def test_a_real_cook_migrated_to_schema_2_keeps_its_figures(case):
    cook = load(f"cook-{case}.json")
    expected = load(f"cook-{case}.expected.json")
    migrated = J.migrate_journal({"schema": 1, "open": cook})["open"]
    assert migrated["schema"] == J.SCHEMA
    assert "pit" not in migrated
    summary = J.summarize(migrated, expected["now"])
    reference = expected_summary(expected["summary"])
    assert summary["pits"] == {"4": reference["pits"]["pit"]}
    assert [{**p, "n": int(p["key"])} for p in summary["probes"]] == reference["probes"]
    derived = J.derive(migrated, expected["now"])
    assert derived["pits"] == {"4": expected["derived"]["pit"]}
    assert derived["probes"] == expected["derived"]["probes"]
