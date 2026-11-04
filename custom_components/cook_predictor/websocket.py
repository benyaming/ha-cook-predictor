"""Websocket commands for the card: the live cook stream and the journal.

Any signed-in user may use them, as with the dashboard itself: renaming, annotating and
deleting a past cook is part of using the card (a deleted cook stays in trash/ on disk).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .runtime import CookRuntime, JournalError

ID_SCHEMA = vol.Match(r"^\d{8}T\d{6}Z$")


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_journal_list)
    websocket_api.async_register_command(hass, ws_journal_get)
    websocket_api.async_register_command(hass, ws_journal_update)
    websocket_api.async_register_command(hass, ws_journal_delete)


def _runtime(hass: HomeAssistant) -> CookRuntime | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    return None


def _no_runtime(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_error(msg["id"], "not_loaded", "Cook Predictor is not set up or not running")


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The cook's configuration and normalised series, then every change, in order.

    The listener is registered before the snapshot is taken, so nothing falls between the
    two; each message carries the runtime generation and a sequence number, and a client
    that sees another generation (the entry reloaded) subscribes again.
    """
    runtime = _runtime(hass)
    if runtime is None:
        _no_runtime(connection, msg)
        return

    @callback
    def forward(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    unsub = runtime.stream.subscribe(forward)
    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    forward(runtime.stream.snapshot())


async def _journal_call(hass, connection, msg, call) -> None:
    runtime = _runtime(hass)
    if runtime is None:
        _no_runtime(connection, msg)
        return
    try:
        connection.send_result(msg["id"], await call(runtime.journal))
    except JournalError as err:
        connection.send_error(msg["id"], err.code, err.message)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/journal/list"})
@websocket_api.async_response
async def ws_journal_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _journal_call(hass, connection, msg, lambda j: j.list())


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/journal/get", vol.Required("cook_id"): ID_SCHEMA}
)
@websocket_api.async_response
async def ws_journal_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _journal_call(hass, connection, msg, lambda j: j.get(msg["cook_id"]))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/journal/update",
        vol.Required("cook_id"): ID_SCHEMA,
        vol.Optional("name"): vol.All(str, vol.Length(max=120)),
        vol.Optional("notes"): vol.All(str, vol.Length(max=5000)),
    }
)
@websocket_api.async_response
async def ws_journal_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _journal_call(
        hass,
        connection,
        msg,
        lambda j: j.edit(msg["cook_id"], name=msg.get("name"), notes=msg.get("notes")),
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/journal/delete", vol.Required("cook_id"): ID_SCHEMA}
)
@websocket_api.async_response
async def ws_journal_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _journal_call(hass, connection, msg, lambda j: j.delete(msg["cook_id"]))
