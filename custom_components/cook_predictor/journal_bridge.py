"""The journal inside Home Assistant: its files, the recorder seen through the channels, and
what the runtime feeds it.

The journal (core/journal_manager.py) follows entities by id and recovers from a recorder.
Here those ids are the channels', not Home Assistant's:

  channel:<key>        a channel's readings, normalised (°C, "disconnected", or a gap), from
                       whichever entity_id its source had at the time and its connection entity
  predictor:<key>      the channel's status sensor, as Home Assistant published it
  target:<key>         the channel's target, from the control log
  press:new_cook, press:finish   accepted presses, from the control log
  members              the active channels, from the control log

so the recorder it is given answers for those ids, and nothing in the journal knows about
units, decoders, connection entities or renamed sources: channel readings come through
history.py with the binding in force at each moment, as the estimator's do.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.const import Platform
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ROLE_AMBIENT, ROLE_INTERNAL
from .core import journal_manager as jm
from .core.normalize import CONNECTED, DISCONNECTED, VALID, Reading
from .entity import signal_cook
from .history import async_channel_readings

if TYPE_CHECKING:
    from .runtime import CookRuntime

_LOGGER = logging.getLogger(__name__)

FINISHED_SHOWN_S = 12 * 3600  # a finished cook stays "finished" on the card this long


class JournalError(Exception):
    """A journal request that cannot be served: code and message for the websocket error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_CODES = {400: "invalid_format", 404: "not_found", 409: "cook_open", 500: "failed"}


def reading_state(r: Reading) -> str:
    """A normalised reading as the journal reads states."""
    if r.kind == VALID and r.value is not None:
        return repr(float(r.value))
    if r.kind == DISCONNECTED:
        return "disconnected"
    if r.kind == CONNECTED:
        return "connected"
    return "unavailable"


class LocalFs:
    """The journal's files, in the executor. A write is flushed to disk before it returns;
    the journal itself writes to a temp name and renames."""

    def __init__(self, runtime: CookRuntime) -> None:
        self.hass = runtime.hass

    async def read_text(self, path: str) -> str:
        return await self.hass.async_add_executor_job(Path(path).read_text, "utf-8")

    async def write_text(self, path: str, data: str) -> None:
        def write() -> None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

        await self.hass.async_add_executor_job(write)

    async def rename(self, source: str, target: str) -> None:
        await self.hass.async_add_executor_job(os.replace, source, target)

    async def unlink(self, path: str) -> None:
        await self.hass.async_add_executor_job(os.unlink, path)

    async def listdir(self, path: str) -> list[str]:
        return await self.hass.async_add_executor_job(os.listdir, path)

    async def mkdir(self, path: str) -> None:
        await self.hass.async_add_executor_job(lambda: os.makedirs(path, exist_ok=True))


def _ts(when: Any) -> float:
    return when.timestamp()


class ChannelRecorder:
    """The recorder, answering for the channel ids (see the module docstring)."""

    def __init__(self, runtime: CookRuntime) -> None:
        self.runtime = runtime
        self.hass = runtime.hass

    async def history(
        self, start: float, end: float, entity_ids: Sequence[str], minimal: bool
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        view = self.runtime.control.view
        channels = [
            pid.partition(":")[2]
            for pid in entity_ids
            if pid.startswith("channel:") and pid.partition(":")[2] in view.channels
        ]
        readings = await async_channel_readings(self.hass, view, channels, start, end)
        predictors = {
            pid: own
            for pid in entity_ids
            if pid.startswith("predictor:") and (own := self._status_entity(pid.partition(":")[2]))
        }
        states = await self._states(start, end, list(predictors.values()))
        out: dict[str, list[dict[str, Any]]] = {}
        for pid in entity_ids:
            kind, _, key = pid.partition(":")
            if kind == "channel" and key in readings:
                rows = [{"s": reading_state(r), "lu": t} for t, r in readings[key]]
            elif pid in predictors:
                rows = self._plain(states.get(predictors[pid], []), start, minimal)
            elif kind == "target":
                rows = self._setting_rows(key, end)
            elif kind == "press":
                rows = self._press_rows(key, end)
            elif pid == "members":
                rows = self._member_rows(end)
            else:
                continue
            if rows:
                out[pid] = _baseline(rows, start)
        return out

    async def _states(
        self, start: float, end: float, entity_ids: list[str]
    ) -> dict[str, list[State]]:
        if not entity_ids:
            return {}
        hist = await get_instance(self.hass).async_add_executor_job(
            history.get_significant_states,
            self.hass,
            dt_util.utc_from_timestamp(start),
            dt_util.utc_from_timestamp(end),
            entity_ids,
            None,
            True,  # the state in force at the start
            False,  # every change
            False,  # with attributes
            False,
        )
        return {k: [s for s in v if isinstance(s, State)] for k, v in hist.items()}

    def _status_entity(self, key: str) -> str | None:
        uid = f"{self.runtime.entry.entry_id}_{key}_status"
        return er.async_get(self.hass).async_get_entity_id(Platform.SENSOR, DOMAIN, uid)

    @staticmethod
    def _plain(states: list[State], start: float, minimal: bool) -> list[dict[str, Any]]:
        rows = []
        for s in states:
            row: dict[str, Any] = {"s": s.state, "lu": max(_ts(s.last_updated), start)}
            if not minimal:
                row["a"] = dict(s.attributes)
            rows.append(row)
        return rows

    def _setting_rows(self, key: str, end: float) -> list[dict[str, Any]]:
        series = self.runtime.control.view.settings.get(key, {}).get("target", [])
        rows = []
        for t, v in series:
            if t <= end:
                rows.append({"s": repr(float(v)), "lu": t})
        return rows

    def _press_rows(self, what: str, end: float) -> list[dict[str, Any]]:
        rows = []
        for p in self.runtime.control.view.presses:
            if p["what"] == what and p["t"] <= end:
                rows.append({"s": dt_util.utc_from_timestamp(p["t"]).isoformat(), "lu": p["t"]})
        return rows

    def _member_rows(self, end: float) -> list[dict[str, Any]]:
        rows = []
        for t, active, rev in member_timeline(self.runtime.control.log):
            if t <= end:
                rows.append({"s": str(rev), "lu": t, "a": {"active": active, "rev": rev}})
        return rows


def _baseline(rows: list[dict[str, Any]], start: float) -> list[dict[str, Any]]:
    """As Home Assistant answers: the row in force at `start` first, stamped `start`, then
    the rows after it."""
    rows = sorted(rows, key=lambda r: r["lu"])
    before = [r for r in rows if r["lu"] <= start]
    after = [r for r in rows if r["lu"] > start]
    out = []
    if before:
        out.append({**before[-1], "lu": start})
    return out + after


def member_timeline(log: Mapping[str, Any]) -> list[tuple[float, dict[str, Any], int]]:
    """(time, active channels {key: {name, role, ambient}}, revision) after each structural
    change of the control log, one entry per change time."""
    active: dict[str, dict[str, Any]] = {}
    out: list[tuple[float, dict[str, Any], int]] = []
    for ev in log["events"]:
        kind = ev["type"]
        key = ev.get("key")
        if kind == "activate" and key not in active:
            c = ev["channel"]
            active[key] = {
                "name": c.get("name"),
                "role": c.get("role"),
                "ambient": c.get("ambient"),
            }
        elif kind == "update" and key in active:
            for f in ("name", "ambient"):
                if f in ev["changes"]:
                    active[key][f] = ev["changes"][f]
        elif kind == "retire":
            active.pop(key, None)
        else:
            continue
        snapshot = {k: dict(v) for k, v in active.items()}
        if out and out[-1][0] == ev["t"]:
            out[-1] = (ev["t"], snapshot, ev["rev"])
        else:
            out.append((ev["t"], snapshot, ev["rev"]))
    return out


class JournalBridge:
    """The runtime's journal."""

    def __init__(self, runtime: CookRuntime) -> None:
        self.runtime = runtime
        self.hass = runtime.hass
        self.manager: jm.JournalManager | None = None
        self._status: dict[str, Any] = {"state": "idle", "attributes": {}}
        self._ticking: asyncio.Task | None = None

    def _ids(self) -> dict[str, Any]:
        view = self.runtime.control.view
        internals = [k for k, c in view.channels.items() if c["role"] == ROLE_INTERNAL]
        ambients = [k for k, c in view.channels.items() if c["role"] == ROLE_AMBIENT]
        return {
            "probes": {k: f"channel:{k}" for k in internals},
            "pits": {k: f"channel:{k}" for k in ambients},
            "predictors": {k: f"predictor:{k}" for k in internals},
            "targets": {k: f"target:{k}" for k in internals},
            "reset": "press:new_cook",
            "finish": "press:finish",
            "members": "members",
        }

    async def async_start(self) -> None:
        data = self.hass.data.setdefault(DOMAIN, {})
        directory = str(self.runtime.dir / "journal")
        self.manager = jm.JournalManager(
            LocalFs(self.runtime),
            ChannelRecorder(self.runtime),
            directory=directory,
            ids=self._ids(),
            build=self.runtime.build,
            previous=data.get("journal"),
            logger=_LOGGER,
        )
        data["journal"] = self.manager
        await self.manager.start()
        self._refresh_status()

    async def async_stop(self) -> None:
        if self.manager is None:
            return
        self.manager.stop()
        await self.manager.drain()

    def observe(self, kind_id: str, t: float, value: Any) -> None:
        if self.manager is None:
            return
        if kind_id.startswith("channel:"):
            self.manager.observe_live(kind_id, t, reading_state(value), None)
        elif kind_id.startswith("predictor:"):
            self.manager.observe_live(kind_id, t, value["status"], value["attributes"])
        elif kind_id.startswith("target:"):
            self.manager.observe_live(kind_id, t, repr(float(value)), None)

    def observe_members(self, t: float) -> None:
        """The applied channel set changed at t (the control log has it)."""
        if self.manager is None:
            return
        timeline = member_timeline(self.runtime.control.log)
        if timeline:
            _, active, rev = timeline[-1]
            self.manager.observe_live("members", t, str(rev), {"active": active, "rev": rev})

    def tick(self, now: float) -> None:
        if self.manager is None or (self._ticking and not self._ticking.done()):
            return

        async def run() -> None:
            await self.manager.tick(now)
            self._refresh_status()

        self._ticking = self.hass.async_create_background_task(run(), f"{DOMAIN} journal tick")

    async def async_press(self, what: str, t: float) -> None:
        if self.manager is None or not self.manager.ready:
            raise JournalNotReadyError("the journal is still starting")
        await self.manager.press(what, t)
        self._refresh_status()

    def request_reconcile(self) -> None:
        """A press the live path could not record: let the next tick reconcile at once."""
        if self.manager is not None:
            self.manager.request_reconcile()

    # ---- the journal sensor ----

    def status(self) -> dict[str, Any]:
        return self._status

    def _refresh_status(self) -> None:
        js = self.manager.journal if self.manager else None
        now = self.runtime_now()
        new: dict[str, Any]
        # started_by / ended_by and press_at let the card confirm the very press it made,
        # not some other start or end nearby (a Finish can end the cook before the press,
        # at its last reading)
        if js and js.get("open") and not js["open"].get("provisional"):
            c = js["open"]
            new = {
                "state": "open",
                "attributes": {
                    "cook_id": c["id"],
                    "start": _iso(c["start"]),
                    "started_by": c.get("started_by"),
                },
            }
        else:
            last = self._last_closed()
            if last and last.get("end") is not None and now - last["end"] < FINISHED_SHOWN_S:
                press_at = last.get("press_at")
                new = {
                    "state": "finished",
                    "attributes": {
                        "cook_id": last["id"],
                        "start": _iso(last["start"]),
                        "end": _iso(last["end"]),
                        "ended_by": last.get("ended_by"),
                        "press_at": _iso(press_at) if press_at is not None else None,
                    },
                }
            else:
                new = {"state": "idle", "attributes": {}}
        if new != self._status:
            self._status = new
            async_dispatcher_send(self.hass, signal_cook(self.runtime.entry.entry_id))

    @staticmethod
    def runtime_now() -> float:
        return dt_util.utcnow().timestamp()

    def _last_closed(self) -> dict[str, Any] | None:
        if self.manager is None:
            return None
        index = getattr(self.manager, "_index", {})
        pending = getattr(self.manager, "_pending", [])
        cooks = [*index.values(), *pending]
        cooks = [c for c in cooks if c.get("end") is not None]
        return max(cooks, key=lambda c: c["end"]) if cooks else None

    # ---- the card's journal commands ----

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except jm.JournalError as err:
            raise JournalError(_CODES.get(err.status, "failed"), err.message) from err

    async def list(self) -> Any:
        return await self._call(self._manager().list())

    async def get(self, cook_id: str) -> Any:
        return await self._call(self._manager().get(cook_id))

    async def edit(self, cook_id: str, name: str | None = None, notes: str | None = None) -> Any:
        return await self._call(self._manager().edit(cook_id, name=name, notes=notes))

    async def delete(self, cook_id: str) -> Any:
        return await self._call(self._manager().delete(cook_id))

    def _manager(self) -> jm.JournalManager:
        if self.manager is None:
            raise JournalError("not_loaded", "the journal is not running")
        return self.manager


class JournalNotReadyError(Exception):
    """The journal has not finished its startup; it takes a press from the log later."""


def _iso(t: float) -> str:
    return dt_util.utc_from_timestamp(t).isoformat()
