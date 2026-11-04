"""Delivered alerts, kept apart from the estimator's state so a rebuilt state does not resend them.

The estimator decides when an alert is due from latches in its state, and that state can be
rebuilt: a replay after a restart, or a code change that discards a saved state. A rebuilt
latch can come out unset for an alert already delivered (a heads-up fired on a transient ETA
between two replay grid points), and the alert would go out again.

Two tables:
  occ    each alert occurrence by its own id: what it was, when, and whether it is pending
         (about to be sent) or sent.
  latch  latch key -> the occurrence that set it. A latch key is channel, event, probe
         session and threshold (the target for approach and done, the second temperature
         for alert_soon and alert, the stall's onset for stall and stall_end). A latch key
         held here is a delivered alert whose latch has not re-armed: after any rebuild the
         runtime sets those latches in the new state, silently.

A re-arm (the ETA moved well past the lead, the temperature fell back) releases the latch key
if the occurrence holding it is not newer than the re-arm, so a re-arm found while replaying
older history never releases a later delivery. A second crossing after a re-arm is a new
occurrence with its own id, so finishing the first send cannot mark the second one sent.

Delivery is an outbox: an occurrence is written pending before the send and sent after,
both to disk. One that reached no target, or not all of them, stays pending for the targets
it missed, and is retried while it is younger than RESEND_S and the runtime still finds it
current (the channel active, the same probe session); older ones are dropped. After a
restart the same rule applies. Duplicates are possible (each crash between a send and its
write repeats it, within RESEND_S); a fresh alert is not silently lost. The companion app
replaces a notification with the same tag, but the phone may still sound twice. Accepted
gap: a crash between a re-arm and its write, followed by the second crossing before the
next re-arm, suppresses that second alert.
"""

from __future__ import annotations

from typing import Any, Final

SCHEMA: Final = 2
RESEND_S: Final = 600


def latch_key(key: str, event: str, session: str | None, threshold: Any) -> str:
    return "|".join(str(x) for x in (key, event, session, threshold))


def new_ledger() -> dict[str, Any]:
    return {"schema": SCHEMA, "seq": 0, "occ": {}, "latch": {}}


def holds(ledger: dict[str, Any], lk: str) -> bool:
    """A delivered (or pending) alert holds this latch: do not send it again."""
    return lk in ledger["latch"]


def note_pending(ledger: dict[str, Any], lk: str, t: float, payload: dict[str, Any]) -> str:
    """Before the send: a new occurrence, holding its latch, with what a resend needs."""
    ledger["seq"] += 1
    occ_id = str(ledger["seq"])
    ledger["occ"][occ_id] = {"latch": lk, "t": t, "state": "pending", "payload": payload}
    ledger["latch"][lk] = occ_id
    return occ_id


def note_sent(ledger: dict[str, Any], occ_id: str) -> None:
    """After the send of this occurrence, and only this one."""
    occ = ledger["occ"].get(occ_id)
    if occ is None:
        return
    occ["state"] = "sent"
    occ.pop("payload", None)
    _collect(ledger)


def note_failed(ledger: dict[str, Any], occ_id: str, targets: list[str]) -> None:
    """Some targets could not be reached: the occurrence stays pending for those only, so a
    retry does not repeat it to the ones that got it."""
    occ = ledger["occ"].get(occ_id)
    if occ is not None and occ["state"] == "pending":
        occ["payload"]["only"] = list(targets)


def note_rearmed(ledger: dict[str, Any], lk: str, t: float) -> bool:
    """The latch re-armed at t: it may fire again. True if the ledger changed."""
    occ_id = ledger["latch"].get(lk)
    if occ_id is None or ledger["occ"].get(occ_id, {}).get("t", -1) > t:
        return False
    del ledger["latch"][lk]
    # an occurrence of this latch still waiting to go out is void: what it announced is no
    # longer so, and a later crossing is an occurrence of its own
    for occ in ledger["occ"].values():
        if occ["latch"] == lk and occ["state"] == "pending" and occ["t"] <= t:
            occ["state"] = "sent"
            occ.pop("payload", None)
    _collect(ledger)
    return True


def release(ledger: dict[str, Any], keep: Any) -> bool:
    """Drop latches no probe can fire again (`keep(latch_key)` false: a retired channel, a
    session that has ended). True if the ledger changed."""
    gone = [lk for lk in ledger["latch"] if not keep(lk)]
    for lk in gone:
        del ledger["latch"][lk]
    if gone:
        _collect(ledger)
    return bool(gone)


def held(ledger: dict[str, Any], key: str) -> list[str]:
    """The latch keys one channel holds."""
    return [lk for lk in ledger["latch"] if lk.split("|", 1)[0] == key]


def pending(
    ledger: dict[str, Any], now: float
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """At startup: ([(occurrence id, payload)] to send again, [occurrence ids] too old to send)."""
    fresh, stale = [], []
    for occ_id, occ in ledger["occ"].items():
        if occ["state"] == "pending":
            if now - occ["t"] < RESEND_S:
                fresh.append((occ_id, occ["payload"]))
            else:
                stale.append(occ_id)
    return fresh, stale


def drop(ledger: dict[str, Any], occ_id: str) -> None:
    """A pending occurrence given up on: it keeps its latch (it may have been delivered)."""
    occ = ledger["occ"].get(occ_id)
    if occ is not None:
        occ["state"] = "sent"
        occ.pop("payload", None)
        _collect(ledger)


def _collect(ledger: dict[str, Any]) -> None:
    """Sent occurrences no latch refers to any more are history; pending ones are kept."""
    live = set(ledger["latch"].values())
    for occ_id in [
        o for o, occ in ledger["occ"].items() if occ["state"] == "sent" and o not in live
    ]:
        del ledger["occ"][occ_id]
