"""Renames, removals, unreadable files and writes across a reload."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cook_predictor import store as store_module
from custom_components.cook_predictor.const import DOMAIN
from custom_components.cook_predictor.store import JsonFile

from .conftest import channel, temp_state
from .test_init import setup


async def reloaded(hass: HomeAssistant, entry: MockConfigEntry):
    """The runtime the entry's own reload started, once it is ready."""
    for _ in range(100):
        await hass.async_block_till_done()
        runtime = getattr(entry, "runtime_data", None)
        if entry.state is ConfigEntryState.LOADED and runtime is not None and runtime.ready:
            return runtime
    raise AssertionError("the entry did not come back")


async def test_a_renamed_source_is_followed_at_once(
    ha: None, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    reg = er.async_get(hass)
    src = reg.async_get_or_create("sensor", "thermo", "probe-uid", suggested_object_id="probe_1")
    assert src.entity_id == "sensor.probe_1"
    data = dict(channel("1", "sensor.probe_1", "internal", "Brisket")["data"])
    data["source_uid"] = src.id
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Cook",
        data={},
        options={},
        subentries_data=[
            ConfigSubentryData(
                data=data, subentry_type="channel", title="Brisket", unique_id=src.id
            )
        ],
    )
    entry.add_to_hass(hass)
    temp_state(hass, "sensor.probe_1", 20.0)
    await setup(hass, entry)

    before = entry.runtime_data
    reg.async_update_entity("sensor.probe_1", new_entity_id="sensor.renamed_probe")
    runtime = await reloaded(hass, entry)  # the rename reloaded the entry
    assert runtime is not before
    aliases = [e for e in runtime.control.log["events"] if e["type"] == "alias"]
    assert [e["entity_id"] for e in aliases] == ["sensor.renamed_probe"]
    freezer.tick(30)
    temp_state(hass, "sensor.renamed_probe", 30.0)
    await hass.async_block_till_done()
    assert runtime.series["1"][-1][1] == 30.0


async def test_a_removed_probe_keeps_its_line_on_the_chart(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    temp_state(hass, "sensor.probe_1", 20.0)
    temp_state(hass, "sensor.pit", 100.0)
    await setup(hass, entry)
    for i in range(4):
        freezer.tick(60)
        temp_state(hass, "sensor.probe_1", 21.0 + i)
        await hass.async_block_till_done()
    sub_id = next(s.subentry_id for s in entry.subentries.values() if s.data["key"] == "1")
    freezer.tick(
        1
    )  # a reading at the very instant of the removal is the removal's, not the probe's
    hass.config_entries.async_remove_subentry(entry, sub_id)
    runtime = await reloaded(hass, entry)
    for _ in range(50):
        if runtime.series_from is not None:
            break
        await hass.async_block_till_done()
    config = runtime.card_config()
    gone = [c for c in config["channels"] if c["key"] == "1"]
    assert gone and gone[0]["active"] is False and gone[0]["until"] is not None
    assert [v for _, v in runtime.series["1"]][-1] == 24.0


async def test_an_unreadable_control_log_stops_the_setup(
    ha: None, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    path = Path(hass.config.path("cook_predictor", "control.json"))

    def corrupt() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")

    await hass.async_add_executor_job(corrupt)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_an_old_write_cannot_land_after_a_newer_one(
    ha: None, hass: HomeAssistant, monkeypatch
) -> None:
    path = Path(hass.config.path("cook_predictor", "test.json"))
    gate = threading.Event()
    real = store_module._write_atomic

    def slow_first(p: Path, text: str) -> None:
        if '"rev":1' in text:
            gate.wait(5)
        real(p, text)

    monkeypatch.setattr(store_module, "_write_atomic", slow_first)
    old = JsonFile(hass, path)
    new = JsonFile(hass, path)
    first = hass.async_create_task(old.async_save({"rev": 1}))
    await asyncio.sleep(0.05)
    first.cancel()  # the runtime that started it is unloading
    second = hass.async_create_task(new.async_save({"rev": 2}))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(first, second, return_exceptions=True)
    assert await new.async_load() == {"rev": 2}
