"""The cook journal's I/O: its files, recovery from the recorder, and the list, get, edit and
delete calls the journal card uses.

It began as a port of the Node-RED function node around journal.js (cook/journal-node.js),
as an asyncio class whose filesystem, recorder and clock are injected, so it can be tested
without Home Assistant. The runtime feeds it normalised rows under pseudo entity ids (the
ids given to the constructor), and its recorder adapter answers history queries for those
ids, membership rows and presses included, from the recorder and the runtime's own logs.

Files under `directory` (in Home Assistant inside /config, so in its backups):
  state.json        {schema, build, snap: {js, at}, pending, edits, tombstones}: the journal
                    state as derived from complete data up to `at`, finished cooks whose own
                    file is not written yet, name/notes given to cooks without a file, and
                    deleted cooks whose tombstone is not written yet. Temp file + rename. A
                    schema-1 state.json (the Node-RED journal's) is migrated when read: its
                    snapshot becomes schema 2, its pending cooks, edits and tombstones stay as
                    they are, and the next save writes it as schema 2.
  cooks/<id>.json   finished cooks. trash/<id>.json: deleted ones; nothing writes them again.
                    Schema-1 cooks stay schema 1; they are read as they are.

Recovery is one mechanism, a reconcile: start from the snapshot, take the recorder's rows
from `at` (minus a minute) to now plus every live event seen since, in one time order, and
derive the state again. It runs at startup, two minutes later (the recorder may not have
committed the newest rows when first asked), and every 30 min (it repairs anything missed
while the event stream was down). The snapshot moves to two minutes before each successful
query, so a row the recorder commits late is still inside the next window.

How the Node-RED plumbing maps here: the startup inject is start(), the 60 s tick is tick(),
a state change is observe_live(), a New cook or Finish press is press(), On Stop is stop(),
and the HTTP endpoints are list(), get(), edit() and delete(). A history query is a call to
the recorder, awaited in a task of its own; the answers are merged as they arrive and the
reconcile applies once all are in, or gives up on a tick 180 s later. A redeploy is a new
JournalManager given the old one as `previous`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final, Protocol

from .journal import (
    SCHEMA,
    UNDEFINED,
    Cook,
    Event,
    Journal,
    advance,
    clone,
    cook_id,
    derive,
    from_json,
    is_finite_number,
    iso,
    js_keys,
    js_round,
    kinds,
    migrate_journal,
    new_journal,
    observe,
    summarize,
    to_json,
)

_LOGGER = logging.getLogger(__name__)

# The channel groups of the ids, keyed by channel.
_KEYED: Final = ("probes", "pits", "predictors", "targets")
PRESSES: Final = {"new_cook": "reset", "finish": "finish"}
# The order of rows at one time in a reconcile (_row_order).
_ROW_RANK: Final = {
    kind: rank
    for rank, kind in enumerate(("members", "press", "probe", "pit", "target", "predictor"))
}

RECONCILE_S: Final = 1800
FIRST_RECONCILE_S: Final = 120
RETRY_S: Final = 300
SETTLE_S: Final = 120  # rows newer than this may not be committed to the recorder yet
OVERLAP_S: Final = 60
CHUNK_S: Final = 6 * 3600
# Each window but the last asks a second past its end: the recorder leaves out a row at a
# window's end, and the next window only repeats it as its starting row, which is dropped.
CHUNK_OVERLAP_S: Final = 1
QUERY_TIMEOUT_S: Final = 180
FIRST_LOOKBACK_S: Final = 48 * 3600
MAX_BACK_S: Final = 10 * 86400  # the recorder keeps about this much
MAX_LOG: Final = 50000
SLOW_HANDOVER_S: Final = 15
NAME_UNITS: Final = 120
NOTES_UNITS: Final = 5000
_ID_RE: Final = re.compile(r"[0-9]{8}T[0-9]{6}Z")


class JournalFilesystem(Protocol):
    """The file operations the journal needs. A missing file raises FileNotFoundError."""

    async def read_text(self, path: str) -> str:
        """The file's text (UTF-8)."""

    async def write_text(self, path: str, data: str) -> None:
        """Create or replace the file with `data` (UTF-8)."""

    async def rename(self, source: str, target: str) -> None:
        """Move `source` over `target`, atomically (FileNotFoundError if `source` is missing)."""

    async def unlink(self, path: str) -> None:
        """Remove the file."""

    async def listdir(self, path: str) -> list[str]:
        """The names of the entries directly inside the directory."""

    async def mkdir(self, path: str) -> None:
        """Create the directory and its parents; one that exists already is fine."""


class JournalRecorder(Protocol):
    """Home Assistant's recorder history, as its history/history_during_period websocket
    command answers with significant_changes_only off.
    """

    async def history(
        self, start: float, end: float, entity_ids: Sequence[str], minimal: bool
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        """{entity_id: [{"s": state, "a": attributes, "lu": last_updated, "lc": last_changed}]}
        over [start, end] (epoch seconds, to the millisecond), each entity's rows starting
        with the state in force at `start`, stamped `start`. minimal=True asks for
        minimal_response and no_attributes (rows without "a"); minimal=False for neither.
        """


class JournalError(Exception):
    """A call the journal cannot answer. `status` and `payload` are the HTTP reply the
    Node-RED journal gave for the same request.
    """

    status = 500
    message = "journal error"

    def __init__(self) -> None:
        super().__init__(self.message)
        self.payload = {"error": self.message}


class BadCookIdError(JournalError):
    """The id is not a cook id (20261011T011920Z)."""

    status = 400
    message = "bad id"


class CookNotFoundError(JournalError):
    """No such cook, or a deleted one."""

    status = 404
    message = "no such cook"


class CookOpenError(JournalError):
    """The open cook cannot be deleted."""

    status = 409
    message = "finish the cook first"


class DeleteFailedError(JournalError):
    """The delete could not write its trash copy or forget the cook; the cook is kept."""

    status = 500
    message = "delete failed"


class PressFailedError(JournalError):
    """A press could not be saved: state.json was not written with its effect, so the
    caller reports the press as not gone through.
    """

    status = 500
    message = "press not saved"


class SupersededError(Exception):
    """A stopped generation tried to commit a write."""


def _unique(items: list[Any]) -> list[Any]:
    """[...new Set(items)] for objects: the first of each, by identity."""
    seen: set[int] = set()
    out = []
    for item in items:
        if id(item) not in seen:
            seen.add(id(item))
            out.append(item)
    return out


def _utf16_prefix(text: str, units: int) -> str:
    """text.slice(0, units): JavaScript counts UTF-16 code units, so a cut can fall inside an
    emoji and leave half of it, which to_json() then writes as an escape, as JSON.stringify
    does.
    """
    if len(text) * 2 <= units:
        return text
    encoded = text.encode("utf-16-le", "surrogatepass")
    return encoded[: 2 * units].decode("utf-16-le", "surrogatepass")


def _to_millisecond(t: float) -> float:
    """A query bound as the Node-RED journal sent it: an ISO time, to the millisecond."""
    return math.trunc(t * 1000) / 1000


def _meta(c: Mapping[str, Any]) -> dict[str, Any]:
    """What a tombstone remembers: the deleted cook's span and what bounded it (press_at: the
    press that closed it, which can be later than its end).
    """
    return {
        "start": c.get("start"),
        "end": c.get("end"),
        "started_by": c.get("started_by"),
        "ended_by": c.get("ended_by"),
        "press_at": c.get("press_at"),
    }


class JournalManager:
    """The journal of one generation of code.

    Every file operation goes through one ordered chain; a failed one is logged and the chain
    carries on. A new generation (`previous` given) takes over only once every file
    operation already queued by the previous one has finished, however long that takes, and
    the previous one, stopped, issues no rename after that, so two generations never write
    over each other. Construct it inside the running event loop.

    The API calls return the JSON payloads the Node-RED endpoints sent (plain dicts and
    lists, safe to keep or change) and raise a JournalError subclass where those answered
    with an error status: BadCookIdError (400), CookNotFoundError (404), CookOpenError (409),
    DeleteFailedError (500). As there, an edit whose write fails is answered as not found.
    The ingress check of the HTTP endpoints has no equivalent: access is the caller's.

    ids are the pseudo entity ids the runtime feeds: {probes: {key: id}, pits: {key: id},
    predictors: {key: id}, targets: {key: id}, reset: id, finish: id, members: id}.
    """

    def __init__(
        self,
        fs: JournalFilesystem,
        recorder: JournalRecorder,
        clock: Callable[[], float] = time.time,
        *,
        directory: str,
        ids: Mapping[str, Any],
        build: str = "dev",
        previous: JournalManager | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._fs = fs
        self._recorder = recorder
        self._clock = clock
        self._dir = directory
        self._state_path = f"{directory}/state.json"
        # channel keys as strings, as JavaScript object keys are
        self._ids = {
            **ids,
            **{
                group: {str(key): entity_id for key, entity_id in (ids.get(group) or {}).items()}
                for group in _KEYED
            },
        }
        self._kind = kinds(self._ids)
        self._position = {entity_id: i for i, entity_id in enumerate(self._kind)}
        self._build = build
        self._log = logger or _LOGGER
        # snap, js (current state), rows (live rows [t, id, state, attrs] not yet in a
        # snapshot), pending, edits, tombs, index (id -> summary of written cooks), trash
        # (id -> span of a deleted cook, None if unreadable), query, the reconcile timer.
        self._stopped = False
        self._ready = False
        self._starting = False
        self._snap: dict[str, Any] = {}
        self._js: Journal | None = None
        self._rows: list[list[Any]] = []
        self._pending: list[Cook] = []
        self._edits: dict[str, dict[str, Any]] = {}
        self._tombs: list[Cook] = []
        self._index: dict[str, dict[str, Any]] = {}
        # cook id -> the name, notes and edited_at an edited cook file holds
        self._saved: dict[str, dict[str, Any]] = {}
        self._trash: dict[str, dict[str, Any] | None] = {}
        self._query: dict[str, Any] | None = None
        self._next_reconcile_at = math.inf
        self._chain: asyncio.Task[None] | None = None
        if previous is not None:
            previous.stop()
            self._chain = asyncio.get_running_loop().create_task(self._wait_for(previous._chain))

    @property
    def ready(self) -> bool:
        """Whether the startup reconcile has applied."""
        return self._ready

    @property
    def journal(self) -> Journal | None:
        """The current journal state (read only)."""
        return self._js

    async def drain(self) -> None:
        """Wait for every file operation queued so far."""
        if self._chain is not None:
            await asyncio.wait([self._chain])

    def request_reconcile(self) -> None:
        """Reconcile at the next tick instead of at the next scheduled time (something the
        live path missed is in the sources the reconcile reads)."""
        if self._ready:
            self._next_reconcile_at = min(self._next_reconcile_at, self._clock())

    def stop(self) -> None:
        """Stop this generation (Node-RED's On Stop, an unload in Home Assistant): it issues
        no rename from now on and takes no more input. Operations already queued run on; a
        generation constructed with this one as `previous` waits for them.
        """
        self._stopped = True
        if self._query is not None:
            self._cancel_requests(self._query)

    # ---- the chain of file operations ----

    async def _wait_for(self, chain: asyncio.Task[None] | None) -> None:
        if chain is None:
            return
        slow = asyncio.get_running_loop().call_later(
            SLOW_HANDOVER_S,
            self._log.warning,
            "journal: still waiting for the previous code to finish writing",
        )
        try:
            await asyncio.wait([chain])
        finally:
            slow.cancel()

    def _queue(self, op: Callable[[], Awaitable[None]], label: str) -> asyncio.Task[None]:
        """Serialise every file operation; a failed one is logged and the chain carries on."""
        previous = self._chain

        async def run() -> None:
            if previous is not None:
                await asyncio.wait([previous])
            try:
                await op()
            except SupersededError:
                # a replaced generation (a reload) declining to write: the new one has the state
                self._log.debug("journal: %s skipped, superseded by newer code", label)
            except Exception as err:  # logged, and the next operation runs
                self._log.error("journal: %s failed: %s", label, err)

        self._chain = asyncio.get_running_loop().create_task(run())
        return self._chain

    async def _rename_owned(self, source: str, target: str) -> None:
        """Every rename goes through here: a stopped generation issues none."""
        if self._stopped:
            raise SupersededError("superseded by newer code")
        await self._fs.rename(source, target)

    async def _write_atomic(self, path: str, data: Any) -> None:
        tmp = f"{path}.tmp-{secrets.token_hex(6)}"
        await self._fs.write_text(tmp, to_json(data))
        try:
            await self._rename_owned(tmp, path)
        except Exception:
            with contextlib.suppress(Exception):
                await self._fs.unlink(tmp)
            raise

    async def _read_json(self, path: str) -> Any:
        return from_json(await self._fs.read_text(path))

    def _with_edits(self, c: Cook, saved: Any = UNDEFINED) -> Cook:
        """The cook with the newest name and notes it was given, one rule for the list, the
        detail, an edit and a write: an edit not yet in a file (`edits`), or what the cook's
        written file holds (`saved`: from the file itself when writing it, else as last
        written), which a re-derived copy of a written cook comes without. The file wins a
        tie, so a saved name or notes never give way to a copy.
        """
        edits = self._edits.get(c["id"])
        out = {**c, **edits, "edited": True} if edits is not None else c
        if saved is UNDEFINED:
            saved = self._saved.get(c["id"])
        if saved is not None:
            theirs = saved.get("edited_at")
            ours = out.get("edited_at")
            if (0 if theirs is None else theirs) >= (0 if ours is None else ours):
                out = {
                    **out,
                    "name": saved.get("name", UNDEFINED),
                    "notes": saved.get("notes", UNDEFINED),
                    "edited": True,
                    "edited_at": saved.get("edited_at", UNDEFINED),
                }
        return out

    def _remember(self, c: Mapping[str, Any]) -> None:
        """Keep what a written cook file holds of its name and notes, if it was edited."""
        if c.get("edited"):
            self._saved[c["id"]] = {
                "name": c.get("name", UNDEFINED),
                "notes": c.get("notes", UNDEFINED),
                "edited_at": c.get("edited_at", UNDEFINED),
            }
        else:
            self._saved.pop(c["id"], None)

    async def _flush(self, saved: asyncio.Future[None] | None = None) -> None:
        """state.json first (it still holds the finished cooks and the tombstones to write, in
        the same write as the snapshot that depends on them), then each tombstone and cook
        file, then state.json again without the ones now written. A failure leaves them in
        state.json, and the next save or a restart retries them. `saved`, if given, is
        resolved once that first write of state.json is done: from then on what led to this
        flush is durable.
        """

        async def save() -> None:
            await self._write_atomic(
                self._state_path,
                {
                    "schema": SCHEMA,
                    "build": self._build,
                    "snap": self._snap,
                    "pending": self._pending,
                    "edits": self._edits,
                    "tombstones": self._tombs,
                },
            )

        await save()
        if saved is not None and not saved.done():
            saved.set_result(None)
        wrote = False
        for c in list(self._tombs):
            await self._write_atomic(f"{self._dir}/trash/{c['id']}.json", c)
            self._tombs = [x for x in self._tombs if x is not c]
            wrote = True
        for c in list(self._pending):
            if c["id"] not in self._trash:
                await self._write_cook(c)
            self._pending = [x for x in self._pending if x is not c]
            wrote = True
        if wrote:
            await save()

    async def _write_cook(self, c: Cook) -> None:
        """The newest name/notes win: an edit made in the card before the file existed, or one
        already in the file from an earlier write of the same cook.
        """
        path = f"{self._dir}/cooks/{c['id']}.json"
        try:
            old = await self._read_json(path)
        except FileNotFoundError:
            old = None
        saved = old if old is not None and old.get("edited") else None
        out = self._with_edits(c, saved)
        await self._write_atomic(path, out)
        self._index[c["id"]] = summarize(out)
        self._remember(out)
        self._edits.pop(c["id"], None)

    # ---- what a reconcile must respect ----

    def _known_cooks(self, window_start: float, now: float) -> list[dict[str, Any]]:
        """Known cooks a reconcile must respect: written, pending and deleted ones reaching
        into its window, by their end or by the press that closed them, which can be later
        than the end. [{start, end, started_by, ended_by, press_at, id, deleted}]
        """
        out: dict[Any, dict[str, Any]] = {}

        def add(k: Mapping[str, Any], deleted: bool) -> None:
            end = k.get("end")
            press_at = k.get("press_at")
            reach = max(now if end is None else end, -math.inf if press_at is None else press_at)
            if is_finite_number(k.get("start")) and reach > window_start:
                out[k.get("id")] = {**_meta(k), "id": k.get("id"), "deleted": deleted}

        for key in js_keys(self._index):
            add(self._index[key], deleted=False)
        for c in self._pending:
            add(c, deleted=False)
        for key, span in self._trash.items():
            if span:
                add({"id": key, **span}, deleted=True)
        return list(out.values())

    def _boundary_rows(self, known: list[dict[str, Any]]) -> list[list[Any]]:
        """The New cook and Finish presses that bounded those cooks, at the times they were
        pressed, as recorder-like rows. A press once observed stays a boundary even if the
        recorder lost its row (press times dedupe it otherwise), so a reconcile can split a
        cook at a press it finds but never merge two. An end inferred from the probes is not
        a press: not replayed.
        """
        reset, finish = self._ids["reset"], self._ids["finish"]
        rows: list[list[Any]] = []
        for k in known:
            if k["started_by"] == "new_cook":
                rows.append([k["start"], reset, iso(k["start"]), None])
            if k["press_at"] is not None and k["ended_by"] == "new_cook":
                rows.append([k["press_at"], reset, iso(k["press_at"]), None])
            if k["press_at"] is not None and k["ended_by"] == "finish":
                rows.append([k["press_at"], finish, iso(k["press_at"]), None])
        return rows

    def _keep_identities(
        self, fresh: list[Cook], known: list[dict[str, Any]], now: float
    ) -> tuple[dict[str, str], list[Cook]]:
        """Recovery may find a boundary the live stream missed, so a cook can come out with
        another start (its id is its start) or split in two; with known boundaries kept,
        never merged. Identity is kept: the re-derived cooks overlapping a known cook most
        are its descendants; the heir takes its id (and with it the name and notes), the
        others are new cooks. Every descendant of a deleted cook is deleted, each with its
        own tombstone. Returns (renamed: derived id -> kept id, cooks to tombstone).
        """

        def span(c: Mapping[str, Any]) -> tuple[float, float]:
            end = c.get("end")
            return (c["start"], now if end is None else end)

        def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
            return min(a[1], b[1]) - max(a[0], b[0])

        live = self._js["open"] if self._js else None
        every = known
        if live and not live["provisional"] and not any(k["id"] == live["id"] for k in known):
            every = [
                *known,
                {**_meta(live), "id": live["id"], "deleted": live["id"] in self._trash},
            ]
        groups: dict[int, tuple[dict[str, Any], list[tuple[Cook, float]]]] = {}
        for c in fresh:
            pick: tuple[dict[str, Any], float] | None = None
            for k in every:
                o = overlap(span(c), span(k))
                if o > 0 and (pick is None or o > pick[1]):
                    pick = (k, o)
            if pick is None:
                continue
            groups.setdefault(id(pick[0]), (pick[0], []))[1].append((c, pick[1]))
        # The heir is the part that starts where the known cook did, or else the largest part.
        renamed: dict[str, str] = {}
        deleted: list[Cook] = []
        heirs: list[Cook] = []
        for k, members in groups.values():
            members.sort(key=lambda member: member[1], reverse=True)
            heir = next((c for c, _ in members if c["id"] == k["id"]), members[0][0])
            heirs.append(heir)
            if heir["id"] != k["id"]:
                renamed[heir["id"]] = k["id"]
                heir["id"] = k["id"]
            for c, _ in members:
                if c is not heir and k["deleted"]:
                    deleted.append(c)
        # Any other part whose id (its start) is already someone's takes the next free second.
        taken = {k["id"] for k in every} | {c["id"] for c in heirs}
        for c in fresh:
            if any(c is heir for heir in heirs):
                continue
            if c["id"] in taken:
                second = js_round(c["start"])
                new_id = c["id"]
                while new_id in taken:
                    second += 1
                    new_id = cook_id(second)
                renamed[c["id"]] = new_id
                c["id"] = new_id
            taken.add(c["id"])
        return renamed, deleted

    # ---- live input ----

    def _note_events(self, events: list[Event]) -> bool:
        """Finished cooks go to pending; True if anything happened that must be saved."""
        boundary = False
        for ev in events:
            if ev["type"] == "close":
                self._pending = [c for c in self._pending if c["id"] != ev["cook"]["id"]]
                self._pending.append(ev["cook"])
                boundary = True
            if ev["type"] in ("open", "drop"):
                boundary = True
        return boundary

    def _handle_events(self, events: list[Event]) -> None:
        if self._note_events(events):
            self._queue(self._flush, "save")

    def _take(
        self, entity_id: str, t: float, state: Any, attrs: Mapping[str, Any] | None
    ) -> list[Event] | None:
        """Log a live row for the next reconcile and observe it if the journal is ready: the
        events it gave, or None if it is not for this journal or was only logged.
        """
        if self._stopped or entity_id not in self._kind or not is_finite_number(t):
            return None
        self._rows.append([t, entity_id, state, attrs])
        if len(self._rows) > MAX_LOG:
            del self._rows[: len(self._rows) - MAX_LOG]
        if not self._ready or self._js is None:
            return None
        events = observe(self._js, self._kind, entity_id, t, state, attrs)
        self._claim_ids(events)
        return events

    def _claim_ids(self, events: list[Event]) -> None:
        """A cook opened live takes an id no other cook has. An id is the start to the second,
        so two New cook presses within one second would give both cooks the same one and the
        second's file would overwrite the first's; the reconcile's repair (_keep_identities)
        comes too late for that. A taken id moves on to the next free second, as there.
        """
        taken = {
            *self._index,
            *(c["id"] for c in self._pending),
            *self._trash,
            *(c["id"] for c in self._tombs),
        }
        for ev in events:
            c = ev["cook"]
            if ev["type"] == "open" and c["id"] in taken:
                second = js_round(c["start"])
                new_id = c["id"]
                while new_id in taken:
                    second += 1
                    new_id = cook_id(second)
                c["id"] = new_id
            if ev["type"] in ("open", "close"):
                taken.add(c["id"])

    def observe_live(
        self, entity_id: str, t: float, state: Any, attrs: Mapping[str, Any] | None = None
    ) -> None:
        """A row of one of the ids, as the runtime normalised it (a channel's state, a
        predictor's or a target's, a membership row): `t` is its time in epoch seconds.
        Logged for the next reconcile, and observed at once once the journal is ready.
        """
        events = self._take(entity_id, t, state, attrs)
        if events:
            self._handle_events(events)

    async def press(self, what: str, t: float) -> None:
        """A New cook ("new_cook") or Finish ("finish") press from the command path, at `t`:
        observed as a press row (its state the press time), and returns once the save it
        triggers has written state.json, which makes it durable; the cook files are written
        after that, on the queue. Raises PressFailedError if that write failed (or this
        generation is stopped), so the caller can report the press as not gone through; a
        cook file that cannot be written stays pending in state.json and does not fail the
        press. Before the startup reconcile has applied, the press waits in the live log and
        that reconcile applies it.
        """
        if what not in PRESSES:
            raise ValueError(f"not a press: {what!r}")
        if self._stopped:
            raise PressFailedError
        entity_id = self._ids[PRESSES[what]]
        events = self._take(entity_id, t, iso(t), None)
        if not events or not self._note_events(events):
            return
        saved: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        flush = self._queue(lambda: self._flush(saved), "save after a press")
        await asyncio.wait([saved, flush], return_when=asyncio.FIRST_COMPLETED)
        if not saved.done():
            raise PressFailedError

    async def tick(self, now: float | None = None) -> None:
        """The 60 s tick: gives up on an unanswered query, closes, drops or flags the open
        cook as time passes, and starts a reconcile when one is due. `now` defaults to the
        clock.
        """
        if self._stopped:
            return
        now = self._clock() if now is None else now
        query = self._query
        if query is not None and now - query["started_at"] > QUERY_TIMEOUT_S:
            self._log.warning(
                "journal: %s history answers missing after %s s", query["waiting"], QUERY_TIMEOUT_S
            )
            if query["startup"]:
                self._apply_query(partial=True)  # go on with what arrived; the snapshot stays put
            else:
                self._query = None
                self._cancel_requests(query)
                self._next_reconcile_at = now + RETRY_S
        # A generation start() was never called for: start here.
        if not self._ready or self._js is None:
            await self.start()
            return
        events: list[Event] = []
        advance(self._js, now, events)
        self._handle_events(events)
        if self._query is None and now >= self._next_reconcile_at:
            self._start_query(startup=False)

    # ---- startup and reconcile ----

    async def start(self) -> None:
        """Read the files and start the startup reconcile. Claimed before queueing, so a
        second call cannot start twice; released if it fails.
        """
        if self._ready or self._starting or self._stopped:
            return
        self._starting = True

        async def run() -> None:
            try:
                await self._startup()
            except BaseException:
                self._starting = False
                raise

        await self._queue(run, "startup")

    async def _startup(self) -> None:
        await self._fs.mkdir(f"{self._dir}/cooks")
        await self._fs.mkdir(f"{self._dir}/trash")
        saved = None
        try:
            saved = await self._read_json(self._state_path)
        except FileNotFoundError:
            pass
        except Exception as err:  # anything else: start over from the recorder
            self._log.warning(
                "journal: state.json unreadable (%s), rebuilding from the recorder", err
            )
        now = self._clock()
        state = saved if isinstance(saved, dict) else {}
        schema = state.get("schema")
        if is_finite_number(schema) and schema in (1, SCHEMA) and state.get("snap"):
            self._snap = state["snap"]
            if schema != SCHEMA:
                # the Node-RED journal's: its snapshot carries on as schema 2; its pending
                # cooks, edits and tombstones stay as they are (schema-1 cooks are readable)
                self._snap = {**self._snap, "js": migrate_journal(self._snap["js"])}
                self._log.info("journal: state.json of schema %s migrated", schema)
            self._pending = state.get("pending") or []
            self._edits = state.get("edits") or {}
            self._tombs = state.get("tombstones") or []
        else:
            self._snap = {"js": new_journal(now - FIRST_LOOKBACK_S), "at": now - FIRST_LOOKBACK_S}
        self._js = clone(self._snap["js"])
        self._trash = {}
        for name in await self._fs.listdir(f"{self._dir}/trash"):
            if not name.endswith(".json"):
                continue
            span = None
            try:
                c = await self._read_json(f"{self._dir}/trash/{name}")
                if is_finite_number(c.get("start")):
                    span = _meta(c)
            except Exception as err:  # a bad tombstone still marks a deletion
                self._log.warning("journal: unreadable tombstone %s: %s", name, err)
            self._trash[name[:-5]] = span
        for c in self._tombs:
            self._trash[c["id"]] = _meta(c)  # deletions not yet written: still deletions
        self._index = {}
        self._saved = {}
        for name in await self._fs.listdir(f"{self._dir}/cooks"):
            if not name.endswith(".json"):
                continue
            try:
                c = await self._read_json(f"{self._dir}/cooks/{name}")
                self._index[c["id"]] = summarize(c)
                self._remember(c)
            except Exception as err:  # one bad file does not hide the others
                self._log.warning("journal: skipped %s: %s", name, err)
        self._start_query(startup=True)

    def _start_query(self, startup: bool) -> None:
        now = self._clock()
        start = max(self._snap["at"] - OVERLAP_S, now - MAX_BACK_S)
        chunks = []
        a = start
        while a < now:
            # overlapping: a row inside the overlap comes twice, and observe() takes it once
            chunks.append((a, min(now, a + CHUNK_S + CHUNK_OVERLAP_S)))
            a += CHUNK_S
        query: dict[str, Any] = {
            "from": start,
            "to": now,
            "rows": [],
            "waiting": len(chunks) * 2,
            "started_at": now,
            "startup": startup,
            "tasks": set(),
        }
        self._query = query
        if not chunks:
            self._apply_query()
            return
        # states without attributes; then the rows whose attributes matter: the predictors'
        # and the membership rows
        simple = [
            *(self._ids["probes"][n] for n in js_keys(self._ids["probes"])),
            *(self._ids["pits"][n] for n in js_keys(self._ids["pits"])),
            *(self._ids["targets"][n] for n in js_keys(self._ids["targets"])),
            self._ids["reset"],
            self._ids["finish"],
        ]
        detailed = [self._ids["predictors"][n] for n in js_keys(self._ids["predictors"])]
        if self._ids.get("members") is not None:
            detailed.append(self._ids["members"])
        loop = asyncio.get_running_loop()
        for a, b in chunks:
            chunk = {"start": a, "first": a == start}
            for entity_ids, minimal in ((simple, True), (detailed, False)):
                query["tasks"].add(
                    loop.create_task(self._ask(query, chunk, a, b, entity_ids, minimal))
                )

    async def _ask(
        self,
        query: dict[str, Any],
        chunk: dict[str, Any],
        start: float,
        end: float,
        entity_ids: list[str],
        minimal: bool,
    ) -> None:
        try:
            result = await self._recorder.history(
                _to_millisecond(start), _to_millisecond(end), entity_ids, minimal
            )
        except Exception as err:  # unanswered: the tick gives up on it
            self._log.warning("journal: history query failed: %s", err)
            return
        self._query_result(query, result, chunk)

    def _cancel_requests(self, query: dict[str, Any]) -> None:
        current = asyncio.current_task()
        for task in query["tasks"]:
            if task is not current and not task.done():
                task.cancel()

    def _query_result(
        self, query: dict[str, Any], result: Mapping[str, Any] | None, chunk: dict[str, Any]
    ) -> None:
        """A query window starts with the state in force at its start. In a later chunk that
        only repeats the previous chunk's end; in the first, it is new only for an entity the
        snapshot has never seen.
        """
        if self._query is not query:
            return  # an answer to a query given up on
        answer = result or {}
        for entity_id in js_keys(answer):
            for k, row in enumerate(answer[entity_id]):
                t = row.get("lu")
                if t is None:
                    t = row.get("lc")
                if not is_finite_number(t):
                    continue  # never applied: the reconcile takes only rows with a time
                baseline = k == 0 and t <= chunk["start"] + 0.001
                if baseline and (not chunk["first"] or self._snap["js"]["last"].get(entity_id)):
                    continue
                query["rows"].append([t, entity_id, row.get("s"), row.get("a")])
        query["waiting"] -= 1
        if query["waiting"] <= 0:
            self._apply_query()

    def _row_order(self, row: list[Any]) -> tuple[float, int, int]:
        """Rows in time order and, at one time, in one fixed order that does not depend on
        which history answer came first: membership rows, then presses, then the channels'
        readings, targets and predictors, each by its place in the ids. At one instant the
        configuration a reading belongs to, and a press it follows, come first (the runtime
        delivers them live in that order too).
        """
        kind = self._kind.get(row[1])
        rank = _ROW_RANK.get(kind[0], len(_ROW_RANK)) if kind else len(_ROW_RANK)
        return (row[0], rank, self._position.get(row[1], len(self._position)))

    def _apply_query(self, partial: bool = False) -> None:
        query = self._query
        if query is None:
            return
        self._query = None
        self._cancel_requests(query)
        known = self._known_cooks(self._snap["at"] - OVERLAP_S, self._clock())
        rows = [
            r
            for r in [*query["rows"], *self._rows, *self._boundary_rows(known)]
            if is_finite_number(r[0])
        ]
        rows.sort(key=self._row_order)
        # The new snapshot: complete data up to SETTLE_S before the query. Not moved by a
        # query that did not complete, so the next one covers the same stretch again.
        snap_at = self._snap["at"] if partial else max(self._snap["at"], query["to"] - SETTLE_S)
        js = clone(self._snap["js"])
        before = self._js["open"] if self._js else None
        events: list[Event] = []
        snap = None
        for t, entity_id, state, attrs in rows:
            if snap is None and t > snap_at:
                advance(js, snap_at, events)
                snap = clone(js)
            events.extend(observe(js, self._kind, entity_id, t, state, attrs))
        if snap is None:
            advance(js, snap_at, events)
            snap = clone(js)
        advance(js, self._clock(), events)
        if query["startup"]:
            # the stretch the journal itself was not running: filled from the recorder
            touched = _unique([c for c in [before, js["open"], *(e["cook"] for e in events)] if c])
            for c in touched:
                if c["provisional"]:
                    continue
                a = max(self._snap["at"], c["start"])
                b = min(query["to"], query["to"] if c.get("end") is None else c["end"])
                if b > a:
                    fill = [js_round(a - c["start"]), js_round(b - c["start"])]
                    c["fills"].append([*fill, "partial"] if partial else fill)
        # the ids the live stream or an earlier reconcile already gave these cooks
        opened = [e["cook"] for e in events if e["type"] in ("close", "open")]
        fresh = _unique([c for c in [*opened, js["open"]] if c and not c["provisional"]])
        renamed, deleted = self._keep_identities(fresh, known, self._clock())
        if snap["open"] and renamed.get(snap["open"]["id"]):
            snap["open"]["id"] = renamed[snap["open"]["id"]]
        for c in deleted:  # written by the flush below, durable in state.json until then
            self._trash[c["id"]] = _meta(c)
            self._tombs = [x for x in self._tombs if x["id"] != c["id"]]
            self._tombs.append(c)
        self._js = js
        self._snap = {"js": snap, "at": snap_at}
        self._rows = [r for r in self._rows if r[0] > snap_at - OVERLAP_S]
        self._ready = True
        self._starting = False
        if partial:
            delay = RETRY_S
        elif query["startup"]:
            delay = FIRST_RECONCILE_S
        else:
            delay = RECONCILE_S
        self._next_reconcile_at = self._clock() + delay
        self._handle_events(events)
        self._queue(self._flush, "save after reconcile")
        if query["startup"] or any(e["type"] != "update" for e in events):
            open_cook = js["open"]
            self._log.info(
                "journal: %s over %s rows from %s%s%s",
                "started" if query["startup"] else "reconciled",
                len(rows),
                iso(query["from"]),
                " (partial)" if partial else "",
                f", cook {open_cook['id']} open"
                if open_cook and not open_cook["provisional"]
                else "",
            )

    # ---- the API (the Node-RED HTTP endpoints) ----

    def _open_cook(self) -> Cook | None:
        c = self._js["open"] if self._js else None
        return c if c and not c["provisional"] and c["id"] not in self._trash else None

    async def _find_cook(self, wanted: str) -> tuple[Cook, str] | None:
        if wanted in self._trash:
            return None
        open_cook = self._open_cook()
        if open_cook and open_cook["id"] == wanted:
            return (self._with_edits(open_cook), "open")
        pending = next((c for c in self._pending if c["id"] == wanted), None)
        if pending is not None:
            return (self._with_edits(pending), "pending")
        try:
            cook = await self._read_json(f"{self._dir}/cooks/{wanted}.json")
        except FileNotFoundError:
            return None
        return (cook, "file")

    @staticmethod
    def _check_id(wanted: Any) -> None:
        if not isinstance(wanted, str) or not _ID_RE.fullmatch(wanted):
            raise BadCookIdError

    async def get(self, cook_id: str) -> dict[str, Any]:
        """{cook, derived}: the record and what is computed from it (GET /:id)."""
        self._check_id(cook_id)
        now = self._clock()
        found = await self._find_cook(cook_id)
        if found is None:
            raise CookNotFoundError
        return clone({"cook": found[0], "derived": derive(found[0], now)})

    async def edit(
        self, cook_id: str, name: str | None = None, notes: str | None = None
    ) -> dict[str, Any]:
        """Rename or annotate a cook (POST /:id): name up to 120 and notes up to 5000 UTF-16
        units, as typed; None leaves one as it is. Returns the cook's summary.
        """
        self._check_id(cook_id)
        now = self._clock()
        patch: dict[str, Any] = {"edited_at": now}
        if isinstance(name, str):
            patch["name"] = _utf16_prefix(name, NAME_UNITS)
        if isinstance(notes, str):
            patch["notes"] = _utf16_prefix(notes, NOTES_UNITS)
        result: dict[str, Any] | None = None

        async def run() -> None:
            nonlocal result
            found = await self._find_cook(cook_id)
            if found is None:
                return
            cook, where = found  # name and notes already the newest (_with_edits)
            if where == "file":
                c = {**cook, **patch, "edited": True}
                await self._write_atomic(f"{self._dir}/cooks/{cook_id}.json", c)
                self._index[cook_id] = summarize(c)
                self._remember(c)
                result = self._index[cook_id]
            else:
                self._edits[cook_id] = {
                    "name": cook.get("name", UNDEFINED),
                    "notes": cook.get("notes", UNDEFINED),
                    **patch,
                }
                await self._flush()
                result = summarize({**cook, **patch}, now)

        await self._queue(run, "edit")
        if result is None:
            raise CookNotFoundError
        return clone(result)

    async def delete(self, cook_id: str) -> dict[str, Any]:
        """Delete a finished cook (DELETE /:id): its file moves to trash/, or a pending one is
        written there, and only then is it forgotten. {deleted: id}
        """
        self._check_id(cook_id)
        status = 500

        def is_open() -> bool:
            open_cook = self._open_cook()
            return open_cook is not None and open_cook["id"] == cook_id

        if is_open():
            raise CookOpenError

        async def run() -> None:
            nonlocal status
            if is_open():
                # asked again here, in the queue: what ran before (a reconcile that found a
                # replacement probe) may have reopened the cook since the call
                status = 409
                return
            unwritten = next((c for c in self._pending if c["id"] == cook_id), None)
            indexed = self._index.get(cook_id)
            if unwritten is None and indexed is None:
                status = 404
                return
            span = _meta(unwritten or indexed or {})
            # the recoverable copy in trash/ first; only then forget the cook
            try:
                await self._rename_owned(
                    f"{self._dir}/cooks/{cook_id}.json", f"{self._dir}/trash/{cook_id}.json"
                )
            except FileNotFoundError:
                if unwritten is None:
                    raise
                await self._write_atomic(
                    f"{self._dir}/trash/{cook_id}.json", self._with_edits(unwritten)
                )
            self._trash[cook_id] = span
            self._pending = [c for c in self._pending if c["id"] != cook_id]
            self._index.pop(cook_id, None)
            self._saved.pop(cook_id, None)
            self._edits.pop(cook_id, None)
            await self._flush()
            status = 200

        await self._queue(run, "delete")
        if status == 200:
            return {"deleted": cook_id}
        if status == 404:
            raise CookNotFoundError
        if status == 409:
            raise CookOpenError
        raise DeleteFailedError

    async def list(self) -> dict[str, Any]:
        """{ready, open, cooks}: the open cook's summary and every other one, newest first
        (GET /).
        """
        now = self._clock()
        by_id = dict(self._index)
        for c in self._pending:
            # a written cook a reconcile re-derived is pending again until its file is
            # rewritten: _with_edits gives it the name and notes its file holds meanwhile
            by_id[c["id"]] = summarize(self._with_edits(c))
        open_cook = self._open_cook()
        cooks = [
            s
            for s in (by_id[key] for key in js_keys(by_id))
            if s["id"] not in self._trash and (open_cook is None or s["id"] != open_cook["id"])
        ]
        cooks.sort(key=lambda s: s["start"], reverse=True)
        summary = summarize(self._with_edits(open_cook), now) if open_cook else None
        return clone({"ready": self._ready, "open": summary, "cooks": cooks})
