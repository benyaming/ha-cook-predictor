"""Phone alerts: the estimator's events as notifications.

Legacy notify services (a phone's mobile_app_<name>) get the companion app's data: a tag per
probe, so a newer alert replaces an older one, a channel and a tap-through path. Notify
entities get the title and the message only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.notify import ATTR_MESSAGE, ATTR_TITLE
from homeassistant.components.notify import DOMAIN as NOTIFY_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def _hhmm(iso: str) -> str:
    when: datetime | None = dt_util.parse_datetime(iso)
    return dt_util.as_local(when).strftime("%H:%M") if when else "?"


def _dur(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes} min"


def message(name: str, event: str, a: dict[str, Any], unit: str = "°C") -> tuple[str, str] | None:
    """Title and message for one event, or None for an event that is not an alert.

    The attributes are in °C; the text is in `unit`, the user's temperature unit.
    """
    fahrenheit = unit == "°F"

    def _deg(v: float | None, delta: bool = False) -> str:
        if v is None:
            return "?"
        if fahrenheit:
            v = round(v * 9 / 5 + (0 if delta else 32), 1)
        return f"{v:g}°"

    temp = _deg(a.get("temperature"))
    if event == "approach":
        return (
            f"{name}: ~{_dur(a['eta_min'])} to {_deg(a['target'])}",
            f"Now {temp}, done around {_hhmm(a['done_at'])}.",
        )
    if event == "alert_soon":
        return (
            f"{name}: ~{_dur(a['alert_eta_min'])} to {_deg(a['alert_temp'])}",
            f"Now {temp}, reaching {_deg(a['alert_temp'])} around {_hhmm(a['alert_at'])}.",
        )
    if event == "alert":
        tail = f" {_deg(a['target'])} around {_hhmm(a['done_at'])}." if a.get("done_at") else ""
        return f"{name} reached {_deg(a['alert_temp'])}", f"Now {temp}.{tail}"
    if event == "done":
        return f"{name} reached {_deg(a['target'])}", f"Now {temp}. Ready to come off."
    if event == "stall":
        after = a.get("eta_after_stall_min")
        tail = f" Once it breaks, about {_dur(after)} more." if after is not None else ""
        return f"{name}: stall at {temp}", f"Rising {_deg(a.get('rate'), delta=True)}/h.{tail}"
    if event == "stall_end":
        tail = f", done around {_hhmm(a['done_at'])}." if a.get("done_at") else "."
        return f"{name}: the stall is over", f"Now {temp}{tail}"
    return None


async def async_send(
    hass: HomeAssistant,
    services: list[str],
    entities: list[str],
    click_path: str | None,
    key: str,
    name: str,
    event: str,
    attrs: dict[str, Any],
    only: list[str] | None = None,
) -> list[str]:
    """Send one alert to every target (or to those in `only`, a retry). A failed target
    does not stop the others. Returns the targets it could not reach ("notify.<service>"
    or "notify entities"), empty when everything went out."""
    text = message(name, event, attrs, hass.config.units.temperature_unit)
    if text is None:
        return []
    title, body = text
    failed: list[str] = []
    own = event in ("alert", "alert_soon")  # its own tag: a target alert does not replace it
    data: dict[str, Any] = {
        "tag": f"cook_predictor_{key}_alert" if own else f"cook_predictor_{key}",
        "channel": "Cooking",
        "importance": "default" if event in ("stall", "stall_end") else "high",
        "priority": "high",
        "ttl": 0,
    }
    if click_path:
        data["clickAction"] = click_path
        data["url"] = click_path
    for service in services:
        target = f"notify.{service}"
        if only is not None and target not in only:
            continue
        try:
            await hass.services.async_call(
                NOTIFY_DOMAIN,
                service,
                {ATTR_TITLE: title, ATTR_MESSAGE: body, "data": data},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Alert to %s failed", target)
            failed.append(target)
    if entities and (only is None or "notify entities" in only):
        try:
            await hass.services.async_call(
                NOTIFY_DOMAIN,
                "send_message",
                {ATTR_ENTITY_ID: entities, ATTR_TITLE: title, ATTR_MESSAGE: body},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Alert to %s failed", ", ".join(entities))
            failed.append("notify entities")
    return failed
