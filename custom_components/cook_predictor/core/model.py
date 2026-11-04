"""The estimator states of every group, and everything done to them.

A group is one ambient channel and the internal channels in its cooker, or the internal
channels with no ambient ("none"): the estimator's math takes one ambient per state, and
temperatures from two cookers never meet. A Model is owned by one thread (the runtime's
worker); nothing else touches it, and what leaves it is a copy.

Observations, in time order: (kind, key, t, value)
  reading   a valid reading of a channel, °C
  off       the channel's probe is disconnected (fed to the estimator as an unplug)
  gap       nothing is known about the channel (unavailable, unknown): stale at once,
            the session kept, and the next reading resumes like a live gap
  report    the source reported its value again unchanged (HA state_reported), °C
  reset     New cook at t (key None: every channel)
"""

from __future__ import annotations

import copy
import math
from typing import Any, Final

from . import estimator as E
from .ledger import latch_key

NONE_GROUP: Final = "none"

# The estimator's latches, and the event each one stands for: (dict, flag, event, threshold)
LATCHES: Final = (
    ("notified", "approach", "approach", "target"),
    ("notified", "done", "done", "target"),
    ("alert", "soon", "alert_soon", "temp"),
    ("alert", "reached", "alert", "temp"),
)


class Model:
    """Every group's estimator state, and which group each channel is in."""

    def __init__(self, groups: dict[str, dict[str, Any]] | None = None) -> None:
        # group id -> {"ambient": ambient key or None, "st": estimator state}
        self.groups: dict[str, dict[str, Any]] = groups or {}
        self.cursor: float | None = None  # the newest observation time processed

    # ---- membership ----

    def group_of(self, key: str) -> str | None:
        for gid, g in self.groups.items():
            if key == g["ambient"] or key in g["st"]["probes"]:
                return gid
        return None

    def set_membership(
        self, internals: dict[str, str | None], ambients: list[str], now: float | None = None
    ) -> None:
        """internals: channel key -> its ambient key (None: no ambient); a group that loses
        everything is dropped.

        A probe that moves to another cooker keeps its session, its readings and its alert
        latches (the same food, the same cook: "done" must not fire again), and relearns
        everything about the heat around it: its fit and stall reference are void, as after
        the probe was moved, from `now`.
        """
        want: dict[str, tuple[str | None, list[str]]] = {}
        for a in ambients:
            want[a] = (a, [])
        for key, amb in internals.items():
            gid = amb if amb in want else NONE_GROUP
            want.setdefault(gid, (None, []))[1].append(key)
        moving: dict[str, dict[str, Any]] = {}
        for gid, g in list(self.groups.items()):
            keep = want.get(gid, (None, []))[1]
            for key in list(g["st"]["probes"]):
                if key not in keep:
                    p = g["st"]["probes"].pop(key)
                    if key in internals:
                        moving[key] = p
            if gid not in want:
                del self.groups[gid]
        for gid, (amb, keys) in want.items():
            g = self.groups.get(gid)
            if g is None:
                g = self.groups[gid] = {"ambient": amb, "st": E.new_state([])}
            probes = g["st"]["probes"]
            for key in keys:
                if key in probes:
                    continue
                if key in moving:
                    p = moving.pop(key)
                    if p.get("session") is not None:
                        E._relearn(p, now if now is not None else (self.cursor or 0))
                    probes[key] = p
                else:
                    probes[key] = E.new_state([key])["probes"][key]

    # ---- observations ----

    def ingest(self, observations: list[tuple[str, str | None, float, float | None]]) -> None:
        for kind, key, t, v in observations:
            self.cursor = t if self.cursor is None else max(self.cursor, t)
            if kind == "reset":
                for g in self.groups.values():
                    st = g["st"]
                    st["reset_at"] = t
                    for k in st["probes"]:
                        E.reset_probe(st, k, t)
                continue
            gid = self.group_of(key)
            if gid is None:
                continue
            g = self.groups[gid]
            st = g["st"]
            ambient = key == g["ambient"]
            if kind == "gap":
                if ambient:
                    E.mark_ambient_gap(st, t)
                else:
                    E.mark_gap(st, key, t)
                continue
            # The estimator's own state says whether a gap is open (a replay or a restored
            # state may have left one), so the first observation after it always ends it.
            in_gap = (
                st.get("ambient_gap_since") is not None
                if ambient
                else st["probes"][key].get("gap_since") is not None
            )
            if in_gap:
                if ambient:
                    E.end_ambient_gap(st, t)
                else:
                    E.end_gap(st, key, t)
            if kind == "reading":
                if ambient:
                    E.ingest_ambient(st, t, v)
                else:
                    E.ingest_probe(st, key, t, v)
            elif kind == "off":
                if ambient:
                    E.ingest_ambient(st, t, math.nan)
                else:
                    E.ingest_probe(st, key, t, math.nan)
            elif kind == "report":
                E.report(st, "ambient" if ambient else key, t, v)

    # ---- evaluation ----

    def evaluate(
        self, now: float, settings: dict[str, tuple[float | None, dict[str, Any]]]
    ) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str]], list[tuple[str, float]]]:
        """Evaluate every internal channel at `now` with its (target, alerts).

        Returns (results by key: status, attrs, events), the events as (key, event, latch
        key) for the ledger, and the latches that re-armed as (latch key, time).
        """
        results: dict[str, dict[str, Any]] = {}
        fired: list[tuple[str, str, str]] = []
        rearmed: list[tuple[str, float]] = []
        for g in self.groups.values():
            st = g["st"]
            for key, p in st["probes"].items():
                target, alerts = settings.get(key, (None, {}))
                before = _latches(key, p)
                stall_since = p["stall"]["since"] if p.get("stall") else None
                r = E.evaluate(st, key, now, target, alerts)
                results[key] = r
                after = _latches(key, p)
                for lk in before - after:
                    rearmed.append((lk, now))
                session = r["attrs"].get("session_start")
                for ev in r["events"]:
                    if ev in ("stall", "stall_end"):
                        onset = p["stall"]["since"] if ev == "stall" else stall_since
                        lk = latch_key(
                            key, ev, session, E._iso(onset) if onset is not None else None
                        )
                    elif ev in ("approach", "done"):
                        lk = latch_key(key, ev, session, target)
                    else:
                        lk = latch_key(key, ev, session, p["alert"].get("temp"))
                    fired.append((key, ev, lk))
        return results, fired, rearmed

    def hold_latches(self, held: list[str]) -> None:
        """Set, silently, the latches of alerts already delivered (after a rebuild)."""
        held_set = set(held)
        for g in self.groups.values():
            for key, p in g["st"]["probes"].items():
                session = E._iso(p["session"]["start"]) if p.get("session") else None
                for box, flag, event, field in LATCHES:
                    d = p.get(box) or {}
                    if latch_key(key, event, session, d.get(field)) in held_set and field in d:
                        d[flag] = True
                        if flag == "approach":
                            d["approach_pending"] = False
                        if flag == "soon":
                            d["soon_pending"] = False

    # ---- persistence ----

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({"groups": self.groups, "cursor": self.cursor})

    @classmethod
    def restore(cls, data: dict[str, Any]) -> Model:
        m = cls(copy.deepcopy(data["groups"]))
        m.cursor = data.get("cursor")
        return m

    def catch_up(
        self,
        rows: dict[str, list[list[Any]]],
        resets: list[float],
        target_of: Any,
        alerts_of: Any,
        grid_s: float,
        cover_end: float,
    ) -> None:
        """Bring a restored state up to cover_end the way a replay would have: the recorded
        rows after the cursor, the New cook presses among them, and an evaluation on the grid
        through the missed stretch with the settings in force at each moment, silently (no
        alert goes out for the past; a crossing in it is history, as in a full replay).

        rows: group id -> replay rows [probe key or "ambient", t, value].
        """
        for gid, g in self.groups.items():
            st = g["st"]
            group_rows = sorted(rows.get(gid, []), key=lambda r: r[1])
            press = [t for t in resets if self.cursor is None or t > self.cursor]
            if press:
                st["reset_at"] = press[-1]
            E.replay(st, group_rows, target_of, grid_s, None, cover_end, alerts_of)
            if press:
                for k in st["probes"]:
                    E.reset_probe(st, k, press[-1])  # a press with no reading after it
        self.cursor = cover_end

    def await_live(self) -> None:
        """After a restore or a replay nothing is fresh until a real reading or report."""
        for g in self.groups.values():
            st = g["st"]
            st["held_is_observed"] = False
            st["replay_end"] = self.cursor
            for p in st["probes"].values():
                if p.get("session"):
                    p["await_live"] = True
            st["ambient_await_live"] = bool(st["ambient"])


def _latches(key: str, p: dict[str, Any]) -> set[str]:
    session = E._iso(p["session"]["start"]) if p.get("session") else None
    out = set()
    for box, flag, event, field in LATCHES:
        d = p.get(box) or {}
        if d.get(flag):
            out.add(latch_key(key, event, session, d.get(field)))
    return out
