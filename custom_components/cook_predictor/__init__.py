"""Cook Predictor: when the cook will be done, from any temperature probes, and a cook journal."""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from . import websocket
from .const import DOMAIN
from .runtime import CookRuntime

PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_URL = f"/{DOMAIN}/frontend"
# One module loads everything the dashboard needs, in a fixed order.
CARD_MODULE = "cook-predictor-card.js"


def _frontend_version(version: str) -> str:
    """The manifest version plus a digest of the files, so a browser never keeps an old card."""
    digest = hashlib.sha256()
    for f in sorted(FRONTEND_DIR.glob("*.js")):
        digest.update(f.read_bytes())
    return f"{version}-{digest.hexdigest()[:10]}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Process-wide, once: the card's files and the websocket commands."""
    integration = await async_get_integration(hass, DOMAIN)
    version = await hass.async_add_executor_job(_frontend_version, str(integration.version))
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(FRONTEND_DIR), cache_headers=False)]
    )
    # The frontend loads before this integration when it is there at all (after_dependencies);
    # without it (a headless install) there is no dashboard to load the card into.
    if "frontend" in hass.config.components:
        add_extra_js_url(hass, f"{FRONTEND_URL}/{CARD_MODULE}?v={version}")
    websocket.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[CookRuntime]) -> bool:
    runtime = CookRuntime(hass, entry)
    await runtime.async_start()
    entry.runtime_data = runtime
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.async_stop()
        raise
    runtime.async_go_live()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry[CookRuntime]) -> None:
    """Sensors added, changed or removed, or new alert options: start again from them.

    The new runtime diffs the subentries against the control log and commits the change
    before anything uses it; the old one saves its estimator state on the way out.
    """
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry[CookRuntime]) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
