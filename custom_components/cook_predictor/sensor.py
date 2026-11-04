"""Per probe: its status (forecast in the attributes) and finish time; the journal state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import ChannelEntity, CookEntity
from .runtime import STATUSES, CookRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[CookRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities([JournalSensor(runtime)])
    for key, subentry_id in runtime.internal_channels():
        async_add_entities(
            [StatusSensor(runtime, key, subentry_id), DoneAtSensor(runtime, key, subentry_id)],
            config_subentry_id=subentry_id,
        )


class StatusSensor(ChannelEntity, SensorEntity):
    """What the predictor says about the probe; the forecast is in the attributes (°C)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATUSES)

    def __init__(self, runtime: CookRuntime, key: str, subentry_id: str) -> None:
        super().__init__(runtime, key, subentry_id, "status")

    @property
    def native_value(self) -> str | None:
        result = self.runtime.result(self.key)
        return result["status"] if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.runtime.result(self.key)
        return result["attributes"] if result else {"key": self.key}


class DoneAtSensor(ChannelEntity, SensorEntity):
    """When the probe is predicted to reach its target; unknown without a confident forecast."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, runtime: CookRuntime, key: str, subentry_id: str) -> None:
        super().__init__(runtime, key, subentry_id, "done_at")

    @property
    def native_value(self) -> datetime | None:
        result = self.runtime.result(self.key)
        done = result and result["attributes"].get("done_at")
        return dt_util.parse_datetime(done) if done else None


class JournalSensor(CookEntity, SensorEntity):
    """The cook as the journal sees it: idle, open or finished."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "open", "finished"]

    def __init__(self, runtime: CookRuntime) -> None:
        super().__init__(runtime, "journal")

    @property
    def native_value(self) -> str | None:
        return self.runtime.journal_status()["state"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.runtime.journal_status()["attributes"]
