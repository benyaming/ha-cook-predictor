"""Base entities: one device for the cook, one per internal channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

if TYPE_CHECKING:
    from .runtime import CookRuntime


def signal_channel(entry_id: str, key: str) -> str:
    return f"{DOMAIN}_{entry_id}_channel_{key}"


def signal_cook(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_cook"


class CookEntity(Entity):
    """An entity of the cook itself (the buttons, the journal)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: CookRuntime, key: str) -> None:
        self.runtime = runtime
        entry = runtime.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Cook Predictor"
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_cook(self.runtime.entry.entry_id), self._refresh
            )
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class ChannelEntity(Entity):
    """An entity of one internal channel; its device is the channel's."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: CookRuntime, key: str, subentry_id: str, kind: str) -> None:
        self.runtime = runtime
        self.key = key
        entry = runtime.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}_{kind}"
        self._attr_translation_key = kind
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{key}")},
            name=runtime.channel(key)["name"],
            manufacturer="Cook Predictor",
            model="Probe",
        )
        self.subentry_id = subentry_id

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_channel(self.runtime.entry.entry_id, self.key), self._refresh
            )
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
