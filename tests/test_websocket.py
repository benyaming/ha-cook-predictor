"""The card's stream: a snapshot, then ordered deltas."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import temp_state
from .test_init import setup


async def test_snapshot_then_points(
    ha: None,
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    temp_state(hass, "sensor.probe_1", 20.0)
    temp_state(hass, "sensor.pit", 110.0)
    await setup(hass, entry)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "cook_predictor/subscribe"})
    msg = await client.receive_json()
    assert msg["success"]
    snap = (await client.receive_json())["event"]
    assert snap["type"] == "snapshot"
    channels = {c["key"]: c for c in snap["config"]["channels"]}
    assert channels["1"]["role"] == "internal" and channels["1"]["ambient"] == "2"
    assert channels["1"]["status"] == "sensor.brisket_status"
    assert channels["1"]["numbers"]["target"] == "number.brisket_target"
    assert channels["2"]["role"] == "ambient"
    assert snap["config"]["buttons"]["finish"] == "button.cook_finish_cook"
    seq = snap["seq"]

    freezer.tick(30)
    temp_state(hass, "sensor.probe_1", 21.5)
    await hass.async_block_till_done()
    ev = (await client.receive_json())["event"]
    while ev["type"] != "points":
        seq = ev["seq"]
        ev = (await client.receive_json())["event"]
    assert ev["seq"] == seq + 1
    assert ev["generation"] == snap["generation"]
    assert ev["points"]["1"][-1][1] == 21.5

    # °F at the source arrives in °C
    hass.states.async_set(
        "sensor.probe_1", "77", {"unit_of_measurement": "°F", "device_class": "temperature"}
    )
    await hass.async_block_till_done()
    ev = (await client.receive_json())["event"]
    while ev["type"] != "points":
        ev = (await client.receive_json())["event"]
    assert ev["points"]["1"][-1][1] == 25.0


async def test_not_loaded(ha: None, hass: HomeAssistant, hass_ws_client) -> None:
    # the commands exist once the integration is set up at all; with no entry, nothing streams
    assert await async_setup_component(hass, "cook_predictor", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "cook_predictor/subscribe"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"
