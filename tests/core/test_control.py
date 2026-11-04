"""The control log: channels, settings and presses over time."""

from custom_components.cook_predictor.core import control as C


def channel(entity_id="sensor.a", role="internal", name="Probe", uid=None, **kw):
    return {
        "entity_id": entity_id,
        "source_uid": uid or f"uid-{entity_id}",
        "role": role,
        "name": name,
        "ambient": kw.get("ambient", "auto"),
        "connection": kw.get("connection"),
        "decoder": kw.get("decoder", "generic"),
    }


def test_keys_are_never_handed_out_twice() -> None:
    log = C.new_log()
    assert C.reserve_key(log, 10) == "1"
    assert C.reserve_key(log, 11) == "2"
    C.note_key(log, "7")  # imported, or reserved before a write that was lost
    assert C.reserve_key(log, 12) == "8"
    C.apply_events(log, 13, [{"type": "activate", "key": "12", "channel": channel()}])
    assert C.reserve_key(log, 14) == "13"


def diff(view: C.View, desired: dict) -> list:
    events, refused = C.diff_channels(view.active(), desired, view.channels)
    assert refused == []
    return events


def test_adding_removing_and_editing_channels() -> None:
    log = C.new_log()
    C.apply_events(log, 100, diff(C.View(log), {"1": channel("sensor.a", name="Point")}))
    v = C.View(log)
    assert list(v.active()) == ["1"]
    assert v.channels["1"]["since"] == 100

    desired = {"1": channel("sensor.a", name="Flat"), "2": channel("sensor.pit", role="ambient")}
    events = diff(v, desired)
    assert [e["type"] for e in events] == ["update", "activate"]
    C.apply_events(log, 200, events)
    v = C.View(log)
    assert v.channels["1"]["name"] == "Flat"
    assert v.channels["1"]["changes"] == [{"t": 200, "name": "Flat"}]

    C.apply_events(log, 300, diff(v, {"2": desired["2"]}))
    v = C.View(log)
    assert list(v.active()) == ["2"]
    assert v.channels["1"]["until"] == 300  # retired, but still known
    assert v.channels["1"]["name"] == "Flat"


def test_a_key_never_names_another_source_or_role() -> None:
    log = C.new_log()
    C.apply_events(log, 10, diff(C.View(log), {"1": channel("sensor.a")}))
    v = C.View(log)
    for wrong in (channel("sensor.b"), channel("sensor.a", role="ambient")):
        events, refused = C.diff_channels(v.active(), {"1": wrong}, v.channels)
        assert (events, refused) == ([], ["1"])
    # a retired key does not come back, with any source
    C.apply_events(log, 20, diff(v, {}))
    v = C.View(log)
    events, refused = C.diff_channels(v.active(), {"1": channel("sensor.a")}, v.channels)
    assert (events, refused) == ([], ["1"])
    # and the log keeps the original channel even if an activate for its key slipped in
    C.apply_events(log, 30, [{"type": "activate", "key": "1", "channel": channel("sensor.b")}])
    v = C.View(log)
    assert (v.channels["1"]["entity_id"], v.channels["1"]["since"]) == ("sensor.a", 10)


def test_a_renamed_entity_is_an_alias_not_a_new_channel() -> None:
    log = C.new_log()
    C.apply_events(log, 100, diff(C.View(log), {"1": channel("sensor.a", uid="u1")}))
    v = C.View(log)
    events = diff(v, {"1": channel("sensor.renamed", uid="u1")})
    assert [e["type"] for e in events] == ["alias"]
    C.apply_events(log, 200, events)
    v = C.View(log)
    assert v.entity_ids("1") == ["sensor.a", "sensor.renamed"]
    assert v.channels["1"]["aliases"] == [{"entity_id": "sensor.a", "until": 200}]


def test_settings_in_force_at_a_time() -> None:
    log = C.new_log()
    C.apply_events(log, 100, [{"type": "setting", "key": "1", "field": "target", "value": 93}])
    C.apply_events(log, 200, [{"type": "setting", "key": "1", "field": "target", "value": 75}])
    v = C.View(log)
    assert v.setting_at("1", "target", 50, default=74) == 74
    assert v.setting_at("1", "target", 100) == 93
    assert v.setting_at("1", "target", 199.9) == 93
    assert v.setting_at("1", "target", 200) == 75
    assert v.setting_now("1", "target") == 75
    assert v.setting_now("2", "target", default=74) == 74


def test_presses() -> None:
    log = C.new_log()
    C.apply_events(log, 100, [{"type": "press", "what": "new_cook", "id": "a"}])
    C.apply_events(log, 200, [{"type": "press", "what": "finish", "id": "b"}])
    v = C.View(log)
    assert v.last_press("new_cook")["t"] == 100
    assert v.last_press("finish", before=150) is None
    assert log["rev"] == 2
