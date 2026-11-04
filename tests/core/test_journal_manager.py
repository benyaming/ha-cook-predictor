"""The journal manager, driven as test-journal.mjs drives the Node-RED function node: the 25
"node:" cases, one test each, same names and assertions (on the normalised inputs: a probe
that is out reads "disconnected", the one pit is channel "4"); then channels, membership and
presses, each live and re-derived through a reconcile, and the migration of a schema-1
state.json.

The world is an in-memory filesystem (writes and renames can fail or be held), a settable
clock and a recorder that answers the manager's history queries the way HA does (and the
runtime's adapter does for the pseudo ids): each window starts with the state in force at
its start, stamped with the window start, and a row exists only once it is committed. A
Node is one generation of the journal; a restart is a new Node, a redeploy a new Node given
the old one as `previous`.
"""

import asyncio
import errno
import itertools
import json
import logging
import math
from typing import Any, NamedTuple

import pytest

from custom_components.cook_predictor.core import journal as J
from custom_components.cook_predictor.core.journal_manager import (
    CookOpenError,
    JournalError,
    JournalManager,
    PressFailedError,
)


def P(n):
    """A channel's pseudo id: internals 1 to 3 and the pit 4 in the ported cases."""
    return f"journal.channel_{n}"


def CP(n):
    return f"journal.predictor_{n}"


def TG(n):
    return f"journal.target_{n}"


RESET = "journal.new_cook"
FINISH = "journal.finish"
MEMBERS = "journal.members"
IDS = {
    "probes": {1: P(1), 2: P(2), 3: P(3)},
    "pits": {4: P(4)},
    "predictors": {1: CP(1), 2: CP(2), 3: CP(3)},
    "targets": {1: TG(1), 2: TG(2), 3: TG(3)},
    "reset": RESET,
    "finish": FINISH,
    "members": MEMBERS,
}
iso = J.iso
T0 = 1791680400  # 2026-10-11 04:00 local
UNPLUGGED = "disconnected"
DIR = "/homeassistant/cook-journal"
STATE = f"{DIR}/state.json"


def same(a, b):
    """The JS tests' same(): equal as JSON text (key order and number formatting included)."""
    assert J.to_json(a) == J.to_json(b)


def heating(t0, t1, *, n=1, start=5, pit=110, k=1e-4, dt=60):
    """A probe heating from `start` C toward `pit` at k, read every `dt` s, plus the pit."""
    rows = []
    t = t0
    while t <= t1:
        v = pit + (start - pit) * math.exp(-k * (t - t0))
        rows.append([t, P(n), J.number_text(J.js_round(v * 18) / 18)])
        rows.append([t + 1, P(4), J.number_text(pit)])
        t += dt
    return rows


class MemoryFilesystem:
    """Files in a dict. fail_write/fail_rename make an operation on a matching path fail
    (EIO); hold_write holds a write after it wrote, hold_rename a rename before it moves, at
    a gate that stays open once release() opens it.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.fail_write = None
        self.fail_rename = None
        self.hold_write = None
        self.hold_rename = None
        self.holds: list[str] = []
        self._gate = asyncio.Event()

    async def _hold(self, path: str) -> None:
        if self._gate.is_set():
            return
        self.holds.append(path)
        await self._gate.wait()

    async def wait_held(self) -> None:
        for _ in range(1000):
            if self.holds:
                return
            await asyncio.sleep(0)
        raise AssertionError("nothing was held")

    def release(self) -> None:
        self._gate.set()
        self.holds.clear()

    async def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, "ENOENT", path)
        return self.files[path]

    async def write_text(self, path: str, data: str) -> None:
        if self.fail_write and self.fail_write(path):
            raise OSError(errno.EIO, "EIO", path)
        data.encode()  # what a real file would refuse
        self.files[path] = data
        if self.hold_write and self.hold_write(path):
            await self._hold(path)

    async def rename(self, source: str, target: str) -> None:
        if self.fail_rename and self.fail_rename(target):
            raise OSError(errno.EIO, "EIO", target)
        if self.hold_rename and self.hold_rename(target):
            await self._hold(target)
        if source not in self.files:
            raise FileNotFoundError(errno.ENOENT, "ENOENT", source)
        self.files[target] = self.files.pop(source)

    async def unlink(self, path: str) -> None:
        self.files.pop(path, None)

    async def listdir(self, path: str) -> list[str]:
        prefix = f"{path}/"
        return [
            k[len(prefix) :]
            for k in self.files
            if k.startswith(prefix) and "/" not in k[len(prefix) :]
        ]

    async def mkdir(self, path: str) -> None:
        pass


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Request(NamedTuple):
    start: float
    end: float
    entity_ids: list[str]
    minimal: bool
    answer: asyncio.Future
    task: asyncio.Task | None


class World:
    """The shared world: disk, recorder rows (t, id, state, attrs, committed_at) and clock."""

    def __init__(self) -> None:
        self.fs = MemoryFilesystem()
        self.rec: list[tuple[Any, ...]] = []
        self.clock = FakeClock(T0)
        self.nodes: list[Node] = []

    async def close(self) -> None:
        """Nothing may linger: stop every generation and end the queries nobody answered."""
        for node in self.nodes:
            node.manager.stop()
        self.fs.release()
        waiting = []
        for node in self.nodes:
            for q in node.recorder.requests + node.recorder.dropped:
                q.answer.cancel()
                if q.task is not None:
                    waiting.append(q.task)
        if waiting:
            await asyncio.wait(waiting)
        for node in self.nodes:
            await node.manager.drain()


class FakeRecorder:
    """Holds each history query until the test answers it, from what is committed by then,
    as Home Assistant's history answers: a window [start, end) whose first row is the state
    in force at its start, stamped start, then each row after the start and before the end
    (a row exactly at the end is left out).
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self.requests: list[Request] = []
        self.dropped: list[Request] = []

    async def history(self, start, end, entity_ids, minimal):
        answer = asyncio.get_running_loop().create_future()
        self.requests.append(
            Request(start, end, list(entity_ids), minimal, answer, asyncio.current_task())
        )
        return await answer

    def window(self, q: Request) -> dict[str, list[dict[str, Any]]]:
        result = {}
        for entity_id in q.entity_ids:
            rows = sorted(
                (r for r in self.world.rec if r[1] == entity_id and r[4] <= self.world.clock.now),
                key=lambda r: r[0],
            )
            before = [r for r in rows if r[0] <= q.start][-1:]
            inside = [r for r in rows if q.start < r[0] < q.end]

            def fmt(r, lu):
                return (
                    {"s": r[2], "lu": lu} if q.minimal else {"s": r[2], "a": r[3] or {}, "lu": lu}
                )

            out = [fmt(r, q.start) for r in before] + [fmt(r, r[0]) for r in inside]
            if out:
                result[entity_id] = out
        return result


class Reply(NamedTuple):
    status: int
    payload: Any


_names = itertools.count()


class Node:
    """One generation of the journal, as the JS test's instance() is one of the node."""

    def __init__(
        self,
        world: World,
        *,
        build: str = "b1",
        previous: Node | None = None,
        ids: dict[str, Any] = IDS,
    ) -> None:
        self.world = world
        self.recorder = FakeRecorder(world)
        self.logs: list[str] = []
        logger = logging.getLogger(f"tests.journal.node{next(_names)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.Handler()
        handler.emit = lambda record: self.logs.append(f"{record.levelname} {record.getMessage()}")
        logger.addHandler(handler)
        self.manager = JournalManager(
            world.fs,
            self.recorder,
            world.clock,
            directory=DIR,
            ids=ids,
            build=build,
            previous=previous.manager if previous else None,
            logger=logger,
        )
        world.nodes.append(self)

    async def settle(self) -> None:
        await self.manager.drain()
        await asyncio.sleep(0)  # queries just issued reach the recorder

    async def answer(
        self, *, drop: bool = False, reverse: bool = False, settle: bool = True
    ) -> None:
        """Answer the queued history queries from what the recorder has committed by now:
        in the order asked, or the last asked first (`reverse`); `settle=False` does not wait
        for the file operations the answers start (one ahead of them may be held).
        """
        await asyncio.sleep(0)
        while self.recorder.requests:
            q = self.recorder.requests.pop(-1 if reverse else 0)
            if drop:
                self.recorder.dropped.append(q)
                continue
            q.answer.set_result(self.recorder.window(q))
            await asyncio.wait([q.task])
            if settle:
                await self.settle()

    async def start(self, *, reverse: bool = False) -> None:
        await self.manager.start()
        await self.settle()
        await self.answer(reverse=reverse)

    async def live(self, t, entity_id, state, attrs=None, *, missed=False) -> None:
        """A state change: committed to the recorder, and delivered live unless `missed`."""
        self.world.rec.append((t, entity_id, state, attrs, t))
        self.world.clock.now = max(self.world.clock.now, t)
        if not missed:
            self.manager.observe_live(entity_id, t, state, attrs or {})
            await self.settle()

    async def tick(self, t) -> None:
        self.world.clock.now = t
        await self.manager.tick()
        await self.settle()

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Reply:
        """An API call as the HTTP reply it replaces: (status, payload)."""
        try:
            payload = await getattr(self.manager, method)(*args, **kwargs)
        except JournalError as err:
            reply = Reply(err.status, err.payload)
        else:
            reply = Reply(200, payload)
        await self.settle()
        return reply

    async def list(self) -> Reply:
        return await self.call("list")

    async def get(self, cook_id) -> Reply:
        return await self.call("get", cook_id)

    async def edit(self, cook_id, **fields) -> Reply:
        return await self.call("edit", cook_id, **fields)

    async def delete(self, cook_id) -> Reply:
        return await self.call("delete", cook_id)


def recorded(w: World, rows, committed_at=None) -> None:
    for r in rows:
        attrs = r[3] if len(r) > 3 else None
        w.rec.append((r[0], r[1], r[2], attrs, r[0] if committed_at is None else committed_at))


def cook_files(w: World) -> list[str]:
    return [k for k in w.fs.files if "/cooks/" in k and k.endswith(".json")]


def state_of(w: World) -> dict[str, Any]:
    return json.loads(w.fs.files[STATE])


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
async def journal_worlds():
    """A maker of worlds, each closed after the test."""
    worlds: list[World] = []

    def make() -> World:
        worlds.append(World())
        return worlds[-1]

    yield make
    for world in worlds:
        await world.close()


@pytest.fixture
def journal_world(journal_worlds):
    return journal_worlds()


async def test_node_a_live_cook_is_saved_finished_written_and_listed(journal_world):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 2 * 3600):
        await a.live(*r)
    assert (await a.list()).payload["open"], "listed while open"
    await a.live(T0 + 2 * 3600 + 60, FINISH, iso(T0 + 2 * 3600 + 60))
    assert len(cook_files(w)) == 1
    listed = await a.list()
    assert listed.payload["open"] is None
    assert len(listed.payload["cooks"]) == 1
    assert listed.payload["cooks"][0]["ended_by"] == "finish"
    assert len(state_of(w)["pending"]) == 0
    one = await a.get(listed.payload["cooks"][0]["id"])
    assert one.payload["cook"]
    assert one.payload["derived"]
    assert one.payload["derived"]["probes"]["1"]


async def test_node_restart_31_h_later_recovers_the_cook_from_the_recorder(journal_world):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    # reconcile once so the snapshot is near T0 + 1 h, then the journal goes down
    await a.tick(T0 + 3600 + 200)
    await a.answer()
    recorded(w, heating(T0 + 3660, T0 + 12 * 3600))
    recorded(w, [[T0 + 12 * 3600 + 60, P(1), UNPLUGGED], [T0 + 12 * 3600 + 3000, P(4), "25"]])
    w.clock.now = T0 + 32 * 3600
    b = Node(w)  # restart: nothing carried over
    await b.start()
    files = cook_files(w)
    assert len(files) == 1
    c = json.loads(w.fs.files[files[0]])
    assert c["ended_by"] == "unplugged"
    assert c["end"] == T0 + 12 * 3600 + 60
    assert c["probes"]["1"]["series"][-2][1] > 10000, "heated past 100 C, from the recorder"
    assert len(c["fills"]) >= 1


async def test_node_new_cook_pressed_while_node_red_was_down_after_a_finished_cook(journal_world):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    await a.live(T0 + 3700, FINISH, iso(T0 + 3700))
    await a.tick(T0 + 4100)
    recorded(w, [[T0 + 5000, RESET, iso(T0 + 5000)], *heating(T0 + 5060, T0 + 9000, start=6)])
    w.clock.now = T0 + 9100
    b = Node(w)
    await b.start()
    opened = (await b.list()).payload["open"]
    assert opened, "the new cook is open"
    assert opened["start"] == T0 + 5000
    assert opened["started_by"] == "new_cook"
    assert len(cook_files(w)) == 1


async def test_node_a_press_the_recorder_commits_late_is_caught_by_the_reconcile_after_startup(
    journal_world,
):
    # review repro: Finish at 999 while the journal was stopped; startup asks before it is
    # committed
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    await a.tick(T0 + 3700)
    await a.answer()
    recorded(w, heating(T0 + 3660, T0 + 5000))
    # committed after startup's query
    recorded(w, [[T0 + 5000 - 1, FINISH, iso(T0 + 5000 - 1)]], T0 + 5100)
    w.clock.now = T0 + 5000
    b = Node(w)
    await b.start()
    assert (await b.list()).payload["open"], "not seen yet"
    await b.tick(T0 + 5000 + 130)  # the reconcile two minutes after startup
    await b.answer()
    assert (await b.list()).payload["open"] is None
    files = cook_files(w)
    assert len(files) == 1
    assert json.loads(w.fs.files[files[0]])["end"] == T0 + 5000 - 1


async def test_node_events_missed_while_the_ha_connection_was_down_are_repaired_by_the_reconcile(
    journal_world,
):
    w = journal_world
    a = Node(w)
    await a.start()
    await a.tick(T0 + 130)  # the reconcile after startup; the next is due 30 min later
    await a.answer()
    for r in heating(T0 + 160, T0 + 1500):
        await a.live(*r)
    # the connection drops: this reaches the recorder but not the journal
    await a.live(T0 + 1600, FINISH, iso(T0 + 1600), None, missed=True)
    await a.tick(T0 + 1700)
    assert len(a.recorder.requests) == 0, "no reconcile before its time"
    assert (await a.list()).payload["open"]
    await a.tick(T0 + 130 + 1800 + 60)  # the periodic one
    await a.answer()
    assert (await a.list()).payload["open"] is None
    assert len(cook_files(w)) == 1


async def test_node_a_recorder_window_that_starts_mid_cook_does_not_invent_a_reading(journal_world):
    # a flat probe (no change for hours, no predictor) that goes unavailable before Finish:
    # re-derived over several 6 h windows, each window's starting row must not count as a
    # fresh reading, so the end is the same as live
    w = journal_world
    a = Node(w)
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10))
    await a.live(T0 + 60, P(1), "70")
    for t in range(T0 + 600, T0 + 8 * 3600, 600):
        await a.live(t, P(4), J.number_text(110 + (1 if t % 1200 else 0)))
    await a.live(T0 + 8 * 3600, P(1), "unavailable")
    early = w.fs.files[STATE]
    await a.live(T0 + 8 * 3600 + 60, FINISH, iso(T0 + 8 * 3600 + 60))
    live_end = json.loads(w.fs.files[cook_files(w)[0]])["end"]
    w.fs.files[STATE] = early
    for f in cook_files(w):
        del w.fs.files[f]
    w.clock.now = T0 + 9 * 3600
    b = Node(w)
    await b.start()
    assert json.loads(w.fs.files[cook_files(w)[0]])["end"] == live_end


async def test_node_replaced_code_cannot_commit_a_write_it_had_started(journal_world):
    # the old generation is stopped while its save is between writing the temp file and the
    # rename
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    before = w.fs.files[STATE]
    w.fs.hold_write = lambda p: "state.json.tmp" in p
    pending = asyncio.create_task(a.live(T0 + 3700, FINISH, iso(T0 + 3700)))
    await w.fs.wait_held()
    a.manager.stop()  # stopped mid-write
    w.fs.release()
    await pending
    w.fs.hold_write = None
    assert w.fs.files[STATE] == before
    assert any("superseded" in line for line in a.logs)


async def test_node_a_deleted_cook_that_ended_by_unplugging_restored_as_open_is_not_shown(
    journal_world,
):
    # no press bounds it, so if the recorder lost the unplug it comes back open: still deleted
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    await a.tick(T0 + 3700)
    await a.answer()
    early = w.fs.files[STATE]
    await a.live(T0 + 3800, P(1), UNPLUGGED)
    await a.tick(T0 + 3800 + 1900)
    cid = (await a.list()).payload["cooks"][0]["id"]
    assert (await a.delete(cid)).status == 200
    w.fs.files[STATE] = early
    w.rec = [r for r in w.rec if r[2] != UNPLUGGED]
    w.clock.now = T0 + 3900
    b = Node(w)
    await b.start()
    assert (await b.list()).payload["open"] is None
    assert (await b.get(cid)).status == 404


async def test_node_a_deleted_cook_restored_as_open_by_an_old_checkpoint_is_not_shown(
    journal_world,
):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    await a.tick(T0 + 3700)  # reconcile: the snapshot holds the open cook
    await a.answer()
    early = w.fs.files[STATE]
    await a.live(T0 + 3800, FINISH, iso(T0 + 3800))
    cid = (await a.list()).payload["cooks"][0]["id"]
    assert (await a.delete(cid)).status == 200
    # the old checkpoint comes back and the Finish row is gone from the recorder
    w.fs.files[STATE] = early
    w.rec = [r for r in w.rec if r[1] != FINISH]
    w.clock.now = T0 + 4000
    b = Node(w)
    await b.start()
    assert (await b.list()).payload["open"] is None
    assert (await b.get(cid)).status == 404


async def test_node_a_history_query_that_times_out_is_retried_not_skipped(journal_world):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    await a.live(T0 + 3700, FINISH, iso(T0 + 3700), None, missed=True)
    await a.tick(T0 + 3600 + 2000)  # reconcile due: asks
    await a.answer(drop=True)  # ...and never hears back
    await a.tick(T0 + 3600 + 2000 + 200)  # gives up
    assert (await a.list()).payload["open"]
    await a.tick(T0 + 3600 + 2000 + 200 + 310)  # retried
    await a.answer()
    assert (await a.list()).payload["open"] is None


async def test_node_a_failed_write_keeps_the_finished_cook_in_state_json_until_it_succeeds(
    journal_world,
):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    w.fs.fail_write = lambda p: "/cooks/" in p
    await a.live(T0 + 3700, FINISH, iso(T0 + 3700))
    assert len(cook_files(w)) == 0
    assert len(state_of(w)["pending"]) == 1
    assert any(line.startswith("ERROR") for line in a.logs)
    w.fs.fail_write = None
    w.clock.now = T0 + 7200
    b = Node(w)  # restart
    await b.start()
    assert len(cook_files(w)) == 1
    assert len(state_of(w)["pending"]) == 0


async def test_node_a_delete_that_cannot_write_its_trash_copy_keeps_the_cook(journal_world):
    # review repro: cook file write fails, then the trash write fails during delete
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    w.fs.fail_write = lambda p: "/cooks/" in p or "/trash/" in p
    await a.live(T0 + 3700, FINISH, iso(T0 + 3700))
    cid = state_of(w)["pending"][0]["id"]
    deleted = await a.delete(cid)
    assert deleted.status == 500
    w.fs.fail_write = None
    await a.tick(T0 + 3800)
    await a.live(T0 + 3900, P(4), "30")  # anything: the next save writes the pending cook
    await a.tick(T0 + 4000)
    assert len(cook_files(w)) == 1 or len(state_of(w)["pending"]) == 1, (
        "the cook still exists somewhere"
    )
    assert (await a.get(cid)).status == 200


async def test_node_a_cook_deleted_before_its_file_was_written_stays_deleted_after_a_restart(
    journal_world,
):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    early = w.fs.files[STATE]  # a genuine earlier checkpoint
    w.fs.fail_write = lambda p: "/cooks/" in p
    await a.live(T0 + 3700, FINISH, iso(T0 + 3700))
    cid = state_of(w)["pending"][0]["id"]
    w.fs.fail_write = None
    deleted = await a.delete(cid)
    assert deleted.status == 200
    assert f"{DIR}/trash/{cid}.json" in w.fs.files, "a tombstone"
    # restart from the earlier checkpoint: the cook is re-derived, but stays deleted
    w.fs.files[STATE] = early
    w.clock.now = T0 + 9000
    b = Node(w)
    await b.start()
    assert len(cook_files(w)) == 0
    assert len((await b.list()).payload["cooks"]) == 0
    assert (await b.get(cid)).status == 404


async def test_node_a_rename_survives_the_cook_being_re_derived_from_an_earlier_checkpoint(
    journal_world,
):
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    early = w.fs.files[STATE]
    opened = (await a.list()).payload["open"]
    await a.edit(opened["id"], name="Brisket 6 kg")  # named while cooking
    await a.live(T0 + 3700, FINISH, iso(T0 + 3700))
    r = await a.edit(opened["id"], notes="post oak")  # noted after
    assert r.payload["name"] == "Brisket 6 kg"
    w.fs.files[STATE] = early
    w.clock.now = T0 + 9000
    b = Node(w)
    await b.start()
    c = json.loads(w.fs.files[cook_files(w)[0]])
    assert c["name"] == "Brisket 6 kg"
    assert c["notes"] == "post oak"


async def test_node_replaced_code_commits_nothing_after_the_new_generation_starts(journal_world):
    # review repro: an old checkpoint held just before its rename, new code starts and writes
    w = journal_world
    a = Node(w, build="b1")
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    w.fs.hold_rename = lambda p: p.endswith("state.json")
    held = asyncio.create_task(a.live(T0 + 3700, FINISH, iso(T0 + 3700)))  # stops at the rename
    await w.fs.wait_held()
    w.fs.hold_rename = None
    a.manager.stop()  # the old generation is stopped
    b = Node(w, build="b2", previous=a)  # new code takes over
    started = asyncio.create_task(b.manager.start())
    await settle()
    await b.answer()  # whatever the new code has asked by now (nothing while it waits)
    await settle()
    # however long the old rename takes, the new code has written nothing
    assert state_of(w)["build"] != "b2"
    assert b.manager.ready is False
    w.fs.release()  # the old rename completes now
    await held
    await started
    await b.settle()
    await b.answer()
    await b.settle()
    # the new code's state is what is on disk, not the old rename that landed late
    s = state_of(w)
    assert s["build"] == "b2"
    await b.live(T0 + 4000, RESET, iso(T0 + 4000))
    assert (await b.list()).payload["open"]["start"] == T0 + 4000


async def missed_press(a: Node) -> str:
    """review repro: New cook at 10 reaches the recorder but not the journal; probe 20 C at
    60, 30 C at 600; Finish at 1200. Live names the cook by its start at 60; the reconcile
    finds the press and the start moves to 10, but the cook keeps its id.
    """
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10), None, missed=True)
    await a.live(T0 + 60, P(1), "20")
    await a.live(T0 + 600, P(1), "30")
    await a.live(T0 + 1200, FINISH, iso(T0 + 1200))
    return (await a.list()).payload["cooks"][0]["id"]


async def test_node_a_reconcile_that_moves_a_cooks_start_keeps_its_identity(journal_world):
    w = journal_world
    a = Node(w)
    cid = await missed_press(a)
    assert cid == J.cook_id(T0 + 60)
    await a.tick(T0 + 2000)
    await a.answer()
    cooks = (await a.list()).payload["cooks"]
    assert len(cooks) == 1
    assert cooks[0]["id"] == cid
    assert cooks[0]["start"] == T0 + 10, "the record itself is corrected"
    assert len(cook_files(w)) == 1


async def test_node_a_cook_deleted_before_a_reconcile_moves_its_start_stays_deleted(journal_world):
    w = journal_world
    a = Node(w)
    cid = await missed_press(a)
    assert (await a.delete(cid)).status == 200
    await a.tick(T0 + 2000)
    await a.answer()
    assert len((await a.list()).payload["cooks"]) == 0
    assert len(cook_files(w)) == 0


async def divided_cooks(w: World, *, delete_first: bool = False):
    """Two written cooks divided by an observed New cook press at 1000, named; then the
    journal restarts from a checkpoint before that press and the recorder has lost its row.
    """
    a = Node(w)
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10))
    for r in heating(T0 + 60, T0 + 900):
        await a.live(*r)
    await a.tick(T0 + 950)  # reconcile: the snapshot is before the second press
    await a.answer()
    early = w.fs.files[STATE]
    await a.live(T0 + 1000, RESET, iso(T0 + 1000))
    for r in heating(T0 + 1060, T0 + 1900, start=40):
        await a.live(*r)
    await a.live(T0 + 2000, FINISH, iso(T0 + 2000))
    second, first = (await a.list()).payload["cooks"]
    await a.edit(first["id"], name="first")
    await a.edit(second["id"], notes="second notes")
    if delete_first:
        assert (await a.delete(first["id"])).status == 200
    w.fs.files[STATE] = early
    w.rec = [r for r in w.rec if not (r[1] == RESET and r[0] == T0 + 1000)]
    w.clock.now = T0 + 3000
    b = Node(w)
    await b.start()
    return b, first, second


async def test_node_a_press_the_recorder_lost_does_not_merge_two_observed_cooks(journal_world):
    w = journal_world
    b, first, second = await divided_cooks(w)
    cooks = (await b.list()).payload["cooks"]
    same(
        [[c["id"], c["start"], c["end"]] for c in cooks],
        [[second["id"], T0 + 1000, T0 + 2000], [first["id"], T0 + 10, T0 + 1000]],
    )
    assert cooks[1]["name"] == "first"
    assert cooks[0]["notes"] == "second notes"


async def test_node_deleting_one_of_two_cooks_leaves_the_other_when_the_recorder_lost_their_boundary(  # noqa: E501
    journal_world,
):
    # review repro: A=[10,1000] deleted, B=[1000,2000] kept, the dividing press gone from the
    # recorder
    w = journal_world
    b, _, second = await divided_cooks(w, delete_first=True)
    cooks = (await b.list()).payload["cooks"]
    same([c["id"] for c in cooks], [second["id"]])
    assert cooks[0]["notes"] == "second notes"


async def test_node_every_part_of_a_deleted_cook_that_a_reconcile_splits_stays_deleted(
    journal_world,
):
    # review repro: New cook at 10 observed, another at 600 missed live, Finish at 1200; the
    # single cook [10,1200] is deleted; the reconcile finds the press at 600
    w = journal_world
    a = Node(w)
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10))
    await a.live(T0 + 60, P(1), "20")
    await a.live(T0 + 600, RESET, iso(T0 + 600), None, missed=True)
    await a.live(T0 + 700, P(1), "30")
    await a.live(T0 + 1200, FINISH, iso(T0 + 1200))
    cid = (await a.list()).payload["cooks"][0]["id"]
    assert (await a.delete(cid)).status == 200
    await a.tick(T0 + 2000)
    await a.answer()
    assert len((await a.list()).payload["cooks"]) == 0
    assert len(cook_files(w)) == 0
    tombstones = [k for k in w.fs.files if "/trash/" in k]
    assert len(tombstones) == 2, "a tombstone for each part"
    # and after a restart the parts stay deleted
    b = Node(w)
    w.clock.now = T0 + 2500
    await b.start()
    assert len((await b.list()).payload["cooks"]) == 0


async def test_node_a_press_after_the_probe_went_quiet_is_replayed_at_its_own_time_not_the_cook_end(
    journal_world,
):
    # review repro: New cook 10, probe 20 C at 60 and 30 C at 600, unplug 700, New cook 800:
    # live ends the cook at 600 (its last valid reading); a reconcile must not invent a press
    # there
    w = journal_world
    a = Node(w)
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10))
    await a.live(T0 + 60, P(1), "20")
    await a.live(T0 + 600, P(1), "30")
    await a.live(T0 + 700, P(1), UNPLUGGED)
    await a.live(T0 + 800, RESET, iso(T0 + 800))
    same([[c["start"], c["end"]] for c in (await a.list()).payload["cooks"]], [[T0 + 10, T0 + 600]])
    await a.tick(T0 + 1000)
    await a.answer()
    same([[c["start"], c["end"]] for c in (await a.list()).payload["cooks"]], [[T0 + 10, T0 + 600]])
    assert len(cook_files(w)) == 1


async def test_node_a_press_that_closed_a_backdated_cook_stays_a_boundary_after_the_window_passes_its_end(  # noqa: E501
    journal_world,
):
    # review repro: New cook 10; probe 20 C at 60, 30 C at 600; unplug 700; reconcile at 950
    # (snapshot at 830); Finish at 1000 archives [10,600] with press_at 1000. The Finish row
    # is lost and the journal restarts at 1100: the cook must not reopen, and Finish must
    # still hold.
    w = journal_world
    a = Node(w)
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10))
    await a.live(T0 + 60, P(1), "20")
    await a.live(T0 + 600, P(1), "30")
    await a.live(T0 + 700, P(1), UNPLUGGED)
    await a.tick(T0 + 950)
    await a.answer()
    await a.live(T0 + 1000, FINISH, iso(T0 + 1000))
    same(
        [[c["start"], c["end"], c["press_at"]] for c in (await a.list()).payload["cooks"]],
        [[T0 + 10, T0 + 600, T0 + 1000]],
    )
    w.rec = [r for r in w.rec if r[1] != FINISH]
    w.clock.now = T0 + 1100
    b = Node(w)
    await b.start()
    listed = (await b.list()).payload
    assert listed["open"] is None
    same([[c["start"], c["end"]] for c in listed["cooks"]], [[T0 + 10, T0 + 600]])
    assert b.manager.journal["inhibit"] is True


async def test_node_a_tombstone_that_cannot_be_written_yet_stays_a_deletion_across_restarts(
    journal_world,
):
    # review repro: New cook 10, a New cook at 600 missed live, unplug 1000; the single cook
    # is deleted; a restart without the unplug row splits it, and the second part's tombstone
    # fails
    w = journal_world
    a = Node(w)
    await a.start()
    await a.live(T0 + 10, RESET, iso(T0 + 10))
    await a.live(T0 + 60, P(1), "20")
    await a.live(T0 + 600, RESET, iso(T0 + 600), None, missed=True)
    await a.live(T0 + 700, P(1), "30")
    await a.live(T0 + 1000, P(1), UNPLUGGED)
    await a.tick(T0 + 2900)  # 30 min unplugged: closed
    await a.answer(drop=True)  # the reconcile this tick asks for never comes back
    cid = (await a.list()).payload["cooks"][0]["id"]
    assert (await a.delete(cid)).status == 200
    w.rec = [r for r in w.rec if r[2] != UNPLUGGED]
    w.fs.fail_write = lambda p: "/trash/" in p
    w.clock.now = T0 + 3000
    b = Node(w)
    await b.start()
    assert (await b.list()).payload["open"] is None, "the second part is hidden"
    w.fs.fail_write = None
    w.clock.now = T0 + 9000
    c = Node(w)
    await c.start()
    assert (await c.list()).payload["open"] is None, "and still after a restart"
    assert len((await c.list()).payload["cooks"]) == 0
    assert len(state_of(w)["tombstones"]) == 0, "the tombstone got written"


async def test_node_ids_validated_the_open_cook_not_deletable(journal_world):
    """The JS case 'HTTP only through ingress, ids validated, the open cook not deletable'.
    The ingress check (403 for a request not from the Supervisor) has no equivalent here:
    access is the caller's. What remains is checked on the methods: a bad id is refused by
    each of them before anything else, and the open cook cannot be deleted.
    """
    w = journal_world
    a = Node(w)
    await a.start()
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    assert (await a.get("../state")).status == 400
    assert (await a.edit("../state", name="x")).status == 400
    assert (await a.delete("../state")).status == 400
    listed = await a.list()
    assert listed.payload["open"]
    assert listed.payload["open"]["status"] == "open"
    assert (await a.delete(listed.payload["open"]["id"])).status == 409
    assert (await a.get(listed.payload["open"]["id"])).payload["cook"]["status"] == "open"


async def test_node_live_events_during_the_startup_query_are_merged_in_time_order(journal_world):
    w = journal_world
    a = Node(w)
    recorded(w, heating(T0, T0 + 3600))
    w.clock.now = T0 + 3600
    await a.manager.start()
    await a.settle()
    # while the history answers are outstanding, the probe and the button go on live; the
    # recorder has not committed these yet when the query is answered
    committed_later = T0 + 4000
    for t, entity_id, s in [
        [T0 + 3660, P(1), "40"],
        [T0 + 3720, RESET, iso(T0 + 3720)],
        [T0 + 3780, P(1), "41"],
    ]:
        w.rec.append((t, entity_id, s, None, committed_later))
        a.manager.observe_live(entity_id, t, s, {})
        await a.settle()
    assert a.manager.ready is False
    w.clock.now = T0 + 3800
    await a.answer()
    opened = (await a.list()).payload["open"]
    assert opened, "the press during the query opened a new cook"
    assert opened["start"] == T0 + 3720
    assert len(cook_files(w)) == 1, "and closed the one before it"
    assert opened["probes"][0]["last"] == 41


# ---- channels, membership and presses (schema 2) ----

CH_IDS = {
    "probes": {1: P(1), 2: P(2), 5: P(5)},
    "pits": {4: P(4), "a": P("a"), "b": P("b")},
    "predictors": {1: CP(1), 2: CP(2), 5: CP(5)},
    "targets": {1: TG(1), 2: TG(2), 5: TG(5)},
    "reset": RESET,
    "finish": FINISH,
    "members": MEMBERS,
}


def internal(name, pit="4"):
    return {"name": name, "role": "internal", "ambient": pit}


def ambient(name):
    return {"name": name, "role": "ambient", "ambient": None}


def members(t, active, rev):
    """A membership row: its state is ignored, its attributes are the applied channel set."""
    return [t, MEMBERS, str(rev), {"active": active, "rev": rev}]


BRISKET = {"1": internal("Brisket"), "4": ambient("Pit")}
PIT_ONLY = {"4": ambient("Pit")}


def tick(t):
    return ("tick", t)


def two_pits():
    active = {
        "1": internal("Brisket", "a"),
        "2": internal("Ribs", "b"),
        "a": ambient("Smoker"),
        "b": ambient("Oven"),
    }
    rows = [members(T0 + 1, active, 1), [T0 + 10, RESET, iso(T0 + 10)]]
    for i, t in enumerate(range(T0 + 60, T0 + 3600, 120)):
        rows += [[t, P(1), J.number_text(20 + i)], [t, P(2), J.number_text(30 + 2 * i)]]
        rows += [[t + 1, P("a"), J.number_text(110 + i % 3)]]
        rows += [[t + 1, P("b"), J.number_text(180 + i % 5)]]
    return [*rows, [T0 + 3700, FINISH, iso(T0 + 3700)]]


def pizza_oven():
    rows = [members(T0 + 1, {"1": internal("Pizza", "b"), "b": ambient("Oven")}, 1)]
    rows.append([T0 + 10, RESET, iso(T0 + 10)])
    for i, t in enumerate(range(T0 + 60, T0 + 1800, 60)):
        rows += [[t, P(1), J.number_text(20 + 2 * i)], [t + 1, P("b"), J.number_text(480 + 3 * i)]]
    return [*rows, [T0 + 1900, FINISH, iso(T0 + 1900)]]


START = [members(T0 + 1, BRISKET, 1), [T0 + 10, RESET, iso(T0 + 10)], *heating(T0 + 60, T0 + 3600)]
REPLACED = {"5": internal("Brisket"), **PIT_ONLY}

# name: (rows and ticks, the end, how the cooks written ended)
SCENARIOS = {
    "retire_and_replace": (
        [
            *START,
            members(T0 + 4000, PIT_ONLY, 2),
            [T0 + 4100, P(4), "111"],
            members(T0 + 4600, REPLACED, 3),
            [T0 + 4700, P(5), "unavailable"],
            tick(T0 + 6000),
            [T0 + 6500, P(5), "61"],
            [T0 + 7000, P(5), "65"],
            [T0 + 8000, FINISH, iso(T0 + 8000)],
        ],
        T0 + 8100,
        ["finish"],
    ),
    "retire_and_replace_in_one_change": (
        [
            *START,
            members(T0 + 4000, REPLACED, 2),
            [T0 + 4700, P(5), "unavailable"],
            [T0 + 6500, P(5), "61"],
            [T0 + 8000, FINISH, iso(T0 + 8000)],
        ],
        T0 + 8100,
        ["finish"],
    ),
    "no_replacement": (
        [
            *START,
            members(T0 + 4000, PIT_ONLY, 2),
            [T0 + 4500, P(4), "115"],
            tick(T0 + 5900),
            members(T0 + 6000, {"5": internal("Ribs"), **PIT_ONLY}, 3),
            [T0 + 6100, P(5), "20"],
        ],
        T0 + 6200,
        ["channels_removed"],
    ),
    "rename": (
        [
            *START[:2],
            *heating(T0 + 60, T0 + 1800),
            members(T0 + 2000, {"1": internal("Brisket 6 kg"), "4": ambient("Kamado")}, 2),
            *heating(T0 + 2060, T0 + 3600, start=60),
            [T0 + 3700, FINISH, iso(T0 + 3700)],
        ],
        T0 + 3800,
        ["finish"],
    ),
    "two_pits": (two_pits(), T0 + 3800, ["finish"]),
    "pizza_oven": (pizza_oven(), T0 + 2000, ["finish"]),
    "disconnected": (
        [*START, [T0 + 3700, P(1), "disconnected"], tick(T0 + 5600)],
        T0 + 5700,
        ["unplugged"],
    ),
}


async def run_live(world: World, steps, end) -> Node:
    a = Node(world, ids=CH_IDS)
    await a.start()
    for step in steps:
        if step[0] == "tick":
            await a.tick(step[1])
            await a.answer()
        else:
            await a.live(*step)
    await a.tick(end)
    await a.answer()
    return a


async def run_rederived(world: World, steps, end, *, reverse: bool = False) -> Node:
    """The same rows, none of them seen live: all from the recorder at a start at `end`
    (its history answers taken in the order asked, or the other way round).
    """
    recorded(world, [step for step in steps if step[0] != "tick"])
    world.clock.now = end
    b = Node(world, ids=CH_IDS)
    await b.start(reverse=reverse)
    return b


def without_fills(c):
    """A cook but for its fills, which mark what a startup recovered from the recorder."""
    return {k: v for k, v in c.items() if k != "fills"}


def outcome(node: Node):
    w = node.world
    files = {}
    for path in cook_files(w):
        c = json.loads(w.fs.files[path])
        files[c["id"]] = without_fills(c)
    js_open = node.manager.journal["open"]
    return files, None if js_open is None else without_fills(J.clone(js_open))


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_node_channels_live_and_re_derived_give_the_same_cooks(journal_worlds, name):
    steps, end, ended = SCENARIOS[name]
    live = await run_live(journal_worlds(), steps, end)
    again = await run_rederived(journal_worlds(), steps, end)
    files, opened = outcome(live)
    same([c["ended_by"] for c in files.values()], ended)
    assert all(c["schema"] == J.SCHEMA for c in files.values())
    same(outcome(again), (files, opened))
    (cook,) = files.values()
    if name.startswith("retire_and_replace"):
        assert cook["id"] == J.cook_id(T0 + 10), "the replacement kept the cook"
        assert set(cook["probes"]) == {"1", "5"}
        assert cook["probes"]["5"]["series"][0][1] is None, "the gap before its first reading"
        assert cook["retired"] == {"1": 3990}
    if name == "no_replacement":
        assert cook["end"] == T0 + 4000
        assert opened["provisional"], "no Finish: the next probe starts a cook by itself"
    if name == "rename":
        assert cook["channels"]["1"]["name"] == "Brisket 6 kg"
        assert cook["channels"]["4"]["name"] == "Kamado"
    if name == "two_pits":
        assert list(cook["pits"]) == ["a", "b"]
    if name == "pizza_oven":
        assert J.summarize({**cook, "fills": []})["pits"]["b"]["max"] >= 520


async def test_node_a_restart_before_or_after_the_deadline_closes_as_live_did(journal_world):
    w = journal_world
    a = Node(w, ids=CH_IDS)
    await a.start()
    for r in START:
        await a.live(*r)
    await a.tick(T0 + 3700)  # reconcile: the snapshot holds the open cook
    await a.answer()
    early = w.fs.files[STATE]
    await a.live(*members(T0 + 4000, PIT_ONLY, 2))
    assert a.manager.journal["open"]["pending_close"]["deadline"] == T0 + 5800
    await a.tick(T0 + 5900)
    (path,) = cook_files(w)
    live = json.loads(w.fs.files[path])
    assert (live["ended_by"], live["end"]) == ("channels_removed", T0 + 4000)
    for restart_at in (T0 + 4600, T0 + 6000):
        w.fs.files[STATE] = early
        for f in cook_files(w):
            del w.fs.files[f]
        w.clock.now = restart_at
        b = Node(w, ids=CH_IDS)
        await b.start()
        if restart_at < T0 + 5800:
            assert b.manager.journal["open"]["pending_close"]["at"] == T0 + 4000
            assert cook_files(w) == []
            await b.tick(T0 + 5900)
            await b.answer()
        (path,) = cook_files(w)
        same(without_fills(json.loads(w.fs.files[path])), without_fills(live))


async def test_node_an_activation_committed_before_the_deadline_but_delivered_late(journal_world):
    w = journal_world
    a = Node(w, ids=CH_IDS)
    await a.start()
    for r in START:
        await a.live(*r)
    await a.tick(T0 + 3700)
    await a.answer()
    cid = (await a.list()).payload["open"]["id"]
    await a.live(*members(T0 + 4000, PIT_ONLY, 2))
    # in the control log in time, but the live row does not arrive
    await a.live(*members(T0 + 5000, REPLACED, 3), missed=True)
    await a.tick(T0 + 5900)  # live: no replacement seen, closed at the retirement
    same([c["ended_by"] for c in (await a.list()).payload["cooks"]], ["channels_removed"])
    await a.answer()  # the reconcile that tick started finds the activation
    listed = (await a.list()).payload
    assert listed["open"]["id"] == cid, "the cook carries on, under its id"
    same(listed["cooks"], [])
    assert a.manager.journal["open"]["waiting"] == {"5": 4990}
    await a.live(T0 + 6500, P(5), "61")
    await a.live(T0 + 7000, FINISH, iso(T0 + 7000))
    (path,) = cook_files(w)
    c = json.loads(w.fs.files[path])
    assert (c["id"], c["ended_by"]) == (cid, "finish")
    same(c["probes"]["5"]["series"], [[4990, None], [6490, 6100]])


async def test_node_press_returns_once_its_save_is_done(journal_world):
    w = journal_world
    a = Node(w, ids=CH_IDS)
    await a.start()
    await a.live(*members(T0 + 1, BRISKET, 1))
    await a.manager.press("new_cook", T0 + 10)
    assert (await a.list()).payload["open"]["start"] == T0 + 10
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    await a.manager.press("finish", T0 + 3700)
    (path,) = cook_files(w)
    assert json.loads(w.fs.files[path])["ended_by"] == "finish"
    same(state_of(w)["pending"], [])
    # the same press again (a replay of the command log) is no second press
    await a.manager.press("finish", T0 + 3700)
    assert len(cook_files(w)) == 1
    with pytest.raises(ValueError, match="not a press"):
        await a.manager.press("reset", T0 + 3800)


async def test_node_press_fails_when_its_save_fails(journal_world):
    w = journal_world
    a = Node(w, ids=CH_IDS)
    await a.start()
    await a.live(*members(T0 + 1, BRISKET, 1))
    await a.manager.press("new_cook", T0 + 10)
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    w.fs.fail_write = lambda p: "state.json.tmp" in p
    with pytest.raises(PressFailedError):
        await a.manager.press("finish", T0 + 3700)
    assert cook_files(w) == []
    # a cook file that cannot be written does not fail a press: it waits in state.json
    w.fs.fail_write = lambda p: "/cooks/" in p
    await a.manager.press("new_cook", T0 + 3800)
    same([c["ended_by"] for c in state_of(w)["pending"]], ["finish"])
    w.fs.fail_write = None
    await a.manager.press("finish", T0 + 3900)  # the probe was still in: a second cook
    same(sorted(json.loads(w.fs.files[p])["start"] for p in cook_files(w)), [T0 + 10, T0 + 3800])
    # a stopped generation takes no press
    a.manager.stop()
    with pytest.raises(PressFailedError):
        await a.manager.press("new_cook", T0 + 4000)


async def test_node_a_press_during_startup_is_applied_by_the_startup_reconcile(journal_world):
    w = journal_world
    recorded(w, [members(T0 - 100, BRISKET, 1), [T0 - 60, P(1), "20"]])
    a = Node(w, ids=CH_IDS)
    await a.manager.start()
    await a.settle()
    assert a.manager.ready is False
    await a.manager.press("new_cook", T0 + 5)  # held in the live log, nothing to wait for yet
    w.clock.now = T0 + 10
    await a.answer()
    opened = (await a.list()).payload["open"]
    assert opened["start"] == T0 + 5
    assert opened["probes"][0]["name"] == "Brisket"


def schema_1_cook(start, end=None, ended_by=None):
    """A cook as the Node-RED journal wrote it."""
    c = {
        "schema": 1, "id": J.cook_id(start), "start": start, "end": end,
        "status": "open" if end is None else "closed", "provisional": False,
        "started_by": "new_cook", "ended_by": ended_by, "watchdog": False, "name": "",
        "notes": "", "probes": {"1": {"first": 20, "min": 20, "series": [[5, 2000], [605, 4000]]}},
        "pit": [[5, 11000]], "targets": {"1": [[0, 93]]}, "predictions": {}, "fills": [],
        "lastObs": start + 605, "lastValidAt": start + 605, "unplugged": {}, "off": {},
    }  # fmt: skip
    if ended_by == "finish":
        c["press_at"] = end
    return c


async def test_node_a_schema_1_state_json_is_migrated(journal_world):
    w = journal_world
    opened = schema_1_cook(T0 - 5)
    finished = schema_1_cook(T0 - 30000, T0 - 20000, "finish")
    deleted = schema_1_cook(T0 - 60000, T0 - 50000, "unplugged")
    legacy = {
        "schema": 1,
        "build": "node-red",
        "snap": {
            "js": {
                "schema": 1,
                "last": {P(1): {"t": T0 + 600, "v": "40"}, P(4): {"t": T0, "v": "110"}},
                "presses": {"new_cook": T0 - 5},
                "since": T0 - 86400,
                "now": T0 + 600,
                "open": opened,
                "inhibit": False,
            },
            "at": T0 + 600,
        },
        "pending": [finished],
        "edits": {opened["id"]: {"name": "Brisket", "notes": "", "edited_at": T0 + 300}},
        "tombstones": [deleted],
    }
    w.fs.files[STATE] = J.to_json(legacy)
    w.clock.now = T0 + 700
    a = Node(w, ids=CH_IDS)
    await a.start()
    # the pending cook and the tombstone are written as they were: schema-1 records
    assert w.fs.files[f"{DIR}/cooks/{finished['id']}.json"] == J.to_json(finished)
    assert w.fs.files[f"{DIR}/trash/{deleted['id']}.json"] == J.to_json(deleted)
    s = state_of(w)
    assert (s["schema"], s["pending"], s["tombstones"]) == (J.SCHEMA, [], [])
    same(s["edits"], legacy["edits"])  # the open cook has no file yet
    js = s["snap"]["js"]
    assert js["schema"] == J.SCHEMA
    same(js["last"], legacy["snap"]["js"]["last"])
    assert "pit" not in js["open"]
    same(js["open"]["pits"], {"4": [[5, 11000]]})
    same(js["open"]["channels"]["1"], internal("Probe 1"))
    listed = (await a.list()).payload
    assert listed["open"]["name"] == "Brisket"
    assert listed["open"]["pits"]["4"]["name"] == "Pit"
    same(
        [(c["id"], c["schema"], c["probes"][0]["n"]) for c in listed["cooks"]],
        [[finished["id"], 1, 1]],
    )
    assert (await a.get(finished["id"])).payload["derived"]["pits"]["pit"]["max"] == 110
    assert (await a.get(deleted["id"])).status == 404
    # and the open cook carries on
    await a.live(T0 + 800, P(1), "45")
    same(a.manager.journal["open"]["probes"]["1"]["series"][-1], [805, 4500])


# ---- review fixes ----


async def test_node_a_re_derived_copy_never_loses_a_saved_name_or_notes(journal_world):
    # a written cook, named; a reconcile re-derives it (pending again, nameless as derived),
    # and its file cannot be rewritten yet
    w = journal_world
    a = Node(w)
    cid = await missed_press(a)
    await a.edit(cid, name="Saved name", notes="Saved notes")
    w.fs.fail_write = lambda p: "/cooks/" in p
    await a.tick(T0 + 2000)
    await a.answer()
    assert [c["id"] for c in state_of(w)["pending"]] == [cid]
    w.fs.fail_write = None
    saved = ["Saved name", "Saved notes"]
    listed = (await a.list()).payload["cooks"][0]
    same([listed["name"], listed["notes"]], saved)
    detail = (await a.get(cid)).payload["cook"]
    same([detail["name"], detail["notes"]], saved)
    # a notes change saved from that detail (the card sends the name as it shows it)
    await a.edit(cid, name=detail["name"], notes="New notes")
    c = json.loads(w.fs.files[f"{DIR}/cooks/{cid}.json"])
    same([c["name"], c["notes"], c["start"]], ["Saved name", "New notes", T0 + 10])


async def test_node_two_new_cook_presses_in_one_second_keep_both_cooks(journal_world):
    w = journal_world
    a = Node(w)
    await a.start()
    await a.live(T0 + 5, P(1), "20")  # connected: it joins each cook at once
    await a.manager.press("new_cook", T0 + 10.1)
    await a.manager.press("new_cook", T0 + 10.2)  # within the same rounded second
    await a.manager.press("finish", T0 + 100)
    await a.settle()
    cooks = sorted((json.loads(w.fs.files[p]) for p in cook_files(w)), key=lambda c: c["start"])
    same(
        [(c["id"], round(c["start"] - T0, 3), round(c["end"] - T0, 3)) for c in cooks],
        [[J.cook_id(T0 + 10), 10.1, 10.2], [J.cook_id(T0 + 11), 10.2, 100]],
    )
    listed = (await a.list()).payload["cooks"]
    same([c["id"] for c in listed], [J.cook_id(T0 + 11), J.cook_id(T0 + 10)])


@pytest.mark.parametrize("reverse", [False, True])
async def test_node_rows_at_one_time_replay_in_one_order_whatever_answers_first(
    journal_worlds, reverse
):
    # channel 5 replaces probe 1 at 4000 and reports unavailable at that same time: the
    # membership row comes first, so 5 is a participant that is off, and the cook closes by
    # the device-off rule; a reading taken before the row that activates it would be dropped
    steps = [
        *START,
        members(T0 + 4000, REPLACED, 2),
        [T0 + 4000, P(5), "unavailable"],
    ]
    end = T0 + 3600 + 6 * 3600 + 60
    live = await run_live(journal_worlds(), steps, end)
    files, opened = outcome(live)
    same([(c["ended_by"], c["end"]) for c in files.values()], [["device_off", T0 + 3600]])
    again = await run_rederived(journal_worlds(), steps, end, reverse=reverse)
    same(outcome(again), (files, opened))


async def test_node_a_delete_queued_behind_a_reconcile_that_reopens_the_cook_is_refused(
    journal_world,
):
    w = journal_world
    a = Node(w, ids=CH_IDS)
    await a.start()
    for r in START:
        await a.live(*r)
    await a.tick(T0 + 3700)
    await a.answer()
    cid = (await a.list()).payload["open"]["id"]
    await a.live(*members(T0 + 4000, PIT_ONLY, 2))
    await a.live(*members(T0 + 5000, REPLACED, 3), missed=True)
    await a.tick(T0 + 5900)  # closed live, written; the reconcile this tick asks is unanswered
    same([c["id"] for c in (await a.list()).payload["cooks"]], [cid])
    # an operation ahead in the queue is held, and a delete of the archive waits behind it
    w.fs.hold_rename = lambda p: "/cooks/" in p
    held = asyncio.create_task(a.manager.edit(cid, notes="busy"))
    await w.fs.wait_held()
    w.fs.hold_rename = None
    deleting = asyncio.create_task(a.manager.delete(cid))
    await settle()
    await a.answer(settle=False)  # the reconcile finds the replacement: the cook reopens
    assert a.manager.journal["open"]["id"] == cid
    w.fs.release()
    with pytest.raises(CookOpenError):
        await deleting
    await held
    await a.settle()
    assert (await a.list()).payload["open"]["id"] == cid
    assert f"{DIR}/trash/{cid}.json" not in w.fs.files


async def test_node_a_change_exactly_at_a_window_boundary_is_recovered(journal_world):
    # a first start 48 h (and a minute) after T0 asks from T0 on in 6 h windows (presses
    # count from T0 + 60): the second window starts at T0 + 6 h, where probe 1 went from 20
    # to 50; the first window leaves a row at its end out, and the second only repeats it as
    # its starting row
    w = journal_world
    recorded(
        w,
        [
            [T0 + 70, RESET, iso(T0 + 70)],
            [T0 + 100, P(1), "20"],
            [T0 + 6 * 3600, P(1), "50"],
            [T0 + 7 * 3600, P(1), "51"],
        ],
    )
    w.clock.now = T0 + 48 * 3600 + 60
    b = Node(w)
    await b.start()
    same(
        b.manager.journal["open"]["probes"]["1"]["series"],
        [[30, 2000], [6 * 3600 - 70, 5000], [7 * 3600 - 70, 5100]],
    )


async def test_node_press_returns_once_state_json_is_written_not_the_cook_files(journal_world):
    w = journal_world
    a = Node(w)
    await a.start()
    await a.manager.press("new_cook", T0 + 10)
    for r in heating(T0 + 60, T0 + 3600):
        await a.live(*r)
    w.fs.hold_rename = lambda p: "/cooks/" in p
    async with asyncio.timeout(5):
        await a.manager.press("finish", T0 + 3700)
    same([c["ended_by"] for c in state_of(w)["pending"]], ["finish"])  # durable
    assert cook_files(w) == []  # its own file is still being written
    w.fs.release()
    await a.settle()
    assert len(cook_files(w)) == 1
