"""Alerts reach the notify services once; a reload or a restart does not repeat them."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.cook_predictor.core import ledger as L

from .conftest import channel, temp_state
from .test_init import setup


async def settle(entry: MockConfigEntry, hass: HomeAssistant) -> None:
    runtime = entry.runtime_data
    while runtime._job is not None:
        await runtime._job
    await hass.async_block_till_done()


async def heat_to(
    hass, entry, freezer, until: float, temp: float = 60.0, pit: float = 115.0, k: float = 3e-4
):
    while temp < until:
        freezer.tick(30)
        temp = pit + (temp - pit) * math.exp(-k * 30)
        temp_state(hass, "sensor.probe_1", round(temp * 18) / 18)
        temp_state(hass, "sensor.pit", pit)
        await hass.async_block_till_done()
    await settle(entry, hass)
    return temp


async def test_reaching_the_target_alerts_once(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    temp_state(hass, "sensor.probe_1", 60.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    number = "number.brisket_target"
    await hass.services.async_call(
        "number", "set_value", {"entity_id": number, "value": 70}, blocking=True
    )
    await heat_to(hass, entry, freezer, 71)
    done = [c for c in calls if "reached" in c.data["title"]]
    assert len(done) == 1, [c.data["title"] for c in calls]
    assert done[0].data["title"] == "Brisket reached 70°"
    assert done[0].data["data"]["tag"] == "cook_predictor_1"

    # a reload rebuilds the estimator; the ledger keeps the alert from going out again
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    for _ in range(50):
        if runtime.ready:
            break
        await hass.async_block_till_done()
    await heat_to(hass, entry, freezer, 72, temp=71)
    assert len([c for c in calls if "reached" in c.data["title"]]) == 1


def flaky_notify(hass: HomeAssistant, fail: list[bool]) -> list[str]:
    """A notify service that raises while fail[0] is true; returns the titles delivered."""
    delivered: list[str] = []

    async def handle(call: ServiceCall) -> None:
        if fail[0]:
            raise HomeAssistantError("the phone is offline")
        delivered.append(call.data["title"])

    hass.services.async_register("notify", "mobile_app_phone", handle)
    return delivered


async def ledger_on_disk(hass: HomeAssistant) -> dict:
    path = Path(hass.config.path("cook_predictor", "alerts.json"))
    return json.loads(await hass.async_add_executor_job(path.read_text))


async def test_a_failed_alert_stays_pending_and_is_retried_once(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    fail = [True]
    delivered = flaky_notify(hass, fail)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    temp_state(hass, "sensor.probe_1", 60.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.brisket_target", "value": 70}, blocking=True
    )
    temp = await heat_to(hass, entry, freezer, 71)
    assert delivered == []
    occ = list((await ledger_on_disk(hass))["occ"].values())
    assert [o["state"] for o in occ] == ["pending"]
    assert occ[0]["payload"]["only"] == ["notify.mobile_app_phone"]

    fail[0] = False  # the phone is back
    await heat_to(hass, entry, freezer, temp + 0.5, temp=temp)
    freezer.tick(60)
    temp_state(hass, "sensor.probe_1", 72.0)
    await hass.async_block_till_done()
    await settle(entry, hass)
    assert delivered == ["Brisket reached 70°"]
    assert all(o["state"] == "sent" for o in (await ledger_on_disk(hass))["occ"].values())


async def test_an_alert_decided_before_a_setting_change_is_dropped(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    temp_state(hass, "sensor.probe_1", 30.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    runtime = entry.runtime_data
    rev = runtime.control.view.rev  # what a step in the worker was computed with
    # meanwhile the user turns the second alert off
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.brisket_second_alert"}, blocking=True
    )
    attrs = {"session_start": "2026-01-01T00:00:00.000Z", "temperature": 65.0, "alert_temp": 65}
    lk = L.latch_key("1", "alert", attrs["session_start"], 65)
    await runtime._async_publish(
        rev,
        {"1": {"status": "predicting", "attrs": attrs, "events": ["alert"]}},
        [("1", "alert", lk)],
        [],
    )
    await hass.async_block_till_done()
    assert calls == []
    assert not L.holds(runtime.ledger, lk)


async def test_an_alert_that_does_not_depend_on_the_change_still_goes_out(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    temp_state(hass, "sensor.probe_1", 30.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    runtime = entry.runtime_data
    rev = runtime.control.view.rev
    # only the heads-up lead changes while a "done" computed before it is on its way
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.brisket_warn_min", "value": 60}, blocking=True
    )
    attrs = {"session_start": "2026-01-01T00:00:00.000Z", "temperature": 74.2, "target": 74.0}
    lk = L.latch_key("1", "done", attrs["session_start"], 74.0)
    await runtime._async_publish(
        rev, {"1": {"status": "done", "attrs": attrs, "events": ["done"]}}, [("1", "done", lk)], []
    )
    await hass.async_block_till_done()
    assert [c.data["title"] for c in calls] == ["Brisket reached 74°"]


async def test_a_pending_alert_is_not_retried_once_it_is_turned_off(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    fail = [True]
    delivered = flaky_notify(hass, fail)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    temp_state(hass, "sensor.probe_1", 30.0)
    temp_state(hass, "sensor.pit", 115.0)
    await setup(hass, entry)
    runtime = entry.runtime_data
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.brisket_second_alert"}, blocking=True
    )
    # a second-alert occurrence that failed to go out, for the current session
    freezer.tick(30)
    temp_state(hass, "sensor.probe_1", 31.0)
    await hass.async_block_till_done()
    await settle(entry, hass)
    session = runtime.results["1"]["attributes"]["session_start"]
    attrs = {"session_start": session, "temperature": 60.0, "alert_temp": 60.0}
    lk = L.latch_key("1", "alert", session, 60.0)
    await runtime._async_publish(
        runtime.control.view.rev,
        {"1": {"status": "predicting", "attrs": attrs, "events": ["alert"]}},
        [("1", "alert", lk)],
        [],
    )
    assert delivered == []
    # the user turns it off; the phone comes back
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.brisket_second_alert"}, blocking=True
    )
    fail[0] = False
    for _ in range(3):
        freezer.tick(40)
        temp_state(hass, "sensor.probe_1", 32.0 + _)
        await hass.async_block_till_done()
        await settle(entry, hass)
    assert delivered == []
    assert all(o["state"] != "pending" for o in runtime.ledger["occ"].values())


async def test_a_heads_up_is_not_retried_once_its_eta_is_past_the_lead(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    fail = [True]
    delivered = flaky_notify(hass, fail)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    await setup(hass, entry)
    runtime = entry.runtime_data
    session = "2026-01-01T00:00:00.000Z"
    good = {"model": "newton", "confidence": "ok", "session_start": session}
    attrs = {
        **good,
        "temperature": 80.0,
        "target": 74.0,
        "eta_min": 29,
        "done_at": "2026-01-01T01:00:00Z",
    }
    lk = L.latch_key("1", "approach", session, 74.0)
    await runtime._async_publish(
        runtime.control.view.rev,
        {"1": {"status": "predicting", "attrs": attrs, "events": ["approach"]}},
        [("1", "approach", lk)],
        [],
    )
    assert delivered == []  # the phone was away; the heads-up waits in the outbox
    # the cook slowed: the ETA is now far outside the 30 min lead
    fail[0] = False
    runtime.results["1"] = {"status": "predicting", "attributes": {**good, "eta_min": 90}}
    for o in runtime.ledger["occ"].values():
        o["payload"]["tried"] = 0
    await runtime._async_retry_outbox(
        runtime.ledger["occ"][next(iter(runtime.ledger["occ"]))]["t"] + 60
    )
    assert delivered == []
    assert all(o["state"] != "pending" for o in runtime.ledger["occ"].values())


async def test_an_alert_turned_off_while_it_is_being_queued_does_not_go_out(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "notify": ["mobile_app_phone"]}
    )
    await setup(hass, entry)
    runtime = entry.runtime_data
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.brisket_second_alert"}, blocking=True
    )
    rev = runtime.control.view.rev
    gate = asyncio.Event()
    real_save = runtime.ledger_file.async_save

    async def held_save(data):
        await gate.wait()
        await real_save(data)

    runtime.ledger_file.async_save = held_save
    session = "2026-01-01T00:00:00.000Z"
    attrs = {"session_start": session, "temperature": 60.5, "alert_temp": 60.0}
    lk = L.latch_key("1", "alert", session, 60.0)
    publish = hass.async_create_task(
        runtime._async_publish(
            rev,
            {"1": {"status": "predicting", "attrs": attrs, "events": ["alert"]}},
            [("1", "alert", lk)],
            [],
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)  # the publish reaches its held outbox write
    # the outbox write is on its way; the user turns the second alert off
    await runtime.async_set_setting("1", "alert_on", False)
    gate.set()
    await publish
    runtime.ledger_file.async_save = real_save
    assert calls == []
    assert not L.holds(runtime.ledger, lk)


async def test_a_second_probe_alert_turned_off_during_the_first_probe_send_does_not_go_out(
    ha: None, hass: HomeAssistant
) -> None:
    entry = MockConfigEntry(
        domain="cook_predictor",
        title="Cook",
        data={},
        options={"notify": ["mobile_app_phone"]},
        subentries_data=[
            channel("1", "sensor.probe_1", "internal", "Brisket"),
            channel("3", "sensor.probe_3", "internal", "Ribs"),
        ],
    )
    entry.add_to_hass(hass)
    gate = asyncio.Event()
    titles: list[str] = []

    async def handle(call: ServiceCall) -> None:
        if call.data["title"].startswith("Brisket"):
            await gate.wait()  # the first probe's send takes its time
        titles.append(call.data["title"])

    hass.services.async_register("notify", "mobile_app_phone", handle)
    await setup(hass, entry)
    runtime = entry.runtime_data
    for key in ("1", "3"):
        await runtime.async_set_setting(key, "alert_on", True)
    rev = runtime.control.view.rev
    session = "2026-01-01T00:00:00.000Z"
    attrs = {"session_start": session, "temperature": 60.5, "alert_temp": 60.0}
    results = {k: {"status": "predicting", "attrs": attrs, "events": ["alert"]} for k in ("1", "3")}
    fired = [(k, "alert", L.latch_key(k, "alert", session, 60.0)) for k in ("1", "3")]
    publish = hass.async_create_task(runtime._async_publish(rev, results, fired, []))
    for _ in range(10):
        await asyncio.sleep(0)
    await runtime.async_set_setting("3", "alert_on", False)  # while Brisket's alert is out
    gate.set()
    await publish
    assert titles == ["Brisket reached 60°"]
