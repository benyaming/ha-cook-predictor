"""Config flow for Cook Predictor: one entry, and one subentry per temperature sensor."""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.notify import DOMAIN as NOTIFY_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    FlowType,
    OptionsFlow,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    AMBIENT_AUTO,
    AMBIENT_NONE,
    CONF_AMBIENT,
    CONF_CLICK_PATH,
    CONF_CONNECTION,
    CONF_DECODER,
    CONF_ENTITY_ID,
    CONF_KEY,
    CONF_NAME,
    CONF_NOTIFY,
    CONF_NOTIFY_ENTITIES,
    CONF_ROLE,
    CONF_SOURCE_UID,
    DECODER_GENERIC,
    DECODER_INKBIRD,
    DECODERS,
    DEFAULT_TITLE,
    DOMAIN,
    ROLE_AMBIENT,
    ROLE_INTERNAL,
    ROLES,
    SUBENTRY_CHANNEL,
)

UNITS = {UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN}
# notify services that are not a delivery target of their own
NOT_TARGETS = {"notify", "send_message", "persistent_notification"}


def _notify_services(hass: HomeAssistant) -> list[str]:
    return sorted(
        s for s in hass.services.async_services_for_domain(NOTIFY_DOMAIN) if s not in NOT_TARGETS
    )


def _options_schema(hass: HomeAssistant) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NOTIFY, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_notify_services(hass), multiple=True, custom_value=True, sort=True
                )
            ),
            vol.Optional(CONF_NOTIFY_ENTITIES, default=[]): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=NOTIFY_DOMAIN, multiple=True)
            ),
            vol.Optional(CONF_CLICK_PATH, default=""): selector.TextSelector(),
        }
    )


def _channels(entry: ConfigEntry) -> list[ConfigSubentry]:
    return [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHANNEL]


def source_unit(hass: HomeAssistant, entity_id: str) -> str | None:
    """The unit a source reports in: from its state, else from the entity registry."""
    if (state := hass.states.get(entity_id)) and (
        unit := state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    ):
        return unit
    entry = er.async_get(hass).async_get(entity_id)
    return entry.unit_of_measurement if entry else None


def _suggested_decoder(hass: HomeAssistant, entity_id: str) -> str:
    """Inkbird for the IBBQ thermometers that tuya_local exposes, otherwise generic."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry and entry.platform == "tuya_local" and "inkbird" in entity_id:
        return DECODER_INKBIRD
    return DECODER_GENERIC


def _suggested_connection(hass: HomeAssistant, entity_id: str) -> str | None:
    """A plug sensor on a device that has exactly one temperature sensor (a Combustion Gauge).

    Only a suggestion: the user sees it and can change or clear it.
    """
    reg = er.async_get(hass)
    entry = reg.async_get(entity_id)
    if not entry or not entry.device_id:
        return None
    temps, plugs = [], []
    for e in er.async_entries_for_device(reg, entry.device_id):
        dc = e.device_class or e.original_device_class
        if e.domain == "sensor" and dc == SensorDeviceClass.TEMPERATURE:
            temps.append(e.entity_id)
        elif e.domain == "binary_sensor" and dc == BinarySensorDeviceClass.PLUG:
            plugs.append(e.entity_id)
    return plugs[0] if temps == [entity_id] and len(plugs) == 1 else None


def _ambient_options(entry: ConfigEntry) -> list[selector.SelectOptionDict]:
    out = [
        selector.SelectOptionDict(value=AMBIENT_AUTO, label=AMBIENT_AUTO),
        selector.SelectOptionDict(value=AMBIENT_NONE, label=AMBIENT_NONE),
    ]
    for s in _channels(entry):
        if s.data[CONF_ROLE] == ROLE_AMBIENT:
            out.append(selector.SelectOptionDict(value=s.data[CONF_KEY], label=s.title))
    return out


def _details_schema(entry: ConfigEntry, role: str) -> vol.Schema:
    fields: dict[Any, Any] = {vol.Required(CONF_NAME): selector.TextSelector()}
    if role == ROLE_INTERNAL:
        fields[vol.Required(CONF_AMBIENT, default=AMBIENT_AUTO)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_ambient_options(entry),
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="ambient",
            )
        )
    fields[vol.Optional(CONF_CONNECTION)] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="binary_sensor")
    )
    fields[vol.Required(CONF_DECODER, default=DECODER_GENERIC)] = selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(DECODERS), translation_key="decoder")
    )
    return vol.Schema(fields)


class CookPredictorConfigFlow(ConfigFlow, domain=DOMAIN):
    """The single Cook Predictor entry; its sensors are subentries."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=DEFAULT_TITLE, data={}, options=user_input)
        return self.async_show_form(step_id="user", data_schema=_options_schema(self.hass))

    @override
    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Go straight on to the first sensor."""
        sub = await self.hass.config_entries.subentries.async_init(
            (result["result"].entry_id, SUBENTRY_CHANNEL),
            context=SubentryFlowContext(source=SOURCE_USER),
        )
        result["next_flow"] = (FlowType.CONFIG_SUBENTRIES_FLOW, sub["flow_id"])
        return result

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_CHANNEL: ChannelFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CookPredictorOptionsFlow()


class CookPredictorOptionsFlow(OptionsFlow):
    """Notifications."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(self.hass), self.config_entry.options
            ),
        )


class ChannelFlow(ConfigSubentryFlow):
    """Add a temperature sensor as a channel, or change what may change about one."""

    def __init__(self) -> None:
        self._source: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        used = [s.data[CONF_ENTITY_ID] for s in _channels(entry)]
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            if entity_id in used:
                errors[CONF_ENTITY_ID] = "already_configured"
            elif source_unit(self.hass, entity_id) not in UNITS:
                errors[CONF_ENTITY_ID] = "unsupported_unit"
            else:
                self._source = user_input
                return await self.async_step_details()
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_ID): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class=SensorDeviceClass.TEMPERATURE,
                        exclude_entities=used,
                    )
                ),
                vol.Required(CONF_ROLE, default=ROLE_INTERNAL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(ROLES),
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="role",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_details(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        entity_id = self._source[CONF_ENTITY_ID]
        role = self._source[CONF_ROLE]
        if user_input is not None:
            if entry.state is not ConfigEntryState.LOADED:
                return self.async_abort(reason="entry_not_loaded")
            reg_entry = er.async_get(self.hass).async_get(entity_id)
            unique_id = reg_entry.id if reg_entry else entity_id
            # another flow for the same sensor may have finished while this one was open
            if any(s.unique_id == unique_id for s in entry.subentries.values()):
                return self.async_abort(reason="already_configured")
            # From the control log, which outlives a lost config entry write: a removed
            # channel's key stays its own in the journal and is never handed out again.
            key = await entry.runtime_data.async_reserve_key()
            data = {
                CONF_KEY: key,
                CONF_ENTITY_ID: entity_id,
                CONF_SOURCE_UID: reg_entry.id if reg_entry else None,
                CONF_ROLE: role,
                CONF_NAME: user_input[CONF_NAME],
                CONF_CONNECTION: user_input.get(CONF_CONNECTION),
                CONF_DECODER: user_input[CONF_DECODER],
            }
            if role == ROLE_INTERNAL:
                data[CONF_AMBIENT] = user_input[CONF_AMBIENT]
            return self.async_create_entry(
                title=user_input[CONF_NAME], data=data, unique_id=unique_id
            )
        state = self.hass.states.get(entity_id)
        suggested = {
            CONF_NAME: (state.name if state else entity_id),
            CONF_DECODER: _suggested_decoder(self.hass, entity_id),
        }
        if connection := _suggested_connection(self.hass, entity_id):
            suggested[CONF_CONNECTION] = connection
        return self.async_show_form(
            step_id="details",
            data_schema=self.add_suggested_values_to_schema(
                _details_schema(entry, role), suggested
            ),
            description_placeholders={"entity_id": entity_id},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        sub = self._get_reconfigure_subentry()
        role = sub.data[CONF_ROLE]
        if user_input is not None:
            updates = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_CONNECTION: user_input.get(CONF_CONNECTION),
                CONF_DECODER: user_input[CONF_DECODER],
            }
            if role == ROLE_INTERNAL:
                updates[CONF_AMBIENT] = user_input[CONF_AMBIENT]
            return self.async_update_and_abort(
                entry, sub, title=user_input[CONF_NAME], data_updates=updates
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _details_schema(entry, role), dict(sub.data)
            ),
            description_placeholders={"entity_id": sub.data[CONF_ENTITY_ID]},
        )
