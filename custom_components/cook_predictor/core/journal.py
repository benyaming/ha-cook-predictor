"""Cook journal: records each cook as Home Assistant published it, from its own stream of
state changes, so past cooks stay browsable after the recorder has purged them.

Pure logic, no I/O: journal_manager.py feeds it state changes (live, or from the recorder
after a restart, merged into one time order) and persists what it returns. Times are epoch
seconds; temperatures are stored in hundredths of a degree (the Inkbird's 0.1 F step is
0.056 C), so a crossing of a target is never manufactured by rounding.

Its inputs are channels, by stable string key: internal ones (food probes) and ambient ones
(a pit or an oven, several possible), whose states arrive normalised: a number of degrees
C, "disconnected" (the probe is out), or anything else (unknown: a gap). Membership rows
say which channels are active, with their names and, for an internal, the pit it is in.

A cook opens on a New cook press (explicit), or on a valid reading of an active internal
while none is open (provisional: never written until a probe has risen heat_evidence_c above
its lowest reading, and it then starts where that probe began to heat). It closes on New
cook (which opens the next), Finish cook, every participating internal disconnected for
unplug_close_s, no valid reading from any of them for off_close_s (device switched off), or
its last participating internal leaving the configuration with no replacement within
unplug_close_s. A probe that is unknown is otherwise a gap, never a disconnection. After
Finish no cook starts by itself until New cook or a probe is disconnected and comes back.

This began as a faithful port of the Node-RED journal (cook/journal.js), whose cooks are
schema 1: one pit, probes numbered 1 to 3. They stay readable, and summarize() and derive()
give both schemas one shape. Where Python and JavaScript disagree (rounding, number parsing
and printing, date parsing, key order, undefined against null) the code follows
JavaScript, so a record reads and writes the same in both; those rules live in the
"JavaScript semantics" section below.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

type Cook = dict[str, Any]
type Journal = dict[str, Any]
type Event = dict[str, Any]
type Kinds = dict[str, list[str]]

SCHEMA: Final = 2
CFG: Final = {
    "unplug_close_s": 1800,
    "off_close_s": 6 * 3600,
    "heat_evidence_c": 5,
    "heat_start_c": 1,  # the cook starts at the last reading within this of the lowest
    # a provisional cook, or an explicit one with no meat reading, is dropped after this
    "provisional_s": 2 * 3600,
    "watchdog_s": 36 * 3600,  # flagged, never closed
    "pred_every_s": 600,  # a published prediction is stored at least this often...
    "pred_move_s": 120,  # ...or when done_at moves by this much
    "spark_points": 60,
}


# ---- JavaScript semantics ----
#
# The journal's records and arithmetic are JavaScript's. These helpers reproduce the parts
# of the language the journal relies on, so the same inputs give the same records, byte for
# byte once serialised.


class _Undefined:
    """JavaScript's undefined: a key that is absent, as opposed to one holding null."""

    _instance: _Undefined | None = None

    def __new__(cls) -> _Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED: Final = _Undefined()

_INDEX_KEY = re.compile(r"0|[1-9][0-9]*")
_MAX_INDEX = 2**32 - 2


def _index_of(key: str) -> int | None:
    """The array index a key spells ('0', '7', '42', canonical and below 2**32 - 1), or None."""
    if _INDEX_KEY.fullmatch(key):
        value = int(key)
        if value <= _MAX_INDEX:
            return value
    return None


def js_keys(obj: Mapping[str, Any]) -> list[str]:
    """The keys of a dict in the order JavaScript enumerates an object's: integer-like keys
    first, ascending, then the others in insertion order. A probe numbered 2 that joined
    before probe 1 still comes second, as it does in a JavaScript object and in its JSON.
    """
    keys = list(obj)
    indexed = [k for k in keys if _index_of(k) is not None]
    if not indexed:
        return keys
    indexed.sort(key=_index_of)
    return indexed + [k for k in keys if _index_of(k) is None]


def _truthy(value: Any) -> bool:
    """JavaScript truthiness: an empty dict or list is true, NaN is false."""
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, int | float):
        return value == value and value != 0  # noqa: PLR0124 - NaN is not equal to itself
    if isinstance(value, str):
        return value != ""
    return True


def _strict_equal(a: Any, b: Any) -> bool:
    """JavaScript ===: numbers by value, strings by content, null and undefined apart,
    booleans never equal to numbers, objects by identity.
    """
    if a is UNDEFINED or b is UNDEFINED or a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is b


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    """Number.isFinite: a finite number, never a string or a boolean."""
    return _is_number(value) and math.isfinite(value)


def _to_number(value: Any) -> float:
    """The numeric value of what the journal compares with < or Math.max: null counts as 0
    (a -Infinity that went through JSON comes back null), undefined as NaN.
    """
    if value is None:
        return 0
    if value is UNDEFINED:
        return math.nan
    return value


def js_round(x: float) -> int | float:
    """Math.round: to the nearest integer, a half always toward +infinity (-2.5 -> -2)."""
    if not math.isfinite(x):
        return x
    low = math.floor(x)
    return low + 1 if x - low >= 0.5 else low


# StrWhiteSpaceChar: what parseFloat and Number skip around a number.
_JS_SPACE = "\t\n\v\f\r \xa0" + "".join(
    chr(code)
    for code in (0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF)
)
_DECIMAL = r"[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
_FLOAT_PREFIX = re.compile(_DECIMAL)
_NON_DECIMAL = re.compile(r"0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")


def _decimal_value(text: str) -> float:
    if text.endswith("Infinity"):
        return -math.inf if text.startswith("-") else math.inf
    return float(text)


def _js_string(value: Any) -> str:
    """String(value) for the values a state or an attribute can hold."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if value is UNDEFINED:
        return "undefined"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int | float):
        return number_text(value)
    if isinstance(value, list | tuple):
        return ",".join("" if x is None or x is UNDEFINED else _js_string(x) for x in value)
    return "[object Object]"


def parse_float(value: Any) -> float:
    """parseFloat: the longest decimal prefix after leading white space ('12.5abc' -> 12.5,
    '1e' -> 1, ' .5' -> 0.5), NaN when there is none. 'inf', 'nan' and '1_0' are not numbers
    here, although Python's float() takes them.
    """
    if _is_number(value):
        return float(value) + 0.0  # String(-0) is '0'
    match = _FLOAT_PREFIX.match(_js_string(value).lstrip(_JS_SPACE))
    return _decimal_value(match.group()) if match else math.nan


def _string_to_number(text: str) -> float:
    """Number(text) for a string: the whole string, trimmed, must be a number ('' is 0)."""
    text = text.strip(_JS_SPACE)
    if not text:
        return 0.0
    if re.fullmatch(_DECIMAL, text):
        return _decimal_value(text)
    if _NON_DECIMAL.fullmatch(text):
        return float(int(text, 0))
    return math.nan


def _key_number(key: str) -> int | float:
    """Number(n) for a probe key: 1 for '1'."""
    value = _string_to_number(key)
    return int(value) if value.is_integer() else value


_ISO_DATE = re.compile(
    r"(?P<year>[+-][0-9]{6}|[0-9]{4})(?:-(?P<month>[0-9]{2})(?:-(?P<day>[0-9]{2}))?)?"
    r"(?:(?P<sep>[Tt ])(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2})(?:\.(?P<fraction>[0-9]+))?)?)?"
    r"(?P<zone>[Zz]|[+-][0-9]{2}:?[0-9]{2})?"
)
_MAX_TIME_MS = 8.64e15


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days from 1970-01-01 to a proleptic Gregorian date; a day past the month's end runs
    into the next month, as MakeDay does (Date.parse('2026-02-30') is March 2).
    """
    y = year - (month <= 2)
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468 + day - 1


def date_parse(text: Any) -> int | None:
    """Date.parse for ISO 8601 date-times, in milliseconds, or None (NaN).

    Takes what V8 takes in that family: YYYY[-MM[-DD]] (UTC), then a time HH:mm[:ss[.f+]]
    after T, t or a space, with Z, z, +HH:mm or +HHmm, or local time when there is no zone.
    Fractions are truncated to milliseconds (HA's '...19.953190+00:00' is 19.953), 24:00 is
    the next midnight, and a day past the end of its month runs into the next one. V8's
    fallback parser also reads looser forms ('Oct 11 2026', '2026/10/11', '5'); those are not
    reproduced and give None.
    """
    if not isinstance(text, str):
        return None
    match = _ISO_DATE.fullmatch(text)
    if match is None:
        return None
    part = match.groupdict()
    if part["year"] == "-000000":
        return None
    year = int(part["year"])
    month = int(part["month"] or 1)
    day = int(part["day"] or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    hour = int(part["hour"] or 0)
    minute = int(part["minute"] or 0)
    second = int(part["second"] or 0)
    ms = int((part["fraction"] or "0")[:3].ljust(3, "0"))
    if minute > 59 or second > 59 or hour > 24 or (hour == 24 and (minute or second or ms)):
        return None
    zone = part["zone"]
    utc = zone in ("Z", "z")
    if zone and part["hour"] is None and not utc:
        return None  # an offset needs a time: '2026-10-11+03:00' is not a date
    local_ms = (
        _days_from_civil(year, month, day) * 86_400_000
        + ((hour * 60 + minute) * 60 + second) * 1000
        + ms
    )
    if zone is None and part["hour"] is not None:
        # a date-time without a zone is local time, a date alone is UTC
        try:
            naive = datetime(1970, 1, 1) + timedelta(milliseconds=local_ms)
            result = round(naive.timestamp() * 1000)
        except OverflowError, OSError, ValueError:
            return None
    elif zone is None or utc:
        result = local_ms
    else:
        digits = zone[1:].replace(":", "")
        offset_h, offset_m = int(digits[:2]), int(digits[2:])
        if offset_h > 23 or offset_m > 59:
            return None
        offset = (offset_h * 60 + offset_m) * 60_000
        result = local_ms - offset if zone[0] == "+" else local_ms + offset
    return result if abs(result) <= _MAX_TIME_MS else None


def iso_seconds(text: Any) -> float | None:
    """The epoch seconds an ISO time string names, or None."""
    t = date_parse(text)
    return None if t is None else t / 1000


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def _iso_from_ms(ms: int) -> str:
    if abs(ms) > _MAX_TIME_MS:
        raise ValueError("Invalid time value")
    d = _EPOCH + timedelta(milliseconds=ms)
    return (
        f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:"
        f"{d.second:02d}.{d.microsecond // 1000:03d}Z"
    )


def iso(t: float) -> str:
    """new Date(t * 1000).toISOString(): UTC with milliseconds (truncated) and a Z."""
    if not math.isfinite(t):
        raise ValueError("Invalid time value")
    return _iso_from_ms(math.trunc(t * 1000))


def number_text(x: float) -> str:
    """How JavaScript prints a number (Number.prototype.toString, and so JSON.stringify):
    integers without '.0', fixed notation from 1e-6 up to 1e21, otherwise 1e-7 or 1.5e+21.
    The digits are the shortest that read back as the same double, as Python's repr's are.
    """
    if isinstance(x, int) and not isinstance(x, bool):
        if abs(x) < 2**53:
            return str(x)
        x = float(x)
    if x != x:  # noqa: PLR0124 - NaN
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    if x.is_integer():
        if abs(x) < 2**53:
            return str(int(x))
    elif 1e-4 <= abs(x) < 1e16:
        return repr(x)  # Python and JavaScript agree in this range, but for integers
    mantissa, _, exponent = repr(abs(x)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    raw = whole + fraction
    digits = raw.lstrip("0")
    point = len(whole) + int(exponent or 0) - (len(raw) - len(digits))
    digits = digits.rstrip("0")
    size = len(digits)
    sign = "-" if x < 0 else ""
    if size <= point <= 21:
        return sign + digits + "0" * (point - size)
    if 0 < point <= 21:
        return f"{sign}{digits[:point]}.{digits[point:]}"
    if -6 < point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    e = point - 1
    head = digits[0] + (f".{digits[1:]}" if size > 1 else "")
    return f"{sign}{head}e{'+' if e >= 0 else '-'}{abs(e)}"


_quote: Callable[[str], str] = json.encoder.encode_basestring
_SURROGATE = re.compile("[\ud800-\udfff]")


def _string_json(s: str) -> str:
    text = _quote(s)
    if _SURROGATE.search(text):
        # a lone surrogate (a name cut inside an emoji): escaped, as JSON.stringify does
        text = _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
    return text


def to_json(value: Any) -> str:
    """JSON.stringify(value): compact, keys in JavaScript's order, numbers as JavaScript
    prints them, NaN and +-Infinity as null, an UNDEFINED value leaves its key out (null in
    a list), non-ASCII text as is.
    """
    parts: list[str] = []
    emit = parts.append

    def write(v: Any) -> None:
        if v is None or v is UNDEFINED:
            emit("null")
        elif v is True:
            emit("true")
        elif v is False:
            emit("false")
        elif isinstance(v, str):
            emit(_string_json(v))
        elif isinstance(v, int | float):
            emit(number_text(v) if math.isfinite(v) else "null")
        elif isinstance(v, dict):
            emit("{")
            first = True
            for key in js_keys(v):
                item = v[key]
                if item is UNDEFINED:
                    continue
                if not first:
                    emit(",")
                first = False
                emit(_string_json(key))
                emit(":")
                write(item)
            emit("}")
        elif isinstance(v, list | tuple):
            emit("[")
            for i, item in enumerate(v):
                if i:
                    emit(",")
                write(item)
            emit("]")
        else:
            raise TypeError(f"not JSON: {type(v).__name__}")

    write(value)
    return "".join(parts)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def from_json(text: str) -> Any:
    """JSON.parse(text): strict JSON, so NaN and Infinity are an error, as in JavaScript."""
    return json.loads(text, parse_constant=_reject_constant)


def clone(value: Any) -> Any:
    """JSON.parse(JSON.stringify(value)): a deep copy as it would be after a save and load
    (a -Infinity becomes null, an integral float an int).
    """
    return from_json(to_json(value))


# ---- the journal ----

INTERNAL: Final = "internal"
AMBIENT: Final = "ambient"
DISCONNECTED: Final = "disconnected"
CONNECTED: Final = "connected"
# The Node-RED journal's one pit, as a channel: the key schema-1 records migrate to.
LEGACY_PIT: Final = "4"


def kinds(ids: Mapping[str, Any]) -> Kinds:
    """Entity id -> kind, from ids = {probes: {key: id}, pits: {key: id}, predictors:
    {key: id}, targets: {key: id}, reset, finish, members}, keyed by channel (keys become
    strings): ['probe', key], ['pit', key], ['predictor', key], ['target', key],
    ['press', 'new_cook'], ['press', 'finish'], ['members'].
    """
    kind: Kinds = {}
    groups = (("probes", "probe"), ("pits", "pit"), ("predictors", "predictor"))
    for group, name in (*groups, ("targets", "target")):
        keyed = {str(key): entity_id for key, entity_id in (ids.get(group) or {}).items()}
        for key in js_keys(keyed):
            kind[keyed[key]] = [name, key]
    kind[ids["reset"]] = ["press", "new_cook"]
    kind[ids["finish"]] = ["press", "finish"]
    if ids.get("members") is not None:
        kind[ids["members"]] = ["members"]
    return kind


def new_journal(since: float | None = None) -> Journal:
    """The persisted journal state (state.json holds exactly this).

    last:    newest observation per entity {t, v}: recorder rows that overlap live ones are
             dropped by time, and a cook opening later knows what is in force
    presses: newest press time per button; since: press times before this are history
    now:     the recovery cursor, the newest time observed
    inhibit: set by Finish; no cook starts by itself until New cook or a probe reconnects
    members: the membership row last applied, {t, active, rev}; None until the first one,
             and until then every channel in the ids counts as active
    """
    start = -math.inf if since is None else since
    return {
        "schema": SCHEMA,
        "last": {},
        "presses": {},
        "since": start,
        "now": start,
        "open": None,
        "inhibit": False,
        "members": None,
    }


def num(value: Any) -> float | None:
    """parseFloat, or None for anything that is not a finite number."""
    v = parse_float(value)
    return v if math.isfinite(v) else None


def _reading(state: Any) -> tuple[str, float | None]:
    """A normalised channel state: a number of degrees C is a reading (a 520 C pizza oven
    too), "disconnected" is a probe that is out, "connected" a probe known to be in whose
    temperature is unknown, anything else is unknown: a gap.
    """
    if state == DISCONNECTED:
        return ("unplugged", None)
    if state == CONNECTED:
        return ("connected", None)
    v = num(state)
    if v is None or v <= -40:
        return ("off", None)
    return ("valid", v)


def _c100(v: float | None) -> Any:
    return None if v is None else js_round(v * 100)


def cook_id(start: float) -> str:
    """A cook's id: its start, to the second, in UTC (20261011T011920Z)."""
    text = _iso_from_ms(int(js_round(start)) * 1000)
    return re.sub(r"\.[0-9]+Z\Z", "Z", text).replace("-", "").replace(":", "")


def _key_of(kind: list[str]) -> Any:
    return kind[1] if len(kind) > 1 else None


# ---- membership ----


def _entry_for(active: Mapping[str, Any] | None, key: str, role: str) -> dict[str, Any] | None:
    """The channel's entry in a membership's active set if it is active in that role."""
    if active is None:
        return None
    entry = active.get(key)
    return entry if isinstance(entry, dict) and entry.get("role") == role else None


def _was_active(members: Mapping[str, Any] | None, key: str, role: str) -> bool:
    if members is None:
        return True  # before the first membership row every channel counted
    return _entry_for(members["active"], key, role) is not None


def _active_entry(js: Journal, key: str, role: str) -> dict[str, Any] | None:
    """The channel's membership entry if it is active in `role` now; before the first
    membership row, a default one (every channel in the ids counts).
    """
    members = js.get("members")
    if members is None:
        name = f"Probe {key}" if role == INTERNAL else f"Pit {key}"
        return {"name": name, "role": role, "ambient": None}
    return _entry_for(members["active"], key, role)


def _channel(entry: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {"name": entry.get("name"), "role": role, "ambient": entry.get("ambient")}


def _participants(c: Cook) -> list[str]:
    """The internals taking part: joined (a reading) or waiting for their first reading, and
    not retired. Only these count for the closes that end a cook by itself.
    """
    retired = c.get("retired") or {}
    keys = js_keys({**c["probes"], **(c.get("waiting") or {})})
    return [key for key in keys if key not in retired]


def _joined_at(c: Cook, key: str) -> int:
    p = c["probes"].get(key)
    if p is not None and p["series"]:
        return p["series"][0][0]
    return c["waiting"].get(key, 0)


def _retire(c: Cook, key: str, rel: int) -> None:
    """The internal left the configuration: its statistics stop here (a null point), and
    whatever it reports later is not this cook's.
    """
    c["retired"][key] = rel
    p = c["probes"].get(key)
    if p is not None:
        _add_point(p["series"], rel, None)
    c["waiting"].pop(key, None)


def _apply_members(js: Journal, t: float, attrs: Mapping[str, Any] | None) -> None:
    """A membership row: {"active": {key: {name, role, ambient}}, "rev"}. Kept as the
    journal's current membership and applied to the open cook at its time.
    """
    if not isinstance(attrs, Mapping) or not isinstance(attrs.get("active"), Mapping):
        return  # not a membership row
    active = clone(dict(attrs["active"]))
    rev = attrs.get("rev")
    before = js.get("members")
    js["members"] = {"t": t, "active": active, "rev": rev}
    c = js["open"]
    if c is None:
        return
    rel = js_round(t - c["start"])
    # names, and an internal's pit, as of this row, for the channels already in the cook
    for key in js_keys(c["channels"]):
        channel = c["channels"][key]
        entry = _entry_for(active, key, channel.get("role"))
        if entry is None:
            continue
        channel["name"] = entry.get("name")
        pit = entry.get("ambient")
        if channel.get("role") == INTERNAL and not _strict_equal(pit, channel.get("ambient")):
            changes = c["ambients"].get(key)
            if changes is None:
                changes = c["ambients"][key] = [[_joined_at(c, key), channel.get("ambient")]]
            _add_point(changes, rel, pit)
            channel["ambient"] = pit
    taking_part = bool(_participants(c))
    for key in _participants(c):
        if _entry_for(active, key, INTERNAL) is None:
            _retire(c, key, rel)
    for key in js_keys(c["pits"]):
        if _entry_for(active, key, AMBIENT) is None:
            _add_point(c["pits"][key], rel, None)  # a pit that left: its statistics stop
    last_left = taking_part and not _participants(c) and not c["provisional"]
    if last_left and c.get("pending_close") is None:
        # the last one left: the cook closes here unless a replacement comes in time
        c["pending_close"] = {"at": t, "deadline": t + CFG["unplug_close_s"], "rev": rev}
    for key in js_keys(active):
        entry = _entry_for(active, key, INTERNAL)
        if entry is None or _was_active(before, key, INTERNAL):
            continue
        # activated: a retired one comes back (its gap stays in its series); while the cook
        # waits to close, one that has not read yet joins as a participant waiting for its
        # first reading, so a probe still unavailable keeps the cook
        c["retired"].pop(key, None)
        pending = c.get("pending_close")
        if pending is not None and t <= pending["deadline"]:
            if c["probes"].get(key) is None:
                c["waiting"][key] = rel
            c["channels"].setdefault(key, _channel(entry, INTERNAL))
            c["pending_close"] = None


# ---- cooks ----


def _new_cook(js: Journal, kind: Kinds, start: float, explicit: bool) -> Cook:
    c: Cook = {
        "schema": SCHEMA,
        "id": cook_id(start),
        "start": start,
        "end": None,
        "status": "open",
        "provisional": not explicit,
        "started_by": "new_cook" if explicit else "probe",
        "ended_by": None,
        "watchdog": False,
        "name": "",
        "notes": "",
        "probes": {},
        "pits": {},
        "targets": {},
        "predictions": {},
        "fills": [],
        "channels": {},
        "ambients": {},
        "lastObs": start,
        "lastValidAt": None,
        "unplugged": {},
        "off": {},
        "retired": {},
        "waiting": {},
        "pending_close": None,
    }
    for entity_id in js_keys(kind):
        k, key = kind[entity_id][0], _key_of(kind[entity_id])
        last = js["last"].get(entity_id)
        if not last:
            continue
        if k == "probe":
            entry = _active_entry(js, key, INTERNAL)
            r = _reading(last.get("v"))
            if entry is not None and r[0] == "valid":
                _join(c, key, start, r[1], entry)
        elif k == "pit":
            entry = _active_entry(js, key, AMBIENT)
            if entry is not None:
                c["pits"][key] = [[0, _c100(_reading(last.get("v"))[1])]]
                c["channels"][key] = _channel(entry, AMBIENT)
        elif k == "target" and num(last.get("v")) is not None:
            c["targets"][key] = [[0, num(last.get("v"))]]
    return c


def _join(c: Cook, key: str, t: float, v: Any, entry: Mapping[str, Any]) -> None:
    """first: the first reading; min: the lowest so far. A probe usually reads the air before
    it goes into cold meat, so heating is measured from the lowest reading, not the first. A
    probe that was waiting starts with a null point where it joined: the stretch before its
    first reading is a gap, not a reading.
    """
    rel = max(0, js_round(t - c["start"]))
    series = [[rel, _c100(v)]]
    waited = c["waiting"].pop(key, None)
    if waited is not None and waited < rel:
        series.insert(0, [waited, None])
    c["probes"][key] = {"first": v, "min": v, "series": series}
    c["lastValidAt"] = max(t if c["lastValidAt"] is None else c["lastValidAt"], t)
    c["channels"].setdefault(key, _channel(entry, INTERNAL))
    c["pending_close"] = None  # a participant again


def _lowest(p: dict[str, Any]) -> float:
    """A probe's lowest reading so far, rebuilt from its series for a probe saved before min
    existed.
    """
    if p.get("min") is not None:
        return p["min"]
    low = p["first"]
    for point in p["series"]:
        if point[1] is not None:
            low = min(low, point[1] / 100)
    return low


def _add_point(series: list[list[Any]], rel: int, v: Any) -> None:
    last = series[-1] if series else None
    if last is not None and _strict_equal(last[1], v):
        return
    if last is not None and _strict_equal(last[0], rel):
        last[1] = v  # same second: keep the newest
    else:
        series.append([rel, v])


def _close(js: Journal, end: float, by: str) -> Event:
    """Close (or drop) the open cook. A provisional cook, or one with no meat reading, was
    never a cook. A button pressed after every participating probe had gone quiet ends the
    cook at its last valid reading, not hours later at the press.
    """
    c = js["open"]
    js["open"] = None
    meat = js_keys(c["probes"])
    if c["provisional"] or not meat:
        return {"type": "drop", "cook": c}
    quiet = all(
        c["unplugged"].get(n) is not None or c["off"].get(n) is not None for n in _participants(c)
    )
    pressed = by in ("new_cook", "finish")
    if pressed:
        c["press_at"] = end  # the press itself; the end below may be earlier
    if pressed and quiet and c["lastValidAt"] is not None:
        c["end"] = min(end, max(c["lastValidAt"], c["start"]))
    else:
        c["end"] = end
    c["status"] = "closed"
    c["ended_by"] = by
    c["pending_close"] = None
    _clip(c, js_round(c["end"] - c["start"]))
    return {"type": "close", "cook": c}


def _clip(c: Cook, end_rel: float) -> None:
    """Nothing after the end: a close dated back (to the last unplug, the last valid reading
    or the departure of the last probe) drops what arrived since, so statistics, charts and
    events stop where the cook did.
    """

    def keep(series: list[list[Any]]) -> list[list[Any]]:
        return [point for point in series if point[0] <= end_rel]

    for n in js_keys(c["probes"]):
        p = c["probes"][n]
        p["series"] = keep(p["series"])
    for key in js_keys(c["pits"]):
        c["pits"][key] = keep(c["pits"][key])
    for n in js_keys(c["targets"]):
        c["targets"][n] = keep(c["targets"][n])
    for key in js_keys(c["ambients"]):
        c["ambients"][key] = keep(c["ambients"][key])
    for n in js_keys(c["predictions"]):
        c["predictions"][n] = [e for e in c["predictions"][n] if e["t"] <= end_rel]


def advance(js: Journal, now: float, events: list[Event]) -> None:
    """Time passes to `now`: the timers that close, drop or flag the open cook."""
    js["now"] = max(_to_number(js.get("now", UNDEFINED)), now)
    c = js["open"]
    if c is None:
        return
    meat = js_keys(c["probes"])
    if (c["provisional"] or not meat) and now - c["start"] >= CFG["provisional_s"]:
        js["open"] = None
        events.append({"type": "drop", "cook": c})
        return
    pending = c.get("pending_close")
    if pending is not None:
        # no participant left: nothing else may end it (an every() over none is true, and
        # an old last valid reading is not this cook's quiet), only the deadline
        if now > pending["deadline"]:
            events.append(_close(js, pending["at"], "channels_removed"))
            return
    else:
        # Which probes must be quiet: the participants only, so a removed probe never keeps a
        # cook open. Since when: the probes that left count as quiet from their departure,
        # and the last valid reading is the cook's, so no close ends before what a removed
        # probe recorded while it took part.
        current = _participants(c)
        if current and all(c["unplugged"].get(n) is not None for n in current):
            left = [c["start"] + rel for rel in c["retired"].values()]
            last = max([c["unplugged"][n] for n in current] + left)
            if now - last >= CFG["unplug_close_s"]:
                events.append(_close(js, last, "unplugged"))
                return
        if (
            current
            and c["lastValidAt"] is not None
            and now - c["lastValidAt"] >= CFG["off_close_s"]
            and all(
                c["unplugged"].get(n) is not None or c["off"].get(n) is not None for n in current
            )
        ):
            events.append(_close(js, c["lastValidAt"], "device_off"))
            return
    if not c["watchdog"] and now - c["start"] >= CFG["watchdog_s"]:
        c["watchdog"] = True
        events.append({"type": "update", "cook": c})


def observe(
    js: Journal,
    kind: Kinds,
    entity_id: str,
    t: float,
    state: Any,
    attrs: Mapping[str, Any] | None = None,
) -> list[Event]:
    """One row: entity kinds (from kinds()), entity id, time (last_updated), state,
    attributes. Events: open (an explicit or confirmed cook: write state now), close, drop,
    update.
    """
    events: list[Event] = []
    k = kind.get(entity_id)
    if not k or not is_finite_number(t):
        return events
    prev = js["last"].get(entity_id)
    if prev and t <= prev["t"]:
        return events  # already seen: recorder rows overlap live ones
    advance(js, t, events)
    js["last"][entity_id] = {"t": t, "v": state}
    kind_type, key = k[0], _key_of(k)

    if kind_type == "members":
        _apply_members(js, t, attrs)
        return events

    if kind_type == "press":
        # A button's state is its press time; a recorder row only repeats it unless it is newer.
        at = iso_seconds(state)
        if at is None or at < _to_number(js.get("since", UNDEFINED)):
            return events
        pressed = js["presses"].get(key)
        if at <= (-math.inf if pressed is None else pressed):
            return events
        js["presses"][key] = at
        if js["open"] is not None:
            events.append(_close(js, at, key))
        if key == "new_cook":
            js["inhibit"] = False
            js["open"] = _new_cook(js, kind, at, explicit=True)
            events.append({"type": "open", "cook": js["open"]})
        else:
            js["inhibit"] = True
        return events

    if kind_type == "probe":
        entry = _active_entry(js, key, INTERNAL)
        if entry is None:
            return events  # not an active internal: no part in any cook
        r = _reading(state)
        if js["open"] is None:
            if (
                js["inhibit"]
                and r[0] == "valid"
                and prev
                and _reading(prev.get("v"))[0] == "unplugged"
            ):
                js["inhibit"] = False
            if r[0] == "valid" and not js["inhibit"]:
                js["open"] = _new_cook(js, kind, t, explicit=False)
            return events
        c = js["open"]
        if key in c["retired"]:
            return events  # it left the cook: what it reads now is not this cook's
        c["lastObs"] = max(c["lastObs"], t)
        rel = js_round(t - c["start"])
        if r[0] == "valid":
            v = r[1]
            if c["probes"].get(key) is None:
                _join(c, key, t, v, entry)
            else:
                _add_point(c["probes"][key]["series"], rel, _c100(v))
            p = c["probes"][key]
            p["min"] = min(_lowest(p), v)
            c["lastValidAt"] = t
            c["unplugged"].pop(key, None)
            c["off"].pop(key, None)
            if c["provisional"] and v >= p["min"] + CFG["heat_evidence_c"]:
                _confirm(c, key, events)
        elif c["probes"].get(key) is not None or key in c["waiting"]:
            if c["probes"].get(key) is not None:
                _add_point(c["probes"][key]["series"], rel, None)
            if r[0] == "unplugged":
                if c["unplugged"].get(key) is None:
                    c["unplugged"][key] = t
            else:
                if r[0] == "connected":
                    # the probe's connection sensor says it is back in, its temperature not
                    # yet: whatever disconnection was running has ended
                    c["unplugged"].pop(key, None)
                # Plain unknown (the whole thermometer unavailable) says nothing about the
                # probe: a disconnection running since before still runs. Unplugging the
                # probes and then switching the thermometer off is a cook ending.
                if c["off"].get(key) is None:
                    c["off"][key] = t
        return events

    c = js["open"]
    if c is None:
        return events
    if kind_type == "pit":
        entry = _active_entry(js, key, AMBIENT)
        if entry is None:
            return events  # not an active pit
        c["lastObs"] = max(c["lastObs"], t)
        series = c["pits"].get(key)
        if series is None:
            series = c["pits"][key] = []
            c["channels"].setdefault(key, _channel(entry, AMBIENT))
        _add_point(series, js_round(t - c["start"]), _c100(_reading(state)[1]))
    elif kind_type == "target":
        c["lastObs"] = max(c["lastObs"], t)
        value = num(state)
        if value is not None:
            targets = c["targets"].get(key)
            if targets is None:
                targets = c["targets"][key] = []
            if not targets or not _strict_equal(targets[-1][1], value):
                targets.append([js_round(t - c["start"]), value])
    elif kind_type == "predictor" and key not in c["retired"]:
        c["lastObs"] = max(c["lastObs"], t)
        _note_prediction(c, key, t, state, attrs or {})
    return events


def _confirm(c: Cook, n: str, events: list[Event]) -> None:
    """A provisional cook is real: it started where this probe began to heat, the last
    reading within heat_start_c of its lowest.
    """
    p = c["probes"][n]
    limit = _c100(_lowest(p) + CFG["heat_start_c"])
    start_at = p["series"][0][0]
    for point in p["series"]:
        if point[1] is not None and point[1] <= limit:
            start_at = point[0]
    if start_at > 0:
        _rebase(c, start_at)
    c["provisional"] = False
    events.append({"type": "open", "cook": c})


def _rebase(c: Cook, by: int) -> None:
    """Move the start later by `by` seconds; what came before was the probe sitting
    unheated.
    """

    def cut(series: list[list[Any]]) -> list[list[Any]]:
        i = 0
        while i < len(series) - 1 and series[i + 1][0] <= by:
            i += 1
        return [[max(0, point[0] - by), point[1]] for point in series[i:]]

    c["start"] += by
    c["id"] = cook_id(c["start"])
    for n in js_keys(c["probes"]):
        p = c["probes"][n]
        p["series"] = cut(p["series"])
        first = next((point for point in p["series"] if point[1] is not None), None)
        if first is not None:
            p["first"] = first[1] / 100  # the start temperature is the one at the new start
    for key in js_keys(c["pits"]):
        c["pits"][key] = cut(c["pits"][key])
    for n in js_keys(c["targets"]):
        c["targets"][n] = cut(c["targets"][n])
    for key in js_keys(c["ambients"]):
        c["ambients"][key] = cut(c["ambients"][key])
    for field in ("retired", "waiting"):
        for key in js_keys(c[field]):
            c[field][key] = max(0, c[field][key] - by)
    for n in js_keys(c["predictions"]):
        moved = []
        for e in c["predictions"][n]:
            if not e["t"] >= by:
                continue
            out = {**e, "t": e["t"] - by}
            for field in ("d", "ss", "st", "r"):
                if out.get(field) is not None:
                    out[field] -= by
            moved.append(out)
        c["predictions"][n] = moved


def _note_prediction(c: Cook, n: str, t: float, status: Any, a: Mapping[str, Any]) -> None:
    """The predictor's published state, stored on a change of status, target, session or
    predictor build, a finish time that moved more than pred_move_s, or every pred_every_s.
    Times relative to the cook start. Its observed_at is also the probe's liveness: the
    predictor sees every report (HA last_reported), while a flat temperature changes nothing.
    """
    if c["probes"].get(n) is None:
        return  # only probes taking part in this cook
    rel = js_round(t - c["start"])

    def rel_iso(s: Any) -> Any:
        x = iso_seconds(s)
        return None if x is None else js_round(x - c["start"])

    if status != "unplugged" and num(a.get("temperature")) is not None:
        seen = iso_seconds(a.get("observed_at"))
        if seen is not None and seen <= t:
            c["lastValidAt"] = max(seen if c["lastValidAt"] is None else c["lastValidAt"], seen)
    e: dict[str, Any] = {
        "t": rel,
        "s": status,
        "g": num(a.get("target")),
        "d": rel_iso(a.get("done_at")),
        "m": a.get("model"),
        "c": a.get("confidence"),
        "ss": rel_iso(a.get("session_start")),
    }
    if status == "stall":
        e["st"] = rel_iso(a.get("stall_since"))
        e["a"] = num(a.get("eta_after_stall_min"))
    if status == "done":
        e["r"] = rel_iso(a.get("reached_at"))
    if not _truthy(c.get("predBuild", UNDEFINED)):
        c["predBuild"] = {}
    build = a.get("build")
    stored = c["predictions"].get(n)
    if stored is None:
        stored = c["predictions"][n] = []
    last = stored[-1] if stored else None
    if not _strict_equal(build, c["predBuild"].get(n)):
        e["b"] = build
        c["predBuild"][n] = build
    elif (
        last is not None
        and _strict_equal(last.get("s", UNDEFINED), e["s"])
        and _strict_equal(last.get("g", UNDEFINED), e["g"])
        and _strict_equal(last.get("ss", UNDEFINED), e["ss"])
        and rel - last["t"] < CFG["pred_every_s"]
        and (
            last.get("d") is None
            if e["d"] is None
            else last.get("d") is not None and abs(e["d"] - last["d"]) <= CFG["pred_move_s"]
        )
    ):
        return
    stored.append(e)


# ---- schema 1 ----


def _cook_v2(c: Cook) -> Cook:
    """A schema-1 cook as schema 2: its pit becomes pit LEGACY_PIT ("Pit"), its probes
    channels 'Probe N' in that pit. The fields keep schema 2's order; any others follow as
    they were.
    """
    if _schema_of(c) >= SCHEMA:
        return c
    probes = c.get("probes") or {}
    channels = {
        key: {"name": f"Probe {key}", "role": INTERNAL, "ambient": LEGACY_PIT}
        for key in js_keys(probes)
    }
    channels[LEGACY_PIT] = {"name": "Pit", "role": AMBIENT, "ambient": None}
    values = {
        **c,
        "schema": SCHEMA,
        "pits": {LEGACY_PIT: c.get("pit") or []},
        "channels": channels,
        "ambients": {},
        "retired": {},
        "waiting": {},
        "pending_close": None,
    }
    values.pop("pit", None)
    order = (
        *("schema", "id", "start", "end", "status", "provisional", "started_by", "ended_by"),
        *("watchdog", "name", "notes", "probes", "pits", "targets", "predictions", "fills"),
        *("channels", "ambients", "lastObs", "lastValidAt", "unplugged", "off"),
        *("retired", "waiting", "pending_close"),
    )
    out = {field: values[field] for field in order if field in values}
    out.update({field: value for field, value in values.items() if field not in out})
    return out


def migrate_journal(js: Journal) -> Journal:
    """A schema-1 journal state (a Node-RED state.json's snapshot) as schema 2: its open cook
    converted by the rules of _cook_v2, no membership yet (so every channel counts until the
    first membership row). Entity ids in `last` are left as they are.
    """
    out = {**js, "schema": SCHEMA}
    out.setdefault("members", None)
    if out.get("open") is not None:
        out["open"] = _cook_v2(out["open"])
    return out


def _schema_of(c: Mapping[str, Any]) -> int:
    schema = c.get("schema")
    return schema if is_finite_number(schema) else 1


# ---- reading a cook back ----


def held_stats(series: list[list[Any]], end: float) -> dict[str, Any]:
    """Time-weighted statistics of a held series over [0, end]: each value counts until the
    next point or the end, whichever comes first; nothing after the end counts.
    """
    total = 0
    duration = 0
    low = None
    high = None
    for i, point in enumerate(series):
        rel, v = point[0], point[1]
        if rel > end:
            break
        if v is None:
            continue
        to = min(end, series[i + 1][0] if i + 1 < len(series) else end)
        if to > rel:
            total += v * (to - rel)
            duration += to - rel
        low = v if low is None else min(low, v)
        high = v if high is None else max(high, v)
    return {
        "avg": total / duration / 100 if duration else None,
        "min": None if low is None else low / 100,
        "max": None if high is None else high / 100,
        "covered": duration,
    }


def target_at(c: Cook, n: str, rel: float) -> float | None:
    """The target in force for probe n at `rel` seconds into the cook."""
    v = None
    for r, x in c["targets"].get(n) or []:
        if r <= rel:
            v = x
        else:
            break
    return v


def hits(c: Cook, n: str, end: float) -> list[dict[str, Any]]:
    """Each time the probe reached the target in force: the first moment within each stretch
    of one target value that the temperature was at or above it, whether the temperature rose
    to it or the target was lowered under the temperature. [{rel, target}]
    """
    pts = c["probes"][n]["series"]
    tg = c["targets"].get(n) or []
    times = sorted(t for t in dict.fromkeys([p[0] for p in pts] + [x[0] for x in tg]) if t <= end)
    out: list[dict[str, Any]] = []
    i = 0
    j = 0
    v = None
    g = None
    hit = False
    for t in times:
        while i < len(pts) and pts[i][0] <= t:
            v = pts[i][1]
            i += 1
        while j < len(tg) and tg[j][0] <= t:
            x = tg[j][1]
            j += 1
            if not _strict_equal(x, g):
                g = x
                hit = False
        if not hit and v is not None and g is not None and v >= _c100(g):
            out.append({"rel": t, "target": g})
            hit = True
    return out


def reached_at(c: Cook, n: str, end: float) -> dict[str, Any] | None:
    """The hit of the target in force at the end, if it was reached while in force."""
    tg = [x for x in c["targets"].get(n) or [] if x[0] <= end]
    if not tg:
        return None
    since = tg[-1][0]
    final = tg[-1][1]
    return next(
        (h for h in hits(c, n, end) if h["rel"] >= since and _strict_equal(h["target"], final)),
        None,
    )


def forecasts(c: Cook, n: str, hit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the predictor said `before` seconds ahead of a hit, for that target and the probe
    session in force when it was reached. [{before, said, off}] (said: rel finish time)
    """
    stored = c["predictions"].get(n) or []
    session: Any = None
    for e in stored:
        if e["t"] <= hit["rel"]:
            session = e.get("ss", UNDEFINED)
        else:
            break
    out: list[dict[str, Any]] = []
    for before in (3 * 3600, 2 * 3600, 3600, 1800):
        at = hit["rel"] - before
        if at < 0:
            continue
        said = None
        for e in stored:
            if e["t"] <= at:
                said = e
            else:
                break
        if (
            said is not None
            and said.get("s") == "predicting"
            and said.get("d") is not None
            and _strict_equal(said.get("g", UNDEFINED), hit["target"])
            and _strict_equal(said.get("ss", UNDEFINED), session)
        ):
            out.append({"before": before, "said": said["d"], "off": said["d"] - hit["rel"]})
    return out


def stall_intervals(c: Cook, n: str, end: float) -> list[list[float]]:
    """Stalls as the predictor published them, from each stall's onset (stall_since) to the
    next report that is not a stall. Any other report ends it there, so unknown time (stale
    after a restart) never counts as stall; a stall reported again with an onset before that
    end (the predictor replayed it, or the lid was open) overlaps the earlier piece and the
    two merge into one, while a stall starting after the gap stays a separate one.
    """
    raw: list[list[float]] = []
    start_at = None
    for e in c["predictions"].get(n) or []:
        if e.get("s") == "stall":
            if start_at is None:
                start_at = e["t"] if e.get("st") is None else e["st"]
        elif start_at is not None:
            raw.append([start_at, e["t"]])
            start_at = None
    if start_at is not None:
        raw.append([start_at, end])
    out: list[list[float]] = []
    for a, b in sorted(raw, key=lambda piece: piece[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _spark(series: list[list[Any]], end: float) -> list[list[Any]]:
    points = CFG["spark_points"]
    if len(series) <= points:
        return series
    out = []
    i = 0
    for k in range(points + 1):
        at = (k * end) / points
        while i + 1 < len(series) and series[i + 1][0] <= at:
            i += 1
        out.append([js_round(at), series[i][1]])
    return out


def _end_of(c: Cook, now: float | None) -> Any:
    if c.get("end") is not None:
        end = c["end"]
    else:
        end = max(c["lastObs"], c["lastObs"] if now is None else now)
    return js_round(end - c["start"])


def _end_for(c: Cook, key: str, end: float) -> float:
    """Where a probe's part of the cook ends: at its retirement, if it left before the end."""
    retired = (c.get("retired") or {}).get(key)
    return end if retired is None else min(end, retired)


def _start_temp(p: Mapping[str, Any]) -> float:
    """The meat's starting temperature: the lowest reading in the first 30 min (a probe often
    reads the air before it goes in, and that reading is the first one).
    """
    low = None
    for point in p["series"]:
        if point[0] > 1800:
            break
        if point[1] is not None:
            low = point[1] if low is None else min(low, point[1])
    return p["first"] if low is None else low / 100


def _probe_name(c: Cook, key: str) -> Any:
    channel = (c.get("channels") or {}).get(key) if _schema_of(c) >= 2 else None
    name = channel.get("name") if isinstance(channel, dict) else None
    return f"Probe {key}" if name is None else name


def _pit_series(c: Cook) -> dict[str, list[list[Any]]]:
    """Every pit's series by key: schema 1's one pit is "pit"."""
    if _schema_of(c) < 2:
        return {"pit": c.get("pit") or []}
    pits = c.get("pits") or {}
    return {key: pits[key] for key in js_keys(pits)}


def _pit_name(c: Cook, key: str) -> Any:
    if _schema_of(c) < 2:
        return "Pit"
    channel = (c.get("channels") or {}).get(key)
    name = channel.get("name") if isinstance(channel, dict) else None
    return f"Pit {key}" if name is None else name


def derive(c: Cook, now: float | None = None) -> dict[str, Any]:
    """What the detail view shows that is computed rather than recorded, per probe and per
    pit (schema 1's one pit under the key "pit").
    """
    end = _end_of(c, now)
    probes: dict[str, Any] = {}
    for n in js_keys(c["probes"]):
        until = _end_for(c, n, end)
        reached = reached_at(c, n, until)
        probes[n] = {
            "hits": hits(c, n, until),
            "reached": reached,
            "stalls": stall_intervals(c, n, until),
            "forecasts": forecasts(c, n, reached) if reached else [],
            "stats": held_stats(c["probes"][n]["series"], until),
            "start_temp": _start_temp(c["probes"][n]),
        }
    pits = {key: held_stats(series, end) for key, series in _pit_series(c).items()}
    return {"end": end, "probes": probes, "pits": pits}


def _last_value(points: Iterable[list[Any]], end: float) -> Any:
    """series.filter(([rel]) => rel <= end).at(-1)?.[1] ?? null"""
    value = None
    for point in points:
        if point[0] <= end:
            value = point[1] if len(point) > 1 else None
    return value


def summarize(c: Cook, now: float | None = None) -> dict[str, Any]:
    """The index entry for a cook (the list view needs nothing else), one shape for both
    schemas: each probe by key and name (schema 1 keeps its number n too: key "1", name
    "Probe 1"), and pits by key (schema 1's one pit is "pit", named "Pit").
    """
    end = _end_of(c, now)
    schema = _schema_of(c)
    probes = []
    for n in js_keys(c["probes"]):
        p = c["probes"][n]
        until = _end_for(c, n, end)
        stats = held_stats(p["series"], until)
        last = _last_value(p["series"], until)
        reached = reached_at(c, n, until)
        stall_s = 0
        for a, b in stall_intervals(c, n, until):
            stall_s = stall_s + (b - a)  # in order, as reduce() adds: sum() would compensate
        entry: dict[str, Any] = {"n": _key_number(n)} if schema < 2 else {}
        entry.update(
            {
                "key": n,
                "name": _probe_name(c, n),
                "start_temp": _start_temp(p),
                "max": stats["max"],
                "last": None if last is None else last / 100,
                "target": _last_value(c["targets"].get(n) or [], until),
                "reached": reached["rel"] if reached else None,
                "stall_s": stall_s,
                "spark": _spark(p["series"], end),
            }
        )
        probes.append(entry)
    pits = {}
    for key, series in _pit_series(c).items():
        stats = held_stats(series, end)
        pits[key] = {
            "name": _pit_name(c, key),
            "avg": stats["avg"],
            "min": stats["min"],
            "max": stats["max"],
            "spark": _spark(series, end),
        }
    summary = {
        "schema": schema,
        "id": c.get("id", UNDEFINED),
        "start": c.get("start", UNDEFINED),
        "end": c.get("end", UNDEFINED),
        "status": c.get("status", UNDEFINED),
        "provisional": c.get("provisional", UNDEFINED),
        "started_by": c.get("started_by", UNDEFINED),
        "ended_by": c.get("ended_by", UNDEFINED),
        "press_at": c.get("press_at"),
        "name": c.get("name", UNDEFINED),
        "notes": c.get("notes", UNDEFINED),
        "watchdog": c.get("watchdog", UNDEFINED),
        "duration_s": end,
        "probes": probes,
        "pits": pits,
        "fills": len(c["fills"]),
    }
    # a field the record lacks is left out, as JSON.stringify leaves out an undefined one
    return {k: v for k, v in summary.items() if v is not UNDEFINED}
