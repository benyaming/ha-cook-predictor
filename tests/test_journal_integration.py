"""A cook through the integration: New cook, heating, Finish, then the journal and its commands."""

from __future__ import annotations

import math

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from .conftest import temp_state
from .test_init import setup


async def settle(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    runtime = entry.runtime_data
    while runtime._job is not None:
        await runtime._job
    await hass.async_block_till_done()


async def journal_ready(hass: HomeAssistant, entry: MockConfigEntry):
    runtime = entry.runtime_data
    for _ in range(50):
        if runtime.journal.manager.ready:
            break
        await hass.async_block_till_done()
    assert runtime.journal.manager.ready
    return runtime.journal


async def test_a_cook_goes_into_the_journal(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    temp_state(hass, "sensor.probe_1", 5.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    journal = await journal_ready(hass, entry)

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.cook_new_cook"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cook_journal").state == "open"

    temp = 5.0
    for _ in range(120):  # an hour of heating, a reading every 30 s
        freezer.tick(30)
        temp = 115 + (temp - 115) * math.exp(-1e-4 * 30)
        temp_state(hass, "sensor.probe_1", round(temp * 18) / 18)
        temp_state(hass, "sensor.pit", 115.0)
        await hass.async_block_till_done()
    await settle(hass, entry)

    # a second later: at the very instant of the last reading, the two clocks (time.time and
    # a state's last_updated) can differ in the last bit, and which comes first would be luck
    freezer.tick(1)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.cook_finish_cook"}, blocking=True
    )
    await hass.async_block_till_done()
    await journal.manager.drain()
    assert hass.states.get("sensor.cook_journal").state == "finished"

    cooks = (await journal.list())["cooks"]
    assert len(cooks) == 1
    cook = cooks[0]
    assert cook["ended_by"] == "finish"
    assert [p["name"] for p in cook["probes"]] == ["Brisket"]
    assert cook["pits"]["2"]["name"] == "Smoker"
    # 115 - 110 e^(-0.36) after the hour
    assert abs(cook["probes"][0]["max"] - (115 - 110 * math.exp(-0.36))) < 0.1

    await journal.edit(cook["id"], name="Test brisket")
    detail = await journal.get(cook["id"])
    assert detail["cook"]["name"] == "Test brisket"
    assert detail["cook"]["channels"]["1"]["name"] == "Brisket"

    # a reload reconciles from the recorder and the control log, and keeps the cook as it was
    await async_wait_recording_done(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    journal = await journal_ready(hass, entry)
    # as soon as the journal is up again, before its writes have settled, and after
    for _ in range(2):
        after = (await journal.list())["cooks"]
        assert [(c["id"], c["name"], c["ended_by"]) for c in after] == [
            (cook["id"], "Test brisket", "finish")
        ]
        await journal.manager.drain()


async def test_journal_commands_over_the_websocket(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    await setup(hass, entry)
    await journal_ready(hass, entry)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "cook_predictor/journal/list"})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert msg["result"]["cooks"] == []
    await client.send_json_auto_id(
        {"type": "cook_predictor/journal/get", "cook_id": "20260101T000000Z"}
    )
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"
    await client.send_json_auto_id({"type": "cook_predictor/journal/get", "cook_id": "nope"})
    msg = await client.receive_json()
    assert not msg["success"]
