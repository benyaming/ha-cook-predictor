"""Fixtures for Cook Predictor integration tests."""

from collections.abc import Generator
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cook_predictor import runtime as runtime_module
from custom_components.cook_predictor.const import DOMAIN


@pytest.fixture
def ha(recorder_mock: Any, enable_custom_integrations: None) -> Generator[None]:
    """Home Assistant with a recorder and custom integrations, for the integration tests.

    Not autouse: the core tests need neither, and the recorder must be set up before hass,
    so an integration test asks for `ha` before `hass`.
    """
    yield


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Each test writes its control log, alert ledger and journal in its own directory."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def no_job_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process readings as they come, instead of gathering a burst for two seconds."""
    monkeypatch.setattr(runtime_module, "MIN_JOB_GAP_S", 0)


def channel(key: str, entity_id: str, role: str, name: str, **extra: Any) -> ConfigSubentryData:
    data = {
        "key": key,
        "entity_id": entity_id,
        "source_uid": None,
        "role": role,
        "name": name,
        "connection": extra.get("connection"),
        "decoder": extra.get("decoder", "generic"),
    }
    if role == "internal":
        data["ambient"] = extra.get("ambient", "auto")
    return ConfigSubentryData(data=data, subentry_type="channel", title=name, unique_id=entity_id)


def temp_state(hass: HomeAssistant, entity_id: str, value: Any, unit: str = "°C") -> None:
    hass.states.async_set(
        entity_id, str(value), {"unit_of_measurement": unit, "device_class": "temperature"}
    )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A cook with one food probe and a pit."""
    e = MockConfigEntry(
        domain=DOMAIN,
        title="Cook",
        data={},
        options={"notify": [], "notify_entities": [], "click_path": ""},
        subentries_data=[
            channel("1", "sensor.probe_1", "internal", "Brisket"),
            channel("2", "sensor.pit", "ambient", "Smoker"),
        ],
    )
    e.add_to_hass(hass)
    return e
