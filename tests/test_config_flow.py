"""The entry and its sensors: adding, refusing, changing."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cook_predictor.const import DOMAIN

from .conftest import temp_state


async def add_sensor(hass: HomeAssistant, entry_id: str, entity_id: str, role: str, **details):
    result = await hass.config_entries.subentries.async_init(
        (entry_id, "channel"), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"entity_id": entity_id, "role": role}
    )
    if result["type"] is not FlowResultType.FORM or result["step_id"] != "details":
        return result
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": details.get("name", entity_id), "decoder": "generic", **details}
    )


async def test_the_first_sensor_follows_the_entry(ha: None, hass: HomeAssistant) -> None:
    temp_state(hass, "sensor.probe", 21)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["next_flow"][0] == "config_subentries_flow"
    entry = result["result"]
    await hass.async_block_till_done()
    sub = await hass.config_entries.subentries.async_configure(
        result["next_flow"][1], {"entity_id": "sensor.probe", "role": "internal"}
    )
    assert sub["step_id"] == "details"
    sub = await hass.config_entries.subentries.async_configure(
        sub["flow_id"], {"name": "Brisket", "ambient": "auto", "decoder": "generic"}
    )
    assert sub["type"] is FlowResultType.CREATE_ENTRY
    data = next(iter(entry.subentries.values())).data
    assert data["key"] == "1"
    assert data["role"] == "internal"
    assert data["name"] == "Brisket"


async def test_keys_are_never_reused(ha: None, hass: HomeAssistant, entry: MockConfigEntry) -> None:
    temp_state(hass, "sensor.probe_1", 21)
    temp_state(hass, "sensor.pit", 22)
    temp_state(hass, "sensor.probe_9", 23)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # the fixture's channels are keys 1 and 2; the log has seen them, so the next is 3
    result = await add_sensor(hass, entry.entry_id, "sensor.probe_9", "internal", name="Ribs")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    keys = sorted(s.data["key"] for s in entry.subentries.values())
    assert keys == ["1", "2", "3"]
    # remove it, add another: 4, not 3 again
    sub_id = next(s.subentry_id for s in entry.subentries.values() if s.data["key"] == "3")
    hass.config_entries.async_remove_subentry(entry, sub_id)
    await hass.async_block_till_done()
    temp_state(hass, "sensor.probe_10", 24)
    result = await add_sensor(hass, entry.entry_id, "sensor.probe_10", "internal", name="Wings")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert sorted(s.data["key"] for s in entry.subentries.values()) == ["1", "2", "4"]


async def test_refusals(ha: None, hass: HomeAssistant, entry: MockConfigEntry) -> None:
    temp_state(hass, "sensor.probe_1", 21)
    temp_state(hass, "sensor.pit", 22)
    hass.states.async_set(
        "sensor.humidity", "40", {"unit_of_measurement": "%", "device_class": "temperature"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # a sensor already added is not offered, and the picker refuses it if sent anyway
    with pytest.raises(InvalidData):
        await add_sensor(hass, entry.entry_id, "sensor.probe_1", "internal")
    result = await add_sensor(hass, entry.entry_id, "sensor.humidity", "internal")
    assert result["errors"] == {"entity_id": "unsupported_unit"}


async def test_reconfigure_changes_only_what_may_change(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    temp_state(hass, "sensor.probe_1", 21)
    temp_state(hass, "sensor.pit", 22)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sub = next(s for s in entry.subentries.values() if s.data["key"] == "1")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "channel"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Point", "ambient": "none", "decoder": "generic"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    sub = entry.subentries[sub.subentry_id]
    assert (sub.title, sub.data["name"], sub.data["ambient"]) == ("Point", "Point", "none")
    assert sub.data["entity_id"] == "sensor.probe_1"
    # the runtime picked the change up through a reload and logged it
    runtime = entry.runtime_data
    updates = [e for e in runtime.control.log["events"] if e["type"] == "update"]
    assert updates and updates[-1]["changes"] == {"name": "Point", "ambient": "none"}
