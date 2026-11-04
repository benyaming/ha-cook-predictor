"""A probe that changes cooker keeps its session across the reload that the change brings."""

from __future__ import annotations

import math

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cook_predictor.const import DOMAIN

from .conftest import channel, temp_state
from .test_init import setup


async def test_adding_a_pit_keeps_the_probe_session_and_sends_nothing_again(
    ha: None, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Cook",
        data={},
        options={"notify": ["mobile_app_phone"]},
        subentries_data=[channel("1", "sensor.probe_1", "internal", "Brisket")],
    )
    entry.add_to_hass(hass)
    temp_state(hass, "sensor.probe_1", 60.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.brisket_target", "value": 70}, blocking=True
    )
    temp = 60.0
    while temp < 71:
        freezer.tick(30)
        temp = 115 + (temp - 115) * math.exp(-3e-4 * 30)
        temp_state(hass, "sensor.probe_1", round(temp * 18) / 18)
        await hass.async_block_till_done()
    runtime = entry.runtime_data
    while runtime._job is not None:
        await runtime._job
    done = [c for c in calls if "reached" in c.data["title"]]
    assert len(done) == 1
    session = hass.states.get("sensor.brisket_status").attributes["session_start"]

    # now a pit sensor: on automatic the probe moves into its cooker
    await async_wait_recording_done(hass)
    freezer.tick(30)
    data = dict(channel("2", "sensor.pit", "ambient", "Smoker")["data"])
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(data=data, subentry_type="channel", title="Smoker", unique_id="sensor.pit"),
    )
    for _ in range(100):
        await hass.async_block_till_done()
        r = getattr(entry, "runtime_data", None)
        if (
            entry.state is ConfigEntryState.LOADED
            and r is not runtime
            and r is not None
            and r.ready
        ):
            break
    runtime = entry.runtime_data
    for _ in range(20):
        freezer.tick(30)
        temp = 115 + (temp - 115) * math.exp(-3e-4 * 30)
        temp_state(hass, "sensor.probe_1", round(temp * 18) / 18)
        temp_state(hass, "sensor.pit", 115.0)
        await hass.async_block_till_done()
    while runtime._job is not None:
        await runtime._job
    await hass.async_block_till_done()
    status = hass.states.get("sensor.brisket_status")
    assert status.attributes["session_start"] == session
    assert len([c for c in calls if "reached" in c.data["title"]]) == 1
