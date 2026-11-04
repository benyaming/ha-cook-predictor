"""A channel's readings over recorded history, with the binding in force at each moment.

The recorder has each entity's states; a channel's readings come from whichever source entity
it had at the time (an entity_id rename is an alias from its time on), normalised with the
connection entity and decoder of that time. Everything that turns history into readings goes
through here, for the estimator's rebuild, the chart's series and the journal's reconcile alike.

Two rules keep a reading honest:
  - each stretch of a binding starts with the state in force at its start (the last row at or
    before it), read once at that time; older rows of the same entity are not re-dated to it;
  - rows at the same instant (the source and its connection entity often change together)
    are applied together and read once.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .normalize import Reading, normalize

# (time, state, unit): one recorded state of an entity, unit from its attributes
type Row = tuple[float, str | None, str | None]


def _in_force(rows: Sequence[Row], t: float) -> Row | None:
    last = None
    for r in rows:
        if r[0] <= t:
            last = r
        else:
            break
    return last


def channel_readings(
    bindings: Sequence[tuple[float, float | None, Mapping[str, Any]]],
    rows: Mapping[str, Sequence[Row]],
    start: float,
    end: float = math.inf,
) -> list[tuple[float, Reading]]:
    """Readings over [start, end] for a channel bound as `bindings` says
    ([(from, to, {entity_id, connection, decoder})], from the control log), given each
    entity's rows sorted by time (`rows`, the recorder's states, including the one in force
    before `start` if there is one)."""
    out: list[tuple[float, Reading]] = []
    for b_from, b_to, b in bindings:
        lo = max(b_from, start)
        hi = min(b_to if b_to is not None else math.inf, end)
        if lo > hi or (b_to is not None and lo >= b_to):
            continue
        src = rows.get(b["entity_id"], ())
        conn_id = b.get("connection")
        con = rows.get(conn_id, ()) if conn_id else ()
        source = _in_force(src, lo)
        conn = _in_force(con, lo)
        conn_state = (conn[1] if conn else "unknown") if conn_id else None
        if source is not None:
            out.append(
                (lo, normalize(source[1], source[2], b.get("decoder") or "generic", conn_state))
            )
        # every change after the stretch's start, one reading per instant; a row at the end
        # of the window belongs to it, a row at the end of the binding to the next binding
        end_in_window = b_to is None or end < b_to
        events: dict[float, list[tuple[int, Row]]] = {}
        for which, series in ((0, src), (1, con)):
            for r in series:
                if lo < r[0] < hi or (r[0] == hi and end_in_window):
                    events.setdefault(r[0], []).append((which, r))
        for t in sorted(events):
            for which, r in events[t]:
                if which == 0:
                    source = r
                else:
                    conn_state = r[1]
            if source is not None:
                out.append(
                    (t, normalize(source[1], source[2], b.get("decoder") or "generic", conn_state))
                )
    return out
