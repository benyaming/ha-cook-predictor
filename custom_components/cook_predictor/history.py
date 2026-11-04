"""Recorded history as channel readings: the recorder's states through core/readings.py."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .core.control import View
from .core.normalize import Reading
from .core.readings import Row, channel_readings


async def async_rows(
    hass: HomeAssistant, start: float, end: float | None, entity_ids: Iterable[str]
) -> dict[str, list[Row]]:
    """Every recorded state of each entity over [start, end] with its attributes (the unit
    is per row), led by the state in force at start: (time, state, unit), sorted."""
    ids = sorted({e for e in entity_ids if e})
    if not ids:
        return {}
    hist = await get_instance(hass).async_add_executor_job(
        history.get_significant_states,
        hass,
        dt_util.utc_from_timestamp(start),
        dt_util.utc_from_timestamp(end) if end is not None else None,
        ids,
        None,
        True,  # the state in force at the start
        False,  # every change, not only significant ones
        False,  # with attributes
        False,
    )
    out: dict[str, list[Row]] = {}
    for eid, states in hist.items():
        rows = [
            (s.last_updated.timestamp(), s.state, s.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
            for s in states
            if isinstance(s, State)
        ]
        rows.sort(key=lambda r: r[0])
        out[eid] = rows
    return out


def binding_entities(view: View, keys: Iterable[str]) -> set[str]:
    """Every source and connection entity the channels have read from."""
    ids: set[str] = set()
    for key in keys:
        for _, _, b in view.bindings(key):
            ids.add(b["entity_id"])
            if b.get("connection"):
                ids.add(b["connection"])
    return ids


async def async_channel_readings(
    hass: HomeAssistant, view: View, keys: Iterable[str], start: float, end: float | None = None
) -> dict[str, list[tuple[float, Reading]]]:
    """Each channel's readings over [start, end], with the binding in force at each moment."""
    keys = [k for k in keys if k in view.channels]
    rows = await async_rows(hass, start, end, binding_entities(view, keys))
    out: dict[str, Any] = {}
    for key in keys:
        out[key] = channel_readings(
            view.bindings(key), rows, start, end if end is not None else float("inf")
        )
    return out
