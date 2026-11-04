"""Setting the integration up, its entities, and a probe heating in front of it."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cook_predictor.const import DOMAIN

from .conftest import temp_state


def own(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    # the estimator loads in the background (an empty history here)
    for _ in range(50):
        if runtime.ready:
            break
        await hass.async_block_till_done()
    assert runtime.ready


async def test_entities_per_internal_channel(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    temp_state(hass, "sensor.probe_1", 21.0)
    temp_state(hass, "sensor.pit", 22.0)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    eid = entry.entry_id
    for platform, uid in [
        ("sensor", f"{eid}_1_status"),
        ("sensor", f"{eid}_1_done_at"),
        ("number", f"{eid}_1_target"),
        ("number", f"{eid}_1_warn_min"),
        ("number", f"{eid}_1_alert_temp"),
        ("number", f"{eid}_1_alert_min"),
        ("switch", f"{eid}_1_alert_on"),
        ("button", f"{eid}_new_cook"),
        ("button", f"{eid}_finish"),
        ("sensor", f"{eid}_journal"),
    ]:
        assert own(hass, platform, uid), uid
    # the ambient channel has no entities of its own
    assert own(hass, "sensor", f"{eid}_2_status") is None
    target = hass.states.get(own(hass, "number", f"{eid}_1_target"))
    assert float(target.state) == 74.0
    assert target.attributes["unit_of_measurement"] == "°C"


async def test_a_setting_goes_through_the_control_log(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await setup(hass, entry)
    number = own(hass, "number", f"{entry.entry_id}_1_target")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": number, "value": 93}, blocking=True
    )
    assert float(hass.states.get(number).state) == 93.0
    runtime = entry.runtime_data
    events = [
        e
        for e in runtime.control.log["events"]
        if e["type"] == "setting" and e["field"] == "target"
    ]
    assert [e["value"] for e in events] == [74.0, 93]
    # and it survives a reload
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(number).state) == 93.0


async def test_a_heating_probe_is_predicted(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    temp_state(hass, "sensor.probe_1", 5.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    status = own(hass, "sensor", f"{entry.entry_id}_1_status")
    pit, temp, k = 115.0, 5.0, 1e-4
    for _ in range(240):  # two hours, a reading every 30 s
        freezer.tick(30)
        temp = pit + (temp - pit) * pow(2.718281828459045, -k * 30)
        temp_state(hass, "sensor.probe_1", round(temp * 18) / 18)
        temp_state(hass, "sensor.pit", pit)
        await hass.async_block_till_done()
    runtime = entry.runtime_data
    while runtime._job is not None:
        await runtime._job
    await hass.async_block_till_done()
    state = hass.states.get(status)
    assert state.state == "predicting", state.attributes
    assert state.attributes["model"] == "newton"
    done = hass.states.get(own(hass, "sensor", f"{entry.entry_id}_1_done_at"))
    assert done.state not in ("unknown", "unavailable")


async def test_an_unavailable_probe_is_stale_at_once(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    temp_state(hass, "sensor.probe_1", 30.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    for i in range(20):
        freezer.tick(30)
        temp_state(hass, "sensor.probe_1", 30.0 + i)
        await hass.async_block_till_done()
    freezer.tick(30)
    hass.states.async_set("sensor.probe_1", "unavailable", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    while runtime._job is not None:
        await runtime._job
    status = hass.states.get(own(hass, "sensor", f"{entry.entry_id}_1_status"))
    assert status.state == "stale"
    assert status.attributes["reason"] == "probe unavailable"
