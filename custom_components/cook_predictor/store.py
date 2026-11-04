"""Files under /config/cook_predictor: atomic JSON writes, and the control log's store.

Writes to one file are serialised across the whole process, not per runtime: a reload starts
a new runtime while the old one may still have a write in the executor, and that write must
not land after the new one's. Each write takes its JSON text at the call (a later change to
the data cannot slip into it), and keeps the file's lock until the write is on disk, even
when the task waiting for it is cancelled.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .core import control


def _read(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, text: str) -> None:
    """Temp file in the same directory, flushed to disk, then renamed over the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _lock(hass: HomeAssistant, path: Path) -> asyncio.Lock:
    locks = hass.data.setdefault(f"{DOMAIN}_file_locks", {})
    return locks.setdefault(str(path), asyncio.Lock())


class JsonFile:
    """One JSON file, read and written in the executor, one operation at a time."""

    def __init__(self, hass: HomeAssistant, path: Path) -> None:
        self.hass = hass
        self.path = path

    async def _locked(self, fn: Any, *args: Any) -> Any:
        async with _lock(self.hass, self.path):
            fut = self.hass.async_add_executor_job(fn, *args)
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:
                # the write goes on in its thread: hold the lock until it is done
                await asyncio.wait([fut])
                raise

    async def async_load(self) -> Any:
        """The file's data, None if there is none. Raises ValueError if it is not JSON."""
        return await self._locked(_read, self.path)

    async def async_save(self, data: Any) -> None:
        text = json.dumps(data, separators=(",", ":"))
        await self._locked(_write_atomic, self.path, text)


class ControlStore:
    """The control log on disk. An append returns once the file holds it; nothing may act on
    an event before that.

    The file is the log, not this object's copy of it: a reload makes a new store while the
    old one may still have appends queued (a setting written by an entity of the old
    runtime). Every change, and every load, is one transaction under a lock shared by all
    stores of the file in the process: read the file, apply the change, write it, and only
    then take it as this store's view. So no store ever writes an event over one it has not
    seen."""

    def __init__(self, hass: HomeAssistant, path: Path) -> None:
        self.hass = hass
        self.file = JsonFile(hass, path)
        self.log: dict[str, Any] = control.new_log()
        self.view = control.View(self.log)

    def _transaction(self) -> asyncio.Lock:
        locks = self.hass.data.setdefault(f"{DOMAIN}_control_locks", {})
        return locks.setdefault(str(self.file.path), asyncio.Lock())

    async def _read(self) -> dict[str, Any]:
        """Raises if the log is unreadable: it is the authority, never silently replaced."""
        data = await self.file.async_load()
        if data is None:
            return control.new_log()
        if not isinstance(data, dict) or data.get("schema") != control.SCHEMA:
            schema = data.get("schema") if isinstance(data, dict) else None
            raise ValueError(f"control log schema {schema} is not {control.SCHEMA}")
        return data

    async def async_load(self) -> None:
        async with self._transaction():
            self.log = await self._read()
            self.view = control.View(self.log)

    # Each change runs as its own task, shielded from the caller: a caller cancelled while the
    # write is under way leaves the change to finish, so the view in memory is the file's.

    async def async_append(self, t: float, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return await asyncio.shield(self._change(lambda log: control.apply_events(log, t, events)))

    async def async_reserve_key(self, t: float) -> str:
        return await asyncio.shield(self._change(lambda log: control.reserve_key(log, t)))

    async def _change(self, edit: Any) -> Any:
        async with self._transaction():
            log = await self._read()
            result = edit(log)
            await self.file.async_save(log)
            self.log = log
            self.view = control.View(self.log)
            return result
