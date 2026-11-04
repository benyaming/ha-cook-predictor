"""History into readings: bindings over time, baselines, same-instant changes."""

from custom_components.cook_predictor.core.normalize import DISCONNECTED, GAP, VALID
from custom_components.cook_predictor.core.readings import channel_readings

C = "°C"


def b(entity="sensor.a", connection=None, decoder="generic"):
    return {"entity_id": entity, "connection": connection, "decoder": decoder}


def vals(readings):
    return [(t, r.kind, r.value) for t, r in readings]


def test_an_alias_reads_its_own_entity_only_from_its_time() -> None:
    rows = {
        "sensor.a": [(0, "20", C), (150, "90", C)],  # the old id, reused later by something else
        "sensor.b": [(100, "30", C), (160, "31", C)],
    }
    out = channel_readings([(0, 100, b("sensor.a")), (100, None, b("sensor.b"))], rows, 0)
    assert vals(out) == [(0, VALID, 20.0), (100, VALID, 30.0), (160, VALID, 31.0)]


def test_only_the_state_in_force_at_activation_is_read_and_once() -> None:
    rows = {"sensor.a": [(10, "20", C), (50, "25", C), (90, "30", C), (300, "31", C)]}
    out = channel_readings([(200, None, b())], rows, 0)
    assert vals(out) == [(200, VALID, 30.0), (300, VALID, 31.0)]


def test_a_source_and_its_connection_changing_together_read_once() -> None:
    rows = {
        "sensor.a": [(0, "unavailable", C), (50, "70", C)],
        "binary_sensor.plug": [(0, "unavailable", None), (50, "on", None)],
    }
    out = channel_readings([(0, None, b(connection="binary_sensor.plug"))], rows, 0)
    assert vals(out) == [(0, GAP, None), (50, VALID, 70.0)]


def test_the_connection_decides_and_a_new_binding_starts_with_both_in_force() -> None:
    rows = {
        "sensor.a": [(0, "70", C), (100, "71", C)],
        "binary_sensor.plug": [(0, "on", None), (80, "off", None)],
    }
    bindings = [(0, 90, b()), (90, None, b(connection="binary_sensor.plug"))]
    out = channel_readings(bindings, rows, 0)
    # no connection bound before 90; from 90 the plug, off since 80, says disconnected
    assert vals(out) == [(0, VALID, 70.0), (90, DISCONNECTED, None), (100, DISCONNECTED, None)]


def test_the_window_start_takes_the_state_in_force_then() -> None:
    rows = {"sensor.a": [(0, "20", C), (500, "40", C)]}
    out = channel_readings([(0, None, b())], rows, 300)
    assert vals(out) == [(300, VALID, 20.0), (500, VALID, 40.0)]


def test_a_row_at_the_window_end_is_read_inside_a_longer_binding() -> None:
    rows = {"sensor.a": [(0, "20", C), (600, "50", C)]}
    out = channel_readings([(0, 900, b())], rows, 0, 600)
    assert vals(out) == [(0, VALID, 20.0), (600, VALID, 50.0)]
    # but a row at the binding's own end is the next binding's
    out = channel_readings([(0, 600, b()), (600, None, b("sensor.a", decoder="inkbird"))], rows, 0)
    assert [t for t, _ in out] == [0, 600]


def test_a_retired_channel_reads_nothing_after_it_ends() -> None:
    rows = {"sensor.a": [(0, "20", C), (500, "40", C), (900, "50", C)]}
    out = channel_readings([(0, 600, b())], rows, 0)
    assert vals(out) == [(0, VALID, 20.0), (500, VALID, 40.0)]
