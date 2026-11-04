"""The control log: what was configured, set and pressed, and when.

Home Assistant keeps only the current configuration (config subentries) and the current
value of each setting. The estimator's replay and the journal need what was in force at any
past moment, so every change the runtime applies is appended here first, with its effective
time, and nothing acts on a change before it is in the log.

Events (each with `rev`, increasing by one, and `t`, epoch seconds):
  reserve   {key}                  a channel key handed out; keys are never handed out twice
  activate  {key, channel}         a channel starts: its fixed source and role, and the rest
  update    {key, changes}         name, ambient, connection or decoder changed
  alias     {key, entity_id}       the source's entity_id changed (same registry entry)
  retire    {key}                  the channel ends; its descriptor stays known
  setting   {key, field, value}    a per-channel setting (target, warn_min, alert_on,
                                   alert_temp, alert_min)
  press     {what, id}             New cook or Finish, accepted

Pure: the store that reads and writes the file is outside (see ControlStore in runtime).
"""

from __future__ import annotations

import copy
from bisect import bisect_right
from typing import Any, Final

SCHEMA: Final = 1
CHANNEL_FIELDS: Final = (
    "entity_id",
    "source_uid",
    "role",
    "name",
    "ambient",
    "connection",
    "decoder",
)
MUTABLE_FIELDS: Final = ("name", "ambient", "connection", "decoder")
# what decides how a reading is taken and which cooker it belongs to
BINDING_FIELDS: Final = ("entity_id", "connection", "decoder", "ambient")
SETTINGS: Final = ("target", "warn_min", "alert_on", "alert_temp", "alert_min")


def new_log() -> dict[str, Any]:
    return {"schema": SCHEMA, "rev": 0, "high_key": 0, "events": []}


def _append(log: dict[str, Any], t: float, event: dict[str, Any]) -> dict[str, Any]:
    log["rev"] += 1
    ev = {"rev": log["rev"], "t": t, **event}
    log["events"].append(ev)
    return ev


def reserve_key(log: dict[str, Any], t: float) -> str:
    """The next channel key: one more than any key ever reserved or seen."""
    log["high_key"] += 1
    key = str(log["high_key"])
    _append(log, t, {"type": "reserve", "key": key})
    return key


def note_key(log: dict[str, Any], key: str) -> None:
    """A key that exists elsewhere (imported, or reserved before a lost write) is never reused."""
    if key.isdigit():
        log["high_key"] = max(log["high_key"], int(key))


class View:
    """The log folded up: channels with their history, settings over time, presses."""

    def __init__(self, log: dict[str, Any]) -> None:
        self.rev: int = log["rev"]
        self.channels: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, dict[str, list[tuple[float, Any]]]] = {}
        self.presses: list[dict[str, Any]] = []
        for ev in log["events"]:
            self._apply(ev)

    def _apply(self, ev: dict[str, Any]) -> None:
        kind = ev["type"]
        key = ev.get("key")
        t = ev["t"]
        if kind == "activate" and key not in self.channels:  # a key activates once, ever
            ch = {f: ev["channel"].get(f) for f in CHANNEL_FIELDS}
            ch.update(
                key=key, active=True, since=t, until=None, aliases=[], changes=[], bindings=[]
            )
            self.channels[key] = ch
            self._bind(ch, t)
        elif kind == "update" and key in self.channels:
            ch = self.channels[key]
            ch["changes"].append({"t": t, **ev["changes"]})
            for f in MUTABLE_FIELDS:
                if f in ev["changes"]:
                    ch[f] = ev["changes"][f]
            self._bind(ch, t)
        elif kind == "alias" and key in self.channels:
            ch = self.channels[key]
            ch["aliases"].append({"entity_id": ch["entity_id"], "until": t})
            ch["entity_id"] = ev["entity_id"]
            self._bind(ch, t)
        elif kind == "retire" and key in self.channels:
            ch = self.channels[key]
            ch["active"] = False
            ch["until"] = t
        elif kind == "setting":
            self.settings.setdefault(key, {}).setdefault(ev["field"], []).append((t, ev["value"]))
        elif kind == "press":
            self.presses.append({"t": t, "what": ev["what"], "id": ev["id"]})

    @staticmethod
    def _bind(ch: dict[str, Any], t: float) -> None:
        """How the channel reads its source from t: which entity, through which connection
        entity and decoder, in which cooker. A change replaces the binding from its time on."""
        b = {f: ch.get(f) for f in BINDING_FIELDS}
        if ch["bindings"] and ch["bindings"][-1]["t"] == t:
            ch["bindings"][-1].update(b)
        elif not ch["bindings"] or any(ch["bindings"][-1][f] != b[f] for f in BINDING_FIELDS):
            ch["bindings"].append({"t": t, **b})

    def active(self) -> dict[str, dict[str, Any]]:
        return {k: c for k, c in self.channels.items() if c["active"]}

    def bindings(self, key: str) -> list[tuple[float, float | None, dict[str, Any]]]:
        """(from, to, binding) for each stretch of the channel's life, to = None while it runs."""
        ch = self.channels[key]
        out = []
        bs = ch["bindings"]
        for i, b in enumerate(bs):
            to = bs[i + 1]["t"] if i + 1 < len(bs) else ch["until"]
            out.append((b["t"], to, {f: b[f] for f in BINDING_FIELDS}))
        return out

    def binding_at(self, key: str, t: float) -> dict[str, Any] | None:
        for start, to, b in self.bindings(key):
            if start <= t and (to is None or t < to):
                return b
        return None

    def setting_at(self, key: str, field: str, t: float, default: Any = None) -> Any:
        """The value of a setting in force at t (the last change at or before t)."""
        series = self.settings.get(key, {}).get(field)
        if not series:
            return default
        i = bisect_right([x[0] for x in series], t)
        return series[i - 1][1] if i else default

    def setting_now(self, key: str, field: str, default: Any = None) -> Any:
        series = self.settings.get(key, {}).get(field)
        return series[-1][1] if series else default

    def last_press(self, what: str, before: float | None = None) -> dict[str, Any] | None:
        for p in reversed(self.presses):
            if p["what"] == what and (before is None or p["t"] <= before):
                return p
        return None

    def entity_ids(self, key: str) -> list[str]:
        """Every entity_id the channel's source has had, the current one last."""
        ch = self.channels[key]
        return [a["entity_id"] for a in ch["aliases"]] + [ch["entity_id"]]


def _identity(ch: dict[str, Any]) -> tuple[Any, Any]:
    return (ch.get("source_uid") or ch.get("entity_id"), ch.get("role"))


def diff_channels(
    applied: dict[str, dict[str, Any]],
    desired: dict[str, dict[str, Any]],
    known: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The events that take the applied channels to the desired ones (keyed by channel key),
    and the desired keys that were refused.

    A key names one source in one role for good. A desired channel whose key the log already
    knows (active or retired, `known`) with another source or role is inconsistent: it is
    refused, never turned into a retire and an activate under the same key. So is
    reactivating a retired key.
    """
    known = applied if known is None else known
    out: list[dict[str, Any]] = []
    refused: list[str] = []
    for key in applied:
        if key not in desired:
            out.append({"type": "retire", "key": key})
    for key, want in desired.items():
        ch = applied.get(key)
        if ch is None:
            if key in known:
                refused.append(key)  # a retired key, or one with another identity
                continue
            out.append(
                {
                    "type": "activate",
                    "key": key,
                    "channel": {f: want.get(f) for f in CHANNEL_FIELDS},
                }
            )
            continue
        if _identity(want) != _identity(ch):
            refused.append(key)
            continue
        if want["entity_id"] != ch["entity_id"]:
            out.append({"type": "alias", "key": key, "entity_id": want["entity_id"]})
        changes = {f: want.get(f) for f in MUTABLE_FIELDS if want.get(f) != ch.get(f)}
        if changes:
            out.append({"type": "update", "key": key, "changes": changes})
    return out, refused


def apply_events(
    log: dict[str, Any], t: float, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append events at t, keeping the key high-water mark; returns the appended copies."""
    out = []
    for ev in events:
        if "key" in ev:
            note_key(log, ev["key"])
        out.append(_append(log, t, copy.deepcopy(ev)))
    return out
