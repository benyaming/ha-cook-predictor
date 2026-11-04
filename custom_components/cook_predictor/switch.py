"""Per probe: whether the second alert temperature is on."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ChannelEntity
from .runtime import CookRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[CookRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for key, subentry_id in runtime.internal_channels():
        async_add_entities(
            [SecondAlertSwitch(runtime, key, subentry_id)], config_subentry_id=subentry_id
        )


class SecondAlertSwitch(ChannelEntity, SwitchEntity):
    """On: alert at the second temperature too (and the heads-up before it, if set)."""

    def __init__(self, runtime: CookRuntime, key: str, subentry_id: str) -> None:
        super().__init__(runtime, key, subentry_id, "alert_on")

    @property
    def is_on(self) -> bool:
        return bool(self.runtime.setting(self.key, "alert_on"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set_setting(self.key, "alert_on", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_setting(self.key, "alert_on", False)
