"""Per probe: the target, the heads-up before it, a second alert temperature and its heads-up.

Values live in the control log, so the estimator's replay and the journal know what was in
force at any moment. Temperatures are native °C with the temperature device class, so Home
Assistant shows and accepts them in the user's unit.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_ALERT_MIN, DEFAULT_ALERT_TEMP_C, DEFAULT_TARGET_C, DEFAULT_WARN_MIN
from .entity import ChannelEntity
from .runtime import CookRuntime


@dataclass(frozen=True, kw_only=True)
class SettingDescription(NumberEntityDescription):
    default: float


SETTINGS: tuple[SettingDescription, ...] = (
    SettingDescription(
        key="target",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=20,
        native_max_value=110,
        native_step=0.5,
        mode=NumberMode.BOX,
        default=DEFAULT_TARGET_C,
    ),
    SettingDescription(
        key="warn_min",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0,
        native_max_value=180,
        native_step=5,
        mode=NumberMode.BOX,
        default=DEFAULT_WARN_MIN,
    ),
    SettingDescription(
        key="alert_temp",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=20,
        native_max_value=110,
        native_step=0.5,
        mode=NumberMode.BOX,
        default=DEFAULT_ALERT_TEMP_C,
    ),
    SettingDescription(
        key="alert_min",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0,
        native_max_value=180,
        native_step=5,
        mode=NumberMode.BOX,
        default=DEFAULT_ALERT_MIN,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[CookRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for key, subentry_id in runtime.internal_channels():
        async_add_entities(
            [SettingNumber(runtime, key, subentry_id, d) for d in SETTINGS],
            config_subentry_id=subentry_id,
        )


class SettingNumber(ChannelEntity, NumberEntity):
    """One per-probe setting."""

    entity_description: SettingDescription

    def __init__(
        self, runtime: CookRuntime, key: str, subentry_id: str, description: SettingDescription
    ) -> None:
        super().__init__(runtime, key, subentry_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        return self.runtime.setting(self.key, self.entity_description.key)

    async def async_set_native_value(self, value: float) -> None:
        await self.runtime.async_set_setting(self.key, self.entity_description.key, value)
