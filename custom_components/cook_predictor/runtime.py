"""The running integration: sources in, estimator and journal, entities and alerts out.

    sources, connections ─ normalize ─┬─> worker: Model (one estimator per cooker) ─> results
    state reports (liveness) ─────────┘─> series buffer ─> websocket stream (the card)
    results ─> status entities, alerts (through the ledger), the journal

Order of authority: the control log first (every structural change, setting and press is
committed there before anything acts on it), then the journal, which keeps its own files.
The estimator's state is not saved: every start rebuilds it from the recorder's last 30 h
and the control log (the settings and the bindings in force at each moment), in the worker,
silently; the alert ledger keeps that rebuild from sending anything twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)

from . import alerts
from .const import (
    AMBIENT_AUTO,
    AMBIENT_NONE,
    CONF_AMBIENT,
    CONF_CLICK_PATH,
    CONF_CONNECTION,
    CONF_DECODER,
    CONF_ENTITY_ID,
    CONF_KEY,
    CONF_NAME,
    CONF_NOTIFY,
    CONF_NOTIFY_ENTITIES,
    CONF_ROLE,
    CONF_SOURCE_UID,
    DECODER_INKBIRD,
    DEFAULT_ALERT_MIN,
    DEFAULT_ALERT_TEMP_C,
    DEFAULT_TARGET_C,
    DEFAULT_WARN_MIN,
    DOMAIN,
    ROLE_AMBIENT,
    ROLE_INTERNAL,
    STORAGE_DIR,
    SUBENTRY_CHANNEL,
)
from .core import control
from .core import estimator as E
from .core import ledger as L
from .core.model import Model
from .core.normalize import DISCONNECTED, VALID, Reading, normalize
from .entity import signal_channel, signal_cook
from .history import async_channel_readings
from .journal_bridge import JournalBridge, JournalError
from .store import ControlStore, JsonFile

_LOGGER = logging.getLogger(__name__)

STATUSES: Final = (
    "unplugged",
    "warming",
    "predicting",
    "stall",
    "unstable",
    "stale",
    "done",
    "no_target",
)
__all__ = ["STATUSES", "CookRuntime", "JournalError"]

REPLAY_S: Final = 30 * 3600  # history the estimator is rebuilt from
SERIES_S: Final = 30 * 3600  # history the card's chart starts with
GRID_S: Final = 60
EVAL_EVERY_S: Final = 30  # flat readings still age: evaluate at least this often
MIN_JOB_GAP_S: Final = 2.0  # a burst of readings is processed together

DEFAULTS: Final = {
    "target": DEFAULT_TARGET_C,
    "warn_min": DEFAULT_WARN_MIN,
    "alert_on": False,
    "alert_temp": DEFAULT_ALERT_TEMP_C,
    "alert_min": DEFAULT_ALERT_MIN,
}

type Obs = tuple[str, str | None, float, float | None]

# The settings each alert is decided with: a change to one of these voids that alert from a
# step computed before it (the estimator decides again with the new value, silently, as it
# does for any settings change), and a pending occurrence of it is not retried.
EVENT_SETTINGS: Final = {
    "approach": ("target", "warn_min"),
    "done": ("target",),
    "alert_soon": ("target", "alert_on", "alert_temp", "alert_min"),
    "alert": ("alert_on", "alert_temp"),
}


def _build() -> str:
    """Which predictor code published a forecast (the journal keeps it with each one)."""
    core = Path(__file__).parent / "core"
    digest = hashlib.sha256()
    for name in ("estimator.py", "model.py", "normalize.py", "readings.py"):
        digest.update((core / name).read_bytes())
    return digest.hexdigest()[:12]


def _ts(when: Any) -> float:
    return when.timestamp()


def _obs(key: str, t: float, r: Reading) -> Obs:
    if r.kind == VALID:
        return ("reading", key, t, r.value)
    if r.kind == DISCONNECTED:
        return ("off", key, t, None)
    return ("gap", key, t, None)  # a gap, or an invalid value: nothing known


@dataclass(frozen=True, slots=True)
class Channel:
    """An active channel as the runtime uses it."""

    key: str
    subentry_id: str | None
    entity_id: str
    role: str
    name: str
    ambient: str | None  # resolved: an active ambient key, or None
    connection: str | None
    decoder: str
    since: float


class Stream:
    """The card's feed: a snapshot, then ordered deltas. Each message has the runtime's
    generation and a sequence number; a subscriber registered before the snapshot is
    taken misses nothing."""

    def __init__(self, runtime: CookRuntime) -> None:
        self.runtime = runtime
        self.seq = 0
        self._subs: set[Callable[[dict[str, Any]], None]] = set()

    def subscribe(self, cb: Callable[[dict[str, Any]], None]) -> CALLBACK_TYPE:
        self._subs.add(cb)

        @callback
        def unsub() -> None:
            self._subs.discard(cb)

        return unsub

    def snapshot(self) -> dict[str, Any]:
        r = self.runtime
        return {
            "type": "snapshot",
            "generation": r.generation,
            "seq": self.seq,
            "config": r.card_config(),
            "series": {k: list(v) for k, v in r.series.items()},
            "coverage_from": r.series_from,
        }

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        msg = {"type": kind, "generation": self.runtime.generation, "seq": self.seq, **payload}
        for cb in list(self._subs):
            cb(msg)

    def close(self) -> None:
        self.publish("closed", {})
        self._subs.clear()


class CookRuntime:
    """One loaded config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.generation = f"{entry.entry_id}-{time.time():.3f}"
        self.dir = Path(hass.config.path(STORAGE_DIR))
        self.control = ControlStore(hass, self.dir / "control.json")
        self.ledger_file = JsonFile(hass, self.dir / "alerts.json")
        self.ledger: dict[str, Any] = L.new_ledger()
        self.channels: dict[str, Channel] = {}
        self.subentry_of: dict[str, str] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.series: dict[str, list[list[float | None]]] = {}
        self.series_from: float | None = None
        self.stream = Stream(self)
        self.journal = JournalBridge(self)
        self.build = ""
        self.ready = False  # the estimator holds the cook so far
        self.live = False  # entities exist: alerts may go out
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{DOMAIN}_worker")
        self._model: Model | None = None  # touched on the worker thread only
        self._pending: list[Obs] = []
        self._dirty = False
        self._job: asyncio.Task | None = None
        self._last_job = 0.0
        self._stopped = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tasks: set[asyncio.Task] = set()
        self._ledger_lock = asyncio.Lock()

    # ---- lifecycle ----

    async def async_start(self) -> None:
        try:
            await self.control.async_load()
        except ValueError as err:
            # the authority on what was configured and set: never silently replaced
            raise ConfigEntryError(f"{self.control.file.path} is unreadable: {err}") from err
        self.build = await self.hass.async_add_executor_job(_build)
        try:
            self.ledger = await self.ledger_file.async_load() or L.new_ledger()
        except ValueError:
            _LOGGER.warning("The alert ledger is unreadable; starting a new one")
            self.ledger = L.new_ledger()
        await self._async_commit_subentries()
        self._build_channels()
        await self.journal.async_start()
        self._subscribe()
        self._spawn(self._async_load_estimator())
        self._spawn(self._async_load_series())

    @callback
    def async_go_live(self) -> None:
        """Entities are set up: from now on alerts go out."""
        self.live = True
        self._kick()

    async def async_stop(self) -> None:
        self._stopped = True
        self.live = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._job:
            # the job's worker call cannot be cancelled; let it finish so the state is whole
            await asyncio.gather(self._job, return_exceptions=True)
        await self.hass.async_add_executor_job(self._worker.shutdown, True)
        await self.journal.async_stop()
        self.stream.close()

    def _spawn(self, coro: Any) -> None:
        task = self.hass.async_create_background_task(coro, f"{DOMAIN} {coro.__name__}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run on the worker thread, which alone touches the model."""
        return await asyncio.wrap_future(self._worker.submit(fn, *args))

    # ---- configuration ----

    def _desired(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        desired: dict[str, dict[str, Any]] = {}
        subentry_of: dict[str, str] = {}
        reg = er.async_get(self.hass)
        for sub in self.entry.subentries.values():
            if sub.subentry_type != SUBENTRY_CHANNEL:
                continue
            d = sub.data
            entity_id = d[CONF_ENTITY_ID]
            # the source's registry entry is its identity; it may have been renamed since
            if d.get(CONF_SOURCE_UID) and (reg_entry := reg.async_get(d[CONF_SOURCE_UID])):
                entity_id = reg_entry.entity_id
            desired[d[CONF_KEY]] = {
                "entity_id": entity_id,
                "source_uid": d.get(CONF_SOURCE_UID),
                "role": d[CONF_ROLE],
                "name": d[CONF_NAME],
                "ambient": d.get(CONF_AMBIENT),
                "connection": d.get(CONF_CONNECTION),
                "decoder": d[CONF_DECODER],
            }
            subentry_of[d[CONF_KEY]] = sub.subentry_id
        return desired, subentry_of

    async def _async_commit_subentries(self) -> None:
        """The subentries are what the user wants; the control log is what was applied.
        Commit the difference, then build from the log."""
        desired, self.subentry_of = self._desired()
        view = self.control.view
        events, refused = control.diff_channels(view.active(), desired, view.channels)
        for key in refused:
            _LOGGER.error(
                "Sensor %s (key %s) conflicts with the channel the log has under that key",
                desired[key]["entity_id"],
                key,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"refused_{key}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="channel_refused",
                translation_placeholders={"name": desired[key]["name"]},
            )
        if events:
            now = time.time()
            # settings of a new internal channel start at their defaults, at its activation
            for ev in list(events):
                if ev["type"] == "activate" and ev["channel"]["role"] == ROLE_INTERNAL:
                    for field, value in DEFAULTS.items():
                        if view.setting_now(ev["key"], field) is None:
                            events.append(
                                {
                                    "type": "setting",
                                    "key": ev["key"],
                                    "field": field,
                                    "value": value,
                                }
                            )
            # the journal reads the membership change from the log in its startup reconcile
            await self.control.async_append(now, events)

    def _resolve_ambient(self, c: dict[str, Any], ambients: list[str]) -> tuple[str | None, bool]:
        """An internal channel's pit: (ambient key or None, whether it is ambiguous)."""
        want = c.get("ambient") or AMBIENT_AUTO
        if want == AMBIENT_AUTO:
            if len(ambients) == 1:
                return ambients[0], False
            return None, len(ambients) > 1
        if want != AMBIENT_NONE and want in ambients:
            return want, False
        return None, False

    def _build_channels(self) -> None:
        view = self.control.view
        active = view.active()
        ambients = [k for k, c in active.items() if c["role"] == ROLE_AMBIENT]
        self.channels = {}
        ambiguous: list[str] = []
        for key, c in active.items():
            if key not in self.subentry_of:
                continue
            amb: str | None = None
            if c["role"] == ROLE_INTERNAL:
                amb, unclear = self._resolve_ambient(c, ambients)
                if unclear:
                    ambiguous.append(c["name"])
            self.channels[key] = Channel(
                key=key,
                subentry_id=self.subentry_of.get(key),
                entity_id=c["entity_id"],
                role=c["role"],
                name=c["name"],
                ambient=amb,
                connection=c.get("connection"),
                decoder=c["decoder"],
                since=c["since"],
            )
        if not any(c.role == ROLE_INTERNAL for c in self.channels.values()):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "no_probe",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_probe",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "no_probe")
        if ambiguous:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "pick_cooker",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="pick_cooker",
                translation_placeholders={"names": ", ".join(ambiguous)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "pick_cooker")

    def channel(self, key: str) -> dict[str, Any]:
        return self.control.view.channels[key]

    def internal_channels(self) -> list[tuple[str, str]]:
        return [
            (c.key, c.subentry_id)
            for c in self.channels.values()
            if c.role == ROLE_INTERNAL and c.subentry_id
        ]

    def _membership(self) -> tuple[dict[str, str | None], list[str]]:
        internals = {c.key: c.ambient for c in self.channels.values() if c.role == ROLE_INTERNAL}
        ambients = [c.key for c in self.channels.values() if c.role == ROLE_AMBIENT]
        return internals, ambients

    def _recent_keys(self, since: float) -> list[str]:
        """Active channels, and retired ones that were still active after `since`."""
        return [
            k
            for k, c in self.control.view.channels.items()
            if c["active"] or (c["until"] is not None and c["until"] > since)
        ]

    # ---- settings and presses ----

    def setting(self, key: str, field: str) -> Any:
        return self.control.view.setting_now(key, field, DEFAULTS[field])

    def _settings_now(self) -> dict[str, tuple[float | None, dict[str, Any]]]:
        out = {}
        for c in self.channels.values():
            if c.role == ROLE_INTERNAL:
                out[c.key] = self._settings(lambda f, k=c.key: self.setting(k, f))
        return out

    @staticmethod
    def _settings(get: Callable[[str], Any]) -> tuple[float | None, dict[str, Any]]:
        return get("target"), {
            "warn_min": get("warn_min"),
            "alert_temp": get("alert_temp") if get("alert_on") else 0,
            "alert_min": get("alert_min"),
        }

    def _settings_changed_since(self, rev: int) -> set[tuple[str, str]]:
        """(channel, setting) pairs changed after control revision `rev`."""
        return {
            (ev["key"], ev["field"])
            for ev in self.control.log["events"]
            if ev["rev"] > rev and ev["type"] == "setting"
        }

    def _event_settings(self, key: str, event: str, rev: int | None = None) -> dict[str, Any]:
        """The settings an alert depends on: now, or as they were at control revision `rev`
        (what a step computed with that revision decided with)."""
        fields = EVENT_SETTINGS.get(event, ())
        if rev is None:
            return {f: self.setting(key, f) for f in fields}
        out = {f: DEFAULTS[f] for f in fields}
        for ev in self.control.log["events"]:
            if (
                ev["rev"] <= rev
                and ev["type"] == "setting"
                and ev["key"] == key
                and ev["field"] in out
            ):
                out[ev["field"]] = ev["value"]
        return out

    def _still_holds(self, occ: str, payload: dict[str, Any]) -> bool | None:
        """Whether what a pending occurrence says is still so (see _still_current)."""
        now = self.results.get(payload["key"])
        if not now:
            return None
        status = now["status"]
        event = payload["event"]
        if status in ("stale", "unplugged"):
            return None
        if payload.get("settings") != self._event_settings(payload["key"], event):
            return False  # the alert, its threshold or its lead was changed or turned off
        if payload.get("latch") and self.ledger["latch"].get(payload["latch"]) != occ:
            return False  # its latch re-armed since, or a later occurrence holds it: void
        a = now["attributes"]
        confident = a.get("model") == "newton" and a.get("confidence") == "ok"
        if event == "done":
            return status == "done"
        if event == "alert":
            return a.get("alert_reached_at") is not None
        if event == "approach":
            # a heads-up is only true while a confident ETA is inside its lead
            lead = payload["settings"].get("warn_min")
            eta = a.get("eta_min")
            return status == "predicting" and confident and eta is not None and eta <= lead
        if event == "alert_soon":
            lead = payload["settings"].get("alert_min")
            eta = a.get("alert_eta_min")
            return status == "predicting" and confident and eta is not None and eta <= lead
        if event == "stall":
            return status == "stall"
        if event == "stall_end":
            return status != "stall"
        return True

    async def async_set_setting(self, key: str, field: str, value: Any) -> None:
        if key not in self.channels:
            raise HomeAssistantError(f"No channel {key}")
        t = time.time()
        await self.control.async_append(
            t, [{"type": "setting", "key": key, "field": field, "value": value}]
        )
        if field == "target":
            self.journal.observe(f"target:{key}", t, value)
        async_dispatcher_send(self.hass, signal_channel(self.entry.entry_id, key))
        self._dirty = True
        self._kick()

    async def async_reserve_key(self) -> str:
        return await self.control.async_reserve_key(time.time())

    async def async_press(self, what: str) -> None:
        """New cook or Finish. It took place once the control log has it on disk: the
        journal reads presses from the log in every reconcile, so one it missed live (still
        starting, a failed write) reaches it at the next one, which is asked for at once. A
        press the log could not store did not happen, anywhere, and the caller is told."""
        t = time.time()
        press_id = f"{what}-{t:.3f}"
        try:
            await self.control.async_append(t, [{"type": "press", "what": what, "id": press_id}])
        except Exception as err:
            raise HomeAssistantError(f"The press did not go through: {err}") from err
        try:
            await self.journal.async_press(what, t)
        except Exception:
            _LOGGER.warning(
                "The journal takes the %s press at its next reconcile", what, exc_info=True
            )
            self.journal.request_reconcile()
        if what == "new_cook":
            self._pending.append(("reset", None, t, None))
            self._kick()
        async_dispatcher_send(self.hass, signal_cook(self.entry.entry_id))

    # ---- sources ----

    def _connection_state(self, c: Channel) -> str | None:
        if not c.connection:
            return None
        s = self.hass.states.get(c.connection)
        return s.state if s else "unknown"

    def _read(self, c: Channel, s: State | None, connection: str | None) -> Reading:
        if s is None:
            return normalize(None, None, c.decoder, connection)
        return normalize(s.state, s.attributes.get(ATTR_UNIT_OF_MEASUREMENT), c.decoder, connection)

    def _subscribe(self) -> None:
        by_source = {c.entity_id: c for c in self.channels.values()}
        by_connection: dict[str, list[Channel]] = {}
        for c in self.channels.values():
            if c.connection:
                by_connection.setdefault(c.connection, []).append(c)
        uids = {
            sub.data.get(CONF_SOURCE_UID)
            for sub in self.entry.subentries.values()
            if sub.data.get(CONF_SOURCE_UID)
        }

        @callback
        def on_source(event: Event) -> None:
            c = by_source.get(event.data["entity_id"])
            new = event.data["new_state"]
            if c is None or new is None:
                return
            self._observe(c, _ts(new.last_updated), self._read(c, new, self._connection_state(c)))

        @callback
        def on_connection(event: Event) -> None:
            new = event.data["new_state"]
            for c in by_connection.get(event.data["entity_id"], []):
                t = _ts(new.last_updated) if new else time.time()
                state = new.state if new else "unknown"
                self._observe(c, t, self._read(c, self.hass.states.get(c.entity_id), state))

        @callback
        def on_report(event: Event) -> None:
            c = by_source.get(event.data["entity_id"])
            new = event.data["new_state"]
            if c is None or new is None:
                return
            r = self._read(c, new, self._connection_state(c))
            if r.kind == VALID:  # a report confirms a valid value, never a fault or a gap
                self._pending.append(("report", c.key, _ts(event.data["last_reported"]), r.value))
                self._kick()

        @callback
        def on_registry(event: Event) -> None:
            # a source renamed: start again, so the rename is committed as an alias and the
            # new entity_id is what is followed
            data = event.data
            if (
                data.get("action") == "update"
                and "entity_id" in data.get("changes", {})
                and (data.get("old_entity_id") in by_source or self._uid(data) in uids)
            ):
                self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

        if by_source:
            self._unsubs.append(
                async_track_state_change_event(self.hass, list(by_source), on_source)
            )
            self._unsubs.append(
                async_track_state_report_event(self.hass, list(by_source), on_report)
            )
        if by_connection:
            self._unsubs.append(
                async_track_state_change_event(self.hass, list(by_connection), on_connection)
            )
        self._unsubs.append(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, on_registry)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_timer, timedelta(seconds=EVAL_EVERY_S))
        )

    def _uid(self, data: dict[str, Any]) -> str | None:
        entry = er.async_get(self.hass).async_get(data.get("entity_id", ""))
        return entry.id if entry else None

    @callback
    def _on_timer(self, _now: Any) -> None:
        self._dirty = True
        self._kick()
        self.journal.tick(time.time())

    @callback
    def _observe(self, c: Channel, t: float, r: Reading) -> None:
        self._pending.append(_obs(c.key, t, r))
        self._series_point(c.key, t, r.value if r.kind == VALID else None)
        self.journal.observe(f"channel:{c.key}", t, r)
        self._kick()

    # ---- the series for the card ----

    def _series_point(self, key: str, t: float, v: float | None) -> None:
        pts = self.series.setdefault(key, [])
        point = [t, None if v is None else round(v, 2)]
        if pts and pts[-1][1] == point[1]:
            return
        pts.append(point)
        cut = t - SERIES_S
        while len(pts) > 1 and pts[1][0] < cut:
            pts.pop(0)
        self.stream.publish("points", {"points": {key: [point]}})

    async def _async_load_series(self) -> None:
        start = time.time() - SERIES_S
        keys = self._recent_keys(start)
        readings = await async_channel_readings(self.hass, self.control.view, keys, start)
        for key, rows in readings.items():
            pts: list[list[float | None]] = []
            for t, r in rows:
                v = round(r.value, 2) if r.kind == VALID and r.value is not None else None
                if not pts or pts[-1][1] != v:
                    pts.append([t, v])
            # live points that arrived while the history loaded come after it
            self.series[key] = pts + [
                p for p in self.series.get(key, []) if not pts or p[0] > pts[-1][0]
            ]
        self.series_from = start
        self.stream.publish(
            "series",
            {"series": {k: list(v) for k, v in self.series.items()}, "coverage_from": start},
        )

    # ---- the estimator ----

    def _moves(self, key: str, start: float, now: float) -> list[float]:
        """When, in (start, now], an internal channel may have changed cooker: its own choice
        of cooker changed, or (on automatic) an ambient channel was added or removed. Live, a
        move keeps the probe's session and relearns its fit (Model.set_membership); the
        rebuild does the same at these times."""
        view = self.control.view
        ch = view.channels[key]
        times: set[float] = set()
        prev = None
        for b_from, _, b in view.bindings(key):
            if prev is not None and b["ambient"] != prev:
                times.add(b_from)
            prev = b["ambient"]
        if (ch.get("ambient") or AMBIENT_AUTO) == AMBIENT_AUTO:
            for c in view.channels.values():
                if c["role"] == ROLE_AMBIENT:
                    times.update(e for e in (c["since"], c["until"]) if e is not None)
        return sorted(t for t in times if start < t <= now and t > ch["since"])

    async def _async_load_estimator(self) -> None:
        """Rebuild the estimator from the last 30 h: the recorded readings with the bindings
        in force at each moment, the settings in force at each grid point, the last New cook
        press; silently, then hold the latches of alerts already delivered."""
        now = time.time()
        start = now - REPLAY_S
        view = self.control.view
        internals, ambients = self._membership()
        readings = await async_channel_readings(self.hass, view, list(self.channels), start)
        press = view.last_press("new_cook", before=now)
        rev = view.rev
        model = Model()

        def target_of(key: str, t: float) -> float:
            return view.setting_at(key, "target", t, DEFAULTS["target"])

        def alerts_of(key: str, t: float) -> dict[str, Any]:
            return self._settings(lambda f: view.setting_at(key, f, t, DEFAULTS[f]))[1]

        moves = {
            k: self._moves(k, start, now)
            for k, c in self.channels.items()
            if c.role == ROLE_INTERNAL
        }

        def replay_all() -> None:
            # Each probe is rebuilt in the cooker it is in now, from its whole history: its
            # session (and the alerts sent in it) is the same one as live. At a time it
            # changed cooker it relearns its fit, as it did live; the readings before that
            # were fitted against another cooker's heat, and are only history.
            model.set_membership(internals, ambients, now)
            for g in model.groups.values():
                st = g["st"]
                st["reset_at"] = press["t"] if press else None
                group_rows: list[list[Any]] = []
                for key in [*st["probes"], *([g["ambient"]] if g["ambient"] else [])]:
                    name = "ambient" if key == g["ambient"] else key
                    for t, r in readings.get(key, []):
                        if r.kind == VALID:
                            group_rows.append([name, t, r.value])
                        elif r.kind == DISCONNECTED:
                            group_rows.append([name, t, math.nan])
                        else:
                            group_rows.append([name, t, E.GAP])
                group_rows.sort(key=lambda x: x[1])
                edges = sorted({t for k in st["probes"] for t in moves.get(k, [])})
                done = 0
                for edge in [*edges, now]:
                    upto = done
                    while upto < len(group_rows) and group_rows[upto][1] <= edge:
                        upto += 1
                    E.replay(st, group_rows[done:upto], target_of, GRID_S, None, edge, alerts_of)
                    done = upto
                    for k, p in st["probes"].items():
                        if edge in moves.get(k, []) and p.get("session") is not None:
                            E._relearn(p, edge)
                if press:
                    for k in st["probes"]:
                        E.reset_probe(st, k, press["t"])  # a press with no reading after it
            model.cursor = now
            model.hold_latches(held)

        held = list(self.ledger["latch"])
        began = time.monotonic()
        await self._run(replay_all)
        if self._stopped:
            return
        self._model = model
        _LOGGER.debug(
            "Estimator rebuilt from %d h of history (control revision %d) in %.1f s",
            REPLAY_S // 3600,
            rev,
            time.monotonic() - began,
        )
        self.ready = True
        self._dirty = True
        self._kick()

    @callback
    def _kick(self) -> None:
        if self._stopped or not self.ready or self._job is not None:
            return
        if not self._pending and not self._dirty:
            return
        # Not eager: the job must not run (and finish, and clear _job) before _job is set.
        self._job = self.hass.async_create_background_task(
            self._async_job(), f"{DOMAIN} evaluate", eager_start=False
        )

    async def _async_job(self) -> None:
        try:
            wait = MIN_JOB_GAP_S - (time.monotonic() - self._last_job)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_job = time.monotonic()
            obs, self._pending = sorted(self._pending, key=lambda o: o[2]), []
            self._dirty = False
            rev = self.control.view.rev
            settings = self._settings_now()
            model = self._model
            now = time.time()

            def step() -> Any:
                model.ingest(obs)
                return model.evaluate(now, settings)

            results, fired, rearmed = await self._run(step)
            if self._stopped:
                return
            await self._async_publish(rev, results, fired, rearmed)
        except Exception:
            _LOGGER.exception("Estimator step failed")
        finally:
            self._job = None
            if not self._stopped and (self._pending or self._dirty):
                self._kick()

    async def _async_publish(
        self,
        rev: int,
        results: dict[str, dict[str, Any]],
        fired: list[tuple[str, str, str]],
        rearmed: list[tuple[str, float]],
    ) -> None:
        """A step's results, computed with the settings of control revision `rev`. A setting
        changed since (while the worker ran) voids the alerts of this step that were decided
        with it (EVENT_SETTINGS), and only those: the next step, already due, sees the new
        value, and the estimator treats a settings change as it always does (a crossing it
        brings about is taken silently). An alert that does not depend on what changed goes
        out as computed."""
        now = time.time()
        changed_settings = self._settings_changed_since(rev)
        if changed_settings:
            self._dirty = True
        async with self._ledger_lock:
            changed = False
            for lk, t in rearmed:
                changed |= L.note_rearmed(self.ledger, lk, t)
            if changed:
                await self.ledger_file.async_save(self.ledger)
        for key, r in results.items():
            if key not in self.channels:
                continue
            attrs = {"key": key, "revision": rev, "build": self.build}
            attrs.update({k: v for k, v in r["attrs"].items() if v is not None})
            payload = {"status": r["status"], "attributes": attrs}
            if self.results.get(key) != payload:
                self.results[key] = payload
                async_dispatcher_send(self.hass, signal_channel(self.entry.entry_id, key))
                self.journal.observe(f"predictor:{key}", now, payload)
        for key, event, lk in fired:
            c = self.channels.get(key)
            # checked again for every alert: an earlier one's send may have taken a while,
            # and a setting may have changed meanwhile
            changed_settings = self._settings_changed_since(rev)
            if c is None or any(
                (key, f) in changed_settings for f in EVENT_SETTINGS.get(event, ())
            ):
                continue
            payload = {
                "key": key,
                "name": c.name,
                "event": event,
                "attrs": results[key]["attrs"],
                # what the step decided with, not what holds now: the send re-checks the two
                "settings": self._event_settings(key, event, rev),
                "latch": lk,
            }
            async with self._ledger_lock:
                if L.holds(self.ledger, lk):
                    continue
                occ = L.note_pending(self.ledger, lk, now, payload)
                await self.ledger_file.async_save(self.ledger)
            await self._async_deliver(occ, payload)
        await self._async_retry_outbox(now)

    def _targets(self) -> tuple[list[str], list[str], str | None]:
        opts = self.entry.options
        return (
            list(opts.get(CONF_NOTIFY) or []),
            list(opts.get(CONF_NOTIFY_ENTITIES) or []),
            opts.get(CONF_CLICK_PATH) or None,
        )

    async def _async_deliver(self, occ: str, payload: dict[str, Any]) -> None:
        """Send an occurrence to its targets; it is sent once every target took it, and stays
        pending for the ones that failed (retried while it is fresh and still current)."""
        if not self.live:
            return  # entities not up yet: the retry after the first step sends it
        # checked right before the send, after every await that led here: a setting changed
        # or an alert turned off while the outbox was being written voids it now
        if payload.get("settings") != self._event_settings(payload["key"], payload["event"]) or (
            payload["key"] not in self.channels
        ):
            async with self._ledger_lock:
                L.drop(self.ledger, occ)
                L.note_rearmed(self.ledger, payload["latch"], time.time())
                await self.ledger_file.async_save(self.ledger)
            return
        async with self._ledger_lock:
            pending = self.ledger["occ"].get(occ)
            if pending is not None and "payload" in pending:
                pending["payload"]["tried"] = time.time()
        services, entities, click = self._targets()
        failed = await alerts.async_send(
            self.hass,
            services,
            entities,
            click,
            payload["key"],
            payload["name"],
            payload["event"],
            payload["attrs"],
            payload.get("only"),
        )
        async with self._ledger_lock:
            if failed:
                L.note_failed(self.ledger, occ, failed)
            else:
                L.note_sent(self.ledger, occ)
            await self.ledger_file.async_save(self.ledger)

    def _still_current(self, occ: str, payload: dict[str, Any]) -> bool | None:
        """Whether a pending occurrence may go out (again): True, for an active channel whose
        probe is reading in the same session; None, not now (stale or unplugged: what the
        alert says cannot be confirmed, but it may be once the probe reads again, while the
        occurrence is fresh); False, never (the channel is gone or another session began)."""
        key = payload["key"]
        now = self.results.get(key)
        if key not in self.channels or not now:
            return False if key not in self.channels else None
        if now["attributes"].get("session_start") != payload["attrs"].get("session_start"):
            return False if now["status"] not in ("stale", "unplugged") else None
        return self._still_holds(occ, payload)

    async def _async_retry_outbox(self, now: float) -> None:
        if not self.live:
            return
        fresh, stale = L.pending(self.ledger, now)
        if stale:
            async with self._ledger_lock:
                for occ in stale:
                    L.drop(self.ledger, occ)
                await self.ledger_file.async_save(self.ledger)
        for occ, payload in fresh:
            if payload.get("tried", 0) > now - EVAL_EVERY_S:
                continue  # tried in this step or the one before: give it a moment
            current = self._still_current(occ, payload)
            if current is None:
                continue  # not confirmable now; it expires if that lasts past RESEND_S
            if not current:
                async with self._ledger_lock:
                    L.drop(self.ledger, occ)
                    await self.ledger_file.async_save(self.ledger)
                continue
            _LOGGER.info(
                "Sending again an alert that did not go out: %s %s",
                payload["name"],
                payload["event"],
            )
            await self._async_deliver(occ, payload)

    def result(self, key: str) -> dict[str, Any] | None:
        if not self.ready:
            return {"status": "stale", "attributes": {"key": key, "reason": "loading history"}}
        return self.results.get(key)

    # ---- the journal sensor and the card ----

    def journal_status(self) -> dict[str, Any]:
        return self.journal.status()

    def _own_entity(self, platform: Platform, unique_id: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(platform, DOMAIN, unique_id)

    def card_config(self) -> dict[str, Any]:
        eid = self.entry.entry_id
        reg = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        channels = []
        battery_devices: dict[str, bool] = {}
        for c in self.channels.values():
            source = reg.async_get(c.entity_id)
            device_id = source.device_id if source else None
            if device_id:
                battery_devices[device_id] = (
                    battery_devices.get(device_id, False) or c.decoder == DECODER_INKBIRD
                )
            item: dict[str, Any] = {
                "key": c.key,
                "name": c.name,
                "role": c.role,
                "ambient": c.ambient,
                "entity_id": c.entity_id,
                "device_id": device_id,
                "since": c.since,
                "active": True,
            }
            if c.role == ROLE_INTERNAL:
                item.update(
                    status=self._own_entity(Platform.SENSOR, f"{eid}_{c.key}_status"),
                    done_at=self._own_entity(Platform.SENSOR, f"{eid}_{c.key}_done_at"),
                    alert_on=self._own_entity(Platform.SWITCH, f"{eid}_{c.key}_alert_on"),
                    numbers={
                        f: self._own_entity(Platform.NUMBER, f"{eid}_{c.key}_{f}")
                        for f in ("target", "warn_min", "alert_temp", "alert_min")
                    },
                )
            channels.append(item)
        # channels removed lately keep their line on the chart of the cook they were in
        for key in self._recent_keys(time.time() - SERIES_S):
            if key in self.channels:
                continue
            ch = self.control.view.channels[key]
            channels.append(
                {
                    "key": key,
                    "name": ch["name"],
                    "role": ch["role"],
                    "ambient": None,
                    "entity_id": ch["entity_id"],
                    "since": ch["since"],
                    "until": ch["until"],
                    "active": False,
                }
            )
        batteries = []
        for device_id, inkbird in battery_devices.items():
            device = devices.async_get(device_id)
            for e in er.async_entries_for_device(reg, device_id):
                dc = e.device_class or e.original_device_class
                if (
                    dc == "battery"
                    and e.domain in ("sensor", "binary_sensor")
                    and not e.disabled_by
                ):
                    batteries.append(
                        {
                            "device_id": device_id,
                            "device": (device.name_by_user or device.name) if device else None,
                            "entity_id": e.entity_id,
                            "kind": "percent" if e.domain == "sensor" else "binary",
                            "decoder": DECODER_INKBIRD if inkbird else "generic",
                        }
                    )
                    break
        return json.loads(
            json.dumps(
                {
                    "entry_id": eid,
                    "title": self.entry.title,
                    "generation": self.generation,
                    "revision": self.control.view.rev,
                    "channels": channels,
                    "batteries": batteries,
                    "buttons": {
                        "new_cook": self._own_entity(Platform.BUTTON, f"{eid}_new_cook"),
                        "finish": self._own_entity(Platform.BUTTON, f"{eid}_finish"),
                    },
                    "journal": self._own_entity(Platform.SENSOR, f"{eid}_journal"),
                }
            )
        )
