"""Turn a temperature source's Home Assistant state into a reading in °C.

Every consumer (the estimator, the journal, the chart series) takes readings from here, so
a source is interpreted one way only. A reading is one of:

  valid         a temperature, in °C
  disconnected  the probe is known to be out: the connection entity bound to the channel
                says off, or the source's decoder recognises its no-probe value
  gap           nothing is known: unavailable, unknown, or a connection entity that is
                itself unavailable says nothing about the probe
  connected     a gap in the value, but the bound connection entity says the probe is in
                (a disconnection has ended; for the estimator it is a gap like any other)
  invalid       a value no cooking probe reads (unparseable, an unsupported unit, outside
                the plausible range): a fault, not a disconnection

Only `disconnected` may end a probe's session or a cook; a gap or an invalid value is
silence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

VALID: Final = "valid"
DISCONNECTED: Final = "disconnected"
GAP: Final = "gap"
INVALID: Final = "invalid"
# a gap whose connection entity says the probe is in: whatever disconnection ran has ended
CONNECTED: Final = "connected"

# Plausible probe readings, °C. Nothing a cook measures is outside (-40, 1000): pizza ovens
# run near 500 °C, and the usual sentinels (0x7FFF or 0xFFFF tenths) are far above.
MIN_C: Final = -40.0
MAX_C: Final = 1000.0
# The Inkbird IBBQ series reads 6553.5 °F (3623.06 °C) with no probe in the socket.
INKBIRD_UNPLUGGED_C: Final = 3000.0

_NO_STATE: Final = frozenset({None, "", "unavailable", "unknown"})


@dataclass(frozen=True, slots=True)
class Reading:
    """One normalised observation: kind and, when valid, the temperature in °C."""

    kind: str
    value: float | None = None

    @property
    def valid(self) -> bool:
        return self.kind == VALID


GAP_READING: Final = Reading(GAP)
CONNECTED_READING: Final = Reading(CONNECTED)
DISCONNECTED_READING: Final = Reading(DISCONNECTED)
INVALID_READING: Final = Reading(INVALID)


def to_celsius(value: float, unit: str | None) -> float | None:
    """`value` in °C, or None for a unit that is not a temperature unit."""
    if unit == "°C":
        return value
    if unit == "°F":
        return (value - 32.0) * 5.0 / 9.0
    if unit == "K":
        return value - 273.15
    return None


def normalize(
    state: str | None,
    unit: str | None,
    decoder: str = "generic",
    connection: str | None = None,
) -> Reading:
    """Normalise a source state.

    `unit` is the source's unit_of_measurement at the time of the state, `decoder` the
    channel's ("generic" or "inkbird"), `connection` the state of the channel's connection
    entity at that time, or None when the channel has none. A connection entity that is
    bound but neither on nor off (unavailable, unknown, not yet seen) leaves the probe's
    state unknown: a gap, whatever the temperature says.
    """
    if connection is not None and connection != "on":
        return DISCONNECTED_READING if connection == "off" else GAP_READING
    reading = _temperature(state, unit, decoder)
    if connection == "on" and reading.kind != VALID:
        # a bound connection entity that says on: the probe is in, whatever its value says
        return CONNECTED_READING
    return reading


def _temperature(state: str | None, unit: str | None, decoder: str) -> Reading:
    if state in _NO_STATE:
        return GAP_READING
    try:
        raw = float(state)
    except TypeError, ValueError:
        return INVALID_READING
    if not math.isfinite(raw):
        return INVALID_READING
    value = to_celsius(raw, unit)
    if value is None:
        return INVALID_READING
    if decoder == "inkbird" and value >= INKBIRD_UNPLUGGED_C:
        return DISCONNECTED_READING
    if not MIN_C < value < MAX_C:
        return INVALID_READING
    return Reading(VALID, value)
