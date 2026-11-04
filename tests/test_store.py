"""The control log's store: a cancelled caller does not leave memory behind the file."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.cook_predictor import store as store_module
from custom_components.cook_predictor.store import ControlStore


async def test_a_cancelled_append_still_lands_in_memory(
    ha: None, hass: HomeAssistant, monkeypatch
) -> None:
    gate = threading.Event()
    real = store_module._write_atomic

    def slow(p: Path, text: str) -> None:
        gate.wait(5)
        real(p, text)

    monkeypatch.setattr(store_module, "_write_atomic", slow)
    store = ControlStore(hass, Path(hass.config.path("cook_predictor", "control.json")))
    await store.async_load()
    first = hass.async_create_task(
        store.async_append(10, [{"type": "press", "what": "finish", "id": "a"}])
    )
    await asyncio.sleep(0.05)
    first.cancel()  # the caller gave up while the write was on its way to disk
    gate.set()
    await asyncio.gather(first, return_exceptions=True)
    monkeypatch.setattr(store_module, "_write_atomic", real)
    await store.async_append(20, [{"type": "press", "what": "new_cook", "id": "b"}])
    assert [e["id"] for e in store.log["events"]] == ["a", "b"]
    again = ControlStore(hass, store.file.path)
    await again.async_load()
    assert [e["id"] for e in again.log["events"]] == ["a", "b"]


async def test_a_new_store_never_writes_over_an_event_an_old_one_accepted(
    ha: None, hass: HomeAssistant, monkeypatch
) -> None:
    gate = threading.Event()
    real = store_module._write_atomic

    def slow_first(p: Path, text: str) -> None:
        if '"id":"first"' in text and '"id":"second"' not in text:
            gate.wait(5)
        real(p, text)

    monkeypatch.setattr(store_module, "_write_atomic", slow_first)
    path = Path(hass.config.path("cook_predictor", "control.json"))
    old = ControlStore(hass, path)
    await old.async_load()
    press = {"type": "press", "what": "finish"}
    a = hass.async_create_task(old.async_append(10, [{**press, "id": "first"}]))
    b = hass.async_create_task(old.async_append(11, [{**press, "id": "second"}]))
    await asyncio.sleep(0.05)
    new = ControlStore(hass, path)  # a reload while the old store still has both queued
    load = hass.async_create_task(new.async_load())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(a, b, load)
    await new.async_append(12, [{**press, "id": "third"}])
    again = ControlStore(hass, path)
    await again.async_load()
    assert [e["id"] for e in again.log["events"]] == ["first", "second", "third"]
