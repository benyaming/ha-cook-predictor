"""New cook and Finish cook.

A press is a command, not just a timestamp: it returns once the control log has it on
disk, and fails (so the caller sees it did not go through) otherwise. The journal takes it
at once, or from the log at its next reconcile, which a missed press brings forward; the
card says "finished" only when the journal shows it. The button's own state, which Home
Assistant writes before calling async_press, is never read back as a press.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import CookEntity
from .runtime import CookRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[CookRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities([PressButton(runtime, "new_cook"), PressButton(runtime, "finish")])


class PressButton(CookEntity, ButtonEntity):
    """New cook: every probe starts a new session, the journal a new cook. Finish: the cook ends."""

    def __init__(self, runtime: CookRuntime, what: str) -> None:
        super().__init__(runtime, what)
        self.what = what

    async def async_press(self) -> None:
        await self.runtime.async_press(self.what)
