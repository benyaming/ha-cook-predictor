"""Readings from source states: one interpretation for every consumer."""

import pytest

from custom_components.cook_predictor.core.normalize import (
    CONNECTED,
    DISCONNECTED,
    GAP,
    INVALID,
    VALID,
    normalize,
)


@pytest.mark.parametrize(
    ("state", "unit", "decoder", "connection", "kind", "value"),
    [
        ("64.5", "°C", "generic", None, VALID, 64.5),
        ("212", "°F", "generic", None, VALID, 100.0),
        ("373.15", "K", "generic", None, VALID, 100.0),
        ("unavailable", "°C", "generic", None, GAP, None),
        ("unknown", "°C", "generic", None, GAP, None),
        (None, "°C", "generic", None, GAP, None),
        ("abc", "°C", "generic", None, INVALID, None),
        ("nan", "°C", "generic", None, INVALID, None),
        ("64.5", None, "generic", None, INVALID, None),
        ("64.5", "%", "generic", None, INVALID, None),
        # a hot oven is a reading, not a disconnected probe
        ("520", "°C", "generic", None, VALID, 520.0),
        # an error value is a fault, not an unplug, unless the decoder knows it
        ("3623.05555555556", "°C", "generic", None, INVALID, None),
        ("3623.05555555556", "°C", "inkbird", None, DISCONNECTED, None),
        ("6553.5", "°F", "inkbird", None, DISCONNECTED, None),
        ("25.1", "°C", "inkbird", None, VALID, 25.1),
        ("-45", "°C", "generic", None, INVALID, None),
        # a bound connection entity: off is disconnected, on defers to the temperature, and
        # anything else (the binding exists but its state is unknown) is a gap
        ("71.2", "°C", "generic", "off", DISCONNECTED, None),
        ("71.2", "°C", "generic", "on", VALID, 71.2),
        ("71.2", "°C", "generic", "unavailable", GAP, None),
        ("71.2", "°C", "generic", "unknown", GAP, None),
        ("71.2", "°C", "generic", "", GAP, None),
        ("unavailable", "°C", "generic", "on", CONNECTED, None),
        # the probe is in whatever the value says: a fault is not an unplug either
        ("nan", "°C", "generic", "on", CONNECTED, None),
        ("1500", "°C", "generic", "on", CONNECTED, None),
    ],
)
def test_normalize(state, unit, decoder, connection, kind, value) -> None:
    reading = normalize(state, unit, decoder, connection)
    assert reading.kind == kind
    if value is None:
        assert reading.value is None
    else:
        assert reading.value == pytest.approx(value)
