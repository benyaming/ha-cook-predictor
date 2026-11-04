"""Cook-time predictor for single-point meat probes plus one ambient (pit/oven) probe.

A line-by-line port of the JavaScript estimator that ran the cook predictor in Node-RED.
Golden files recorded from that JavaScript (tests/fixtures/estimator) must come out of
this module unchanged, floats bit for bit once V8's exp and log are swapped in, so keep
the operation order when changing anything here.

Model: Newton heating, dT/dt = k (A - T), A = measured ambient. k is fitted over a
sliding window by simulating forward through the real ambient history, never by
differencing adjacent quantised samples (the Inkbird reports in 0.1 °F steps).
A rolling linear slope is computed alongside as the baseline to beat on replay.

Times are epoch seconds, temperatures °C. The state is plain dicts and lists of
numbers, strings, booleans and None, so it stays JSON-serialisable.

Names: camelCase became snake_case throughout, in CFG keys, state keys and functions
(sentinelC -> sentinel_c, kPeak -> k_peak, heldIsObserved -> held_is_observed,
approachPending -> approach_pending, fitK -> fit_k, ...). Attribute keys, statuses and
events were snake_case already and are unchanged. The per-probe alert settings are
warn_min, alert_temp and alert_min (warnMin, alertTemp, alertMin).

Where Python differs from JavaScript the port keeps the JavaScript behaviour:
- Math.round rounds halves toward +infinity (_js_round), not to even.
- Math.exp and Math.log run on the platform libm (exp, log), which differs from V8's
  fdlibm code in the last bit for about 1% of exp arguments. The golden tests swap in a
  port of V8's code to compare bit for bit. With the libm nothing published differed on
  any golden; a value right at a rounding boundary could still move by one step.
- Times format like Date.prototype.toISOString() (_iso), k like
  Number(k.toPrecision(3)) (_precision3).
- A key missing from a dict is JavaScript's undefined; a key holding None is null.
  Where the JavaScript does arithmetic on a null ambient, null counts as 0 (_num).
- Float sums accumulate in order with +=; sum() would compensate and round differently.
- replay() takes the probes in Object.keys() order (_js_keys).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

type State = dict[str, Any]
type Probe = dict[str, Any]
type Result = dict[str, Any]

CFG: dict[str, Any] = {
    # The JavaScript had 500: unplugged Inkbird probes read 3623 °C (raw 6553.5 °F). Readings
    # now arrive normalised (core/normalize.py: valid below 1000 °C, a disconnected probe as
    # NaN), so the limit only has to stay above every valid reading: a 600 °C pizza oven is a
    # pit, not an unplugged probe. The goldens' 3623 rows are still invalid.
    "sentinel_c": 1000,
    "unplug_end_s": 120,  # unplugged this long ends the session
    "warmup_s": 600,  # no ETA in the first 10 min of a session...
    "min_rise_c": 1.5,  # ...nor before the fit window has risen this much
    "win_min_s": 600,
    "win_max_s": 5400,
    "win_rise_c": 5,  # fit window grows back until it spans this rise (10-90 min)
    "slope_s": 1800,  # linear baseline: slope over the last 30 min
    "amb_fast_s": 900,  # forecast ambient: time-weighted median of the last 15 min
    "amb_slow_s": 3600,
    "amb_keep_s": 3 * 3600,
    "amb_thin_s": 10,  # keep at most about one ambient sample per 10 s
    # 15-min ambient this far under the 60-min one: heat lost or lid open
    "amb_drop_c": 15,
    "k_min": 1e-6,
    "k_max": 1e-2,  # 1/s
    "fit_rmse_max_c": 0.6,
    "fit_rmse_ok_c": 0.3,
    # second-half slope over first-half slope above this: still accelerating
    "accel_ratio": 1.3,
    # reference k: the session's highest 30-min median of good fits...
    "k_smooth_s": 1800,
    # ...made once the core is this share of the way from its start to the pit
    "k_learn_frac": 1 / 3,
    "stall_min_temp_c": 40,
    # brisket-type cooks: a stall is still likely below this core temp
    "stall_ahead_below_c": 70,
    "stall_ahead_target_c": 80,
    "stall_ahead_pit_c": 150,
    "stall_enter": 0.35,  # observed slope / slope the reference k expects
    "stall_exit": 0.6,
    "stall_min_s": 900,
    "jump_drop_c": 5,  # a fall this big within jump_s: probe moved or meat flipped
    "jump_s": 300,
    "done_hyst_c": 1,
    "done_rearm_c": 3,
    # default heads-up before the target (per probe: the warn_min alert setting)
    "approach_s": 1800,
    # a heads-up re-arms once its ETA is this much (or half the lead) past the lead
    "rearm_s": 900,
    "stale_s": 600,  # no report (change or heartbeat) for this long: no prediction
    "hold_min_s": 30,  # a heartbeat this much newer than the last change extends the trace
    "max_samples": 6000,
}


def _is_number(v: object) -> bool:
    """JavaScript's typeof v === 'number' (bool is an int subclass in Python)."""
    return isinstance(v, int | float) and not isinstance(v, bool)


def valid(v: object) -> bool:
    """A real probe reading: finite, above -40 °C and under the unplugged sentinel."""
    return _is_number(v) and math.isfinite(v) and v > -40 and v < CFG["sentinel_c"]


def _js_round(x: float) -> float:
    """Math.round: the nearest integer, halves toward +infinity (round() goes to even)."""
    if not math.isfinite(x):
        return x
    n = math.floor(x)
    return n + 1 if x - n >= 0.5 else n


def _r1(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else _js_round(x * 10) / 10


def _r2(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else _js_round(x * 100) / 100


def _iso(t: float) -> str:
    """new Date(t * 1000).toISOString(): UTC to the millisecond with a Z suffix.

    Like JavaScript, a year past 9999 takes the +YYYYYY form, and a time outside the
    Date range raises (toISOString() throws a RangeError there).
    """
    ms = t * 1000
    if not abs(ms) <= 8.64e15:
        raise ValueError(f"invalid time value: {t!r}")
    days, ms = divmod(math.trunc(ms), 86_400_000)
    # The proleptic Gregorian date of a day count from 1970-01-01.
    z = days + 719_468
    era = z // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year = yoe + era * 400 + (month <= 2)
    y = f"{year:04d}" if 0 <= year <= 9999 else f"{'+' if year > 0 else '-'}{abs(year):06d}"
    hh, ms = divmod(ms, 3_600_000)
    mm, ms = divmod(ms, 60_000)
    ss, ms = divmod(ms, 1000)
    return f"{y}-{month:02d}-{day:02d}T{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}Z"


def _precision3(x: float) -> float:
    """Number(x.toPrecision(3)): three significant digits of the exact binary value,
    a tie going away from zero."""
    if x == 0 or not math.isfinite(x):
        return x
    d = Decimal(x)
    return float(d.quantize(Decimal(1).scaleb(d.adjusted() - 2), rounding=ROUND_HALF_UP))


def _num(x: float | None) -> float:
    """JavaScript arithmetic on a reading that may be null: null counts as 0."""
    return 0 if x is None else x


def _js_keys(obj: dict[str, Any]) -> list[str]:
    """Object.keys() order: array-index keys ascending, then the rest as inserted."""

    def is_index(k: object) -> bool:
        return (
            isinstance(k, str)
            and k.isascii()
            and k.isdigit()
            and (k == "0" or k[0] != "0")
            and int(k) < 2**32 - 1
        )

    return sorted((k for k in obj if is_index(k)), key=int) + [k for k in obj if not is_index(k)]


# Math.exp and Math.log. Module-level names, looked up at each call, so the golden tests can
# put a port of V8's own code (tests/core/_jsmath.py) in their place and compare with the
# JavaScript bit for bit: the platform libm differs from V8 in the last bit for about 1% of
# exp arguments.
exp = math.exp
log = math.log

# A replay row's value for "nothing known from here" (see mark_gap); compared by identity.
GAP: Any = type("Gap", (), {"__repr__": lambda self: "GAP"})()


# ---- the model ----


# Observation model. samples holds value changes only. Between changes the value is
# taken as held while the device is known to be alive: from HA's recorded history (an
# unreachable device leaves an unavailable row) or, live, from heartbeats: HA last_reported
# for the entity. That is integration liveness, not a per-probe measurement time;
# tuya_local rewrites every entity when any DP arrives, and on the IBBQ-4T all four
# probes share DP 107. So a heartbeat says "the device is talking and this probe's value
# has not changed", which is the cache assumption the hold rests on.
#
# Live (st["held_is_observed"] false) a silence longer than stale_s is a gap: nothing is
# bridged across it; the fit and any stall candidate restart from the resumed reading.
# replay() runs recorded history with held_is_observed true: there are no heartbeats, an
# unreachable device leaves an unavailable row, so a valid value is current until the
# next row, up to the replay cursor. Replay never sets a live observation time: after it
# every probe waits (await_live) for a real change or heartbeat before it is fresh.
#
# last_t: ordering watermark over every reading, valid or not. last_obs: newest change or
# heartbeat. reported_at: newest heartbeat that repeated the last value.
def _new_probe() -> Probe:
    return {
        "samples": [],
        "last_t": None,
        "last_obs": None,
        "reported_at": None,
        "await_live": False,
        "session": None,
        "off_since": None,
        "fit_from": None,
        "moved_at": None,
        "gap_at": None,
        "k_hist": [],
        "k_peak": None,
        "stall": None,
        "stall_cand": None,
        "stall_seen": False,
        "done": False,
        "done_at": None,
        "ever_predicting": False,
        "notified": {},
        "alert": {},
    }


def new_state(keys: Iterable[str]) -> State:
    """A fresh state for the probes named by keys (strings: they are JSON object keys)."""
    probes = {}
    for k in keys:
        probes[k] = _new_probe()
    return {
        "probes": probes,
        "ambient": [],
        "ambient_last_t": None,
        "ambient_last_obs": None,
        "ambient_reported_at": None,
        "ambient_gap_at": None,
        "ambient_off_since": None,
        "ambient_await_live": False,
        "reset_at": None,
        "held_is_observed": False,
        "replay_end": None,
    }


def _start_session(p: Probe, start: float, t: float | None, v: float | None = None) -> None:
    """A session that starts at start (a plug-in, or a "New cook" press) and, once a real
    reading exists, holds it as its first sample."""
    last_t = p["last_t"]
    p.update(_new_probe())
    p.update(last_t=last_t, session={"start": start, "start_temp": None}, fit_from=start)
    if t is not None:
        _first_sample(p, t, v)


def _first_sample(p: Probe, t: float, v: float) -> None:
    p["samples"].append([t, v])
    p["session"]["start_temp"] = v
    p["last_obs"] = t


def _relearn(p: Probe, t: float) -> None:
    """The probe sits somewhere new: everything learned about the old spot is void."""
    p.update(
        fit_from=t,
        moved_at=t,
        k_hist=[],
        k_peak=None,
        stall=None,
        stall_cand=None,
        ever_predicting=False,
    )


def _note_observation(st: State, p: Probe, t: float) -> None:
    """Live only: a silence beyond stale_s ends the continuous stretch. Keep the session and
    what was learned about this spot (k_peak), but refit and recheck a stall from here."""
    # The first live reading after a replay continues the replayed trace if it lands within
    # stale_s of the recorded coverage; after that, against the previous observation.
    prev = st["replay_end"] if p["await_live"] else p["last_obs"]
    p["await_live"] = False
    if not st["held_is_observed"] and prev is not None and t - prev > CFG["stale_s"]:
        p["fit_from"] = max(p["fit_from"] if p["fit_from"] is not None else t, t)
        p["gap_at"] = t
        p["stall_cand"] = None
    p["last_obs"] = max(p["last_obs"] if p["last_obs"] is not None else t, t)


def _since(samples: list[list[float]], t0: float) -> list[list[float]]:
    i = len(samples)
    while i > 0 and samples[i - 1][0] >= t0:
        i -= 1
    return samples[i:]


def _held(samples: list[list[float]], t0: float) -> list[list[float]]:
    """_since(), led by the value held at t0. Samples are changes only, so a flat stretch
    has almost none; the reading in force at t0 is still a reading of that moment."""
    pts = _since(samples, t0)
    before = len(samples) - len(pts) - 1
    if before >= 0 and (not pts or pts[0][0] > t0):
        pts.insert(0, [t0, samples[before][1]])
    return pts


def ingest_probe(st: State, key: str, t: float, v: float | None) -> None:
    """A probe's value changed to v at t (an invalid v: unplugged or unavailable)."""
    p = st["probes"][key]
    if p["last_t"] is not None and t <= p["last_t"]:
        return
    p["last_t"] = t
    if not valid(v):
        if p["off_since"] is None:
            p["off_since"] = t
        return
    was_off = p["off_since"] is not None
    ended = was_off and t - p["off_since"] >= CFG["unplug_end_s"]
    reset = (
        st["reset_at"] is not None
        and p["session"] is not None
        and p["session"]["start"] < st["reset_at"]
        and t >= st["reset_at"]
    )
    p["off_since"] = None
    if p["session"] is None or ended or reset:
        _start_session(p, st["reset_at"] if reset and not ended else t, t, v)
        return
    if t < p["session"]["start"]:
        return  # from before a "New cook" press
    if not p["samples"]:
        _first_sample(p, t, v)
        return
    # Movement: a fall against the recent peak. The value held before this change counts
    # when it was current within jump_s: always in replay (held up to the change), live when
    # confirmed within jump_s, or for the first live change, by the replay's coverage end.
    # Never across an unavailable stretch.
    prev = p["samples"][-1]
    peak = -math.inf
    for _, tv in _since(p["samples"], max(t - CFG["jump_s"], p["fit_from"])):
        peak = max(peak, tv)
    confirmed_at = st["replay_end"] if p["await_live"] else p["last_obs"]
    held_recent = st["held_is_observed"] or (
        confirmed_at is not None and t - confirmed_at <= CFG["jump_s"]
    )
    if not was_off and held_recent:
        peak = max(peak, prev[1])
    _note_observation(st, p, t)
    if peak - v >= CFG["jump_drop_c"]:
        _relearn(p, t)
    p["reported_at"] = None  # heartbeats confirmed the previous value, not this one
    p["samples"].append([t, v])
    if len(p["samples"]) > CFG["max_samples"]:
        del p["samples"][: len(p["samples"]) - CFG["max_samples"]]


def report(st: State, key: str, t: float, v: float | None) -> None:
    """A heartbeat: HA last_reported moved while the value stayed (see the observation
    model). It lets a flat stall age without inventing a sample. Right after "New cook" the
    first heartbeat later than the press starts the new session with the value it confirms.
    key "ambient" is the pit probe."""
    if not valid(v):
        return
    if key == "ambient":
        last = st["ambient"][-1] if st["ambient"] else None
        if last and st["ambient_off_since"] is None and v == last[1] and t > last[0]:
            _note_ambient(st, t)
            reported_at = st["ambient_reported_at"]
            st["ambient_reported_at"] = max(reported_at if reported_at is not None else 0, t)
        return
    p = st["probes"][key]
    if p["session"] is None or p["off_since"] is not None:
        return
    last = p["samples"][-1] if p["samples"] else None
    if not last:
        if t > p["session"]["start"]:
            _first_sample(p, t, v)
        return
    if v == last[1] and t > last[0]:
        _note_observation(st, p, t)
        p["reported_at"] = max(p["reported_at"] if p["reported_at"] is not None else 0, t)


def mark_gap(st: State, key: str, t: float) -> None:
    """Nothing is known about the probe from t (its source is unavailable or unknown).

    Not in the JavaScript, which took an unavailable row as an unplug and ended the session
    two minutes later, so a short radio dropout restarted the cook's model. A gap keeps the
    session: the probe shows stale at once (no ETA, no alerts, no value held across it), and
    the next reading resumes it like a live silence (end_gap). Only a disconnected probe
    (an invalid value through ingest_probe) ends a session.
    """
    p = st["probes"][key]
    if p["last_t"] is not None and t <= p["last_t"]:
        return
    p["last_t"] = t
    if p["session"] is not None and p.get("gap_since") is None:
        p["gap_since"] = t


def end_gap(st: State, key: str, t: float) -> None:
    """The first observation after a gap: refit and recheck a stall from here."""
    p = st["probes"][key]
    if p.get("gap_since") is None:
        return
    p["gap_since"] = None
    if p["session"] is not None:
        p["fit_from"] = max(p["fit_from"] if p["fit_from"] is not None else t, t)
        p["gap_at"] = t
        p["stall_cand"] = None


def mark_ambient_gap(st: State, t: float) -> None:
    """The pit's equivalent of mark_gap: no Newton model until it resumes."""
    if st["ambient_last_t"] is not None and t <= st["ambient_last_t"]:
        return
    st["ambient_last_t"] = t
    if st.get("ambient_gap_since") is None:
        st["ambient_gap_since"] = t


def end_ambient_gap(st: State, t: float) -> None:
    """The pit resumed: no fit may span the gap."""
    if st.get("ambient_gap_since") is None:
        return
    st["ambient_gap_since"] = None
    st["ambient_gap_at"] = t


def reset_probe(st: State, key: str, t: float) -> None:
    """A "New cook" press: a new session from the press. It holds no reading until the
    device supplies one after the press: the button is not a measurement."""
    p = st["probes"][key]
    if p["session"] is None or p["session"]["start"] >= t:
        return
    off_since = p["off_since"]
    _start_session(p, t, None)
    p["off_since"] = off_since


def _note_ambient(st: State, t: float) -> None:
    """The pit is the fit's forcing input: an unavailable stretch or a live silence is a gap,
    and no Newton fit may span it (see fit_start in evaluate)."""
    prev = st["replay_end"] if st["ambient_await_live"] else st["ambient_last_obs"]
    if st["ambient_off_since"] is not None or (
        not st["held_is_observed"] and prev is not None and t - prev > CFG["stale_s"]
    ):
        st["ambient_gap_at"] = t
    st["ambient_await_live"] = False
    last_obs = st["ambient_last_obs"]
    st["ambient_last_obs"] = max(last_obs if last_obs is not None else t, t)


def ingest_ambient(st: State, t: float, v: float | None) -> None:
    """The pit probe's value changed to v at t (an invalid v: unplugged or unavailable)."""
    a = st["ambient"]
    if st["ambient_last_t"] is not None and t <= st["ambient_last_t"]:
        return
    st["ambient_last_t"] = t
    if not valid(v):
        if st["ambient_off_since"] is None:
            st["ambient_off_since"] = t
        return
    _note_ambient(st, t)
    st["ambient_off_since"] = None
    if len(a) > 1 and t - a[-2][0] < CFG["amb_thin_s"]:
        a[-1] = [t, v]
    else:
        a.append([t, v])
    cut = 0
    while cut + 1 < len(a) and a[cut + 1][0] < t - CFG["amb_keep_s"]:
        cut += 1
    if cut:
        del a[:cut]


def ambient_median(st: State, from_: float, to: float) -> float | None:
    """Time-weighted median of the ambient step function over [from_, to]."""
    a = st["ambient"]
    seg = []
    for i in range(len(a)):
        s = max(a[i][0], from_)
        e = min(a[i + 1][0] if i + 1 < len(a) else to, to)
        if e > s:
            seg.append([a[i][1], e - s])
    if not seg:
        return None
    seg.sort(key=lambda x: x[0])
    total = 0
    for x in seg:
        total += x[1]
    half = total / 2
    acc = 0
    for v, w in seg:
        acc += w
        if acc >= half:
            return v
    return seg[-1][0]


def simulate(win: list[list[float]], amb: list[list[float]], k: float) -> float:
    """Sum of squared errors of a forward Newton simulation anchored at win[0], driven by
    the ambient step function (its first value extended backwards)."""
    n_amb = len(amb)
    j = 0
    while j + 1 < n_amb and amb[j + 1][0] <= win[0][0]:
        j += 1
    T = win[0][1]
    sse = 0
    for i in range(1, len(win)):
        t = win[i - 1][0]
        t_end = win[i][0]
        while t < t_end:
            nxt = amb[j + 1][0] if j + 1 < n_amb else math.inf
            stop = min(t_end, nxt)
            A = amb[j][1]
            T = A + (T - A) * exp(-k * (stop - t))
            t = stop
            if t >= nxt:
                j += 1
        e = T - win[i][1]
        sse += e * e
    return sse


def fit_k(win: list[list[float]], amb: list[list[float]]) -> dict[str, Any]:
    """1-D fit of k on a log grid, then golden-section refinement."""
    lo = log(CFG["k_min"])
    hi = log(CFG["k_max"])
    n = 24
    step = (hi - lo) / n

    def f(x: float) -> float:
        return simulate(win, amb, exp(x))

    best = lo
    best_s = math.inf
    for i in range(n + 1):
        x = lo + step * i
        s = f(x)
        if s < best_s:
            best_s = s
            best = x
    a = max(lo, best - step)
    b = min(hi, best + step)
    g = (math.sqrt(5) - 1) / 2
    c = b - g * (b - a)
    d = a + g * (b - a)
    fc = f(c)
    fd = f(d)
    for _ in range(20):
        if fc < fd:
            b = d
            d = c
            fd = fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a = c
            c = d
            fc = fd
            d = a + g * (b - a)
            fd = f(d)
    x = (a + b) / 2
    return {
        "k": exp(x),
        "rmse": math.sqrt(f(x) / (len(win) - 1)),
        "at_bound": x <= lo + step / 2 or x >= hi - step / 2,
    }


def _slope(pts: list[list[float]]) -> float | None:
    n = len(pts)
    if n < 2:
        return None
    t0 = pts[0][0]
    sx = sy = sxx = sxy = 0
    for t, v in pts:
        x = t - t0
        sx += x
        sy += v
        sxx += x * x
        sxy += x * v
    den = n * sxx - sx * sx
    return (n * sxy - sx * sy) / den if den > 0 else None  # °C/s


def _pick_window(samples: list[list[float]], from_: float) -> list[list[float]]:
    """Walk back from the newest sample until the window spans win_rise_c (10-90 min)."""
    last = samples[-1]
    i = len(samples) - 1
    while i > 0:
        s = samples[i - 1]
        if s[0] < from_ or last[0] - s[0] > CFG["win_max_s"]:
            break
        i -= 1
        if last[0] - s[0] >= CFG["win_min_s"] and last[1] - s[1] >= CFG["win_rise_c"]:
            break
    return samples[i:]


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) >> 1
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _alert_settings(o: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-probe alert settings, all optional: warn_min, the heads-up before the target in
    minutes (0 = off, default approach_s); alert_temp, a second temperature to alert at
    (0 or missing = off); alert_min, a heads-up before alert_temp (0 = only when reached)."""
    o = {} if o is None else o

    def lead(v: object, dflt: float) -> float:
        return v * 60 if _is_number(v) and math.isfinite(v) and v >= 0 else dflt

    alert_temp = o.get("alert_temp")
    return {
        "warn_s": lead(o.get("warn_min"), CFG["approach_s"]),
        "alert_temp": alert_temp if valid(alert_temp) and alert_temp > 0 else None,
        "alert_lead_s": lead(o.get("alert_min"), 0),
    }


def _rearm_at(lead_s: float) -> float:
    return lead_s + max(CFG["rearm_s"], lead_s / 2)


def _heads_up(
    obj: dict[str, Any],
    name: str,
    eta: float | None,
    lead_s: float,
    confident: bool,
    out: Result,
    event: str,
) -> None:
    """A heads-up latch, obj[name]: sent once a confident ETA comes within lead_s, re-armed
    once the ETA is well past it. After a settings change (obj[name + "_pending"]) the next
    estimate of any confidence sets the latch silently: already inside the window = sent."""
    if eta is None or not math.isfinite(eta) or eta < 0 or not lead_s > 0:
        return
    pending = f"{name}_pending"
    if obj.get(pending):
        obj[name] = eta <= lead_s
        obj[pending] = False
    elif not confident:
        return
    elif not obj.get(name) and eta <= lead_s:
        obj[name] = True
        out["events"].append(event)
    elif obj.get(name) and eta > _rearm_at(lead_s):
        obj[name] = False


def evaluate(
    st: State, key: str, now: float, target: float | None, alerts: dict[str, Any] | None = None
) -> Result:
    """Status of one probe at now. Mutates the probe's session state (stall, done,
    notification flags) and returns {"status", "attrs", "events"}.

    Alerts fire when the cook crosses them, never because a setting changed: a new target
    or alert temperature already passed is marked sent silently, and a changed heads-up
    takes its sent/armed state from the next estimate.
    """
    p = st["probes"][key]
    out: Result = {"status": "unplugged", "attrs": {}, "events": []}
    if (
        p["session"] is not None
        and p["off_since"] is not None
        and now - p["off_since"] >= CFG["unplug_end_s"]
    ):
        p["session"] = None
    if p["session"] is None or p["off_since"] is not None:
        return out
    if not p["samples"]:
        out["status"] = "warming"
        out["attrs"]["session_start"] = _iso(p["session"]["start"])
        out["attrs"]["reason"] = "waiting for a reading"
        return out

    # The JavaScript's T, A, A60 and X are temp, amb, amb60 and alert_temp here.
    # tl: last change; t_obs: last time temp is known to hold (a change, a heartbeat, or in
    # replay the cursor). Durations, windows and the finish time run on t_obs.
    s = p["samples"]
    tl, temp = s[-1]
    t_obs = max(
        tl,
        p["reported_at"] if p["reported_at"] is not None else tl,
        now if st["held_is_observed"] else -math.inf,
    )
    gap_since = p.get("gap_since")
    if gap_since is not None:
        t_obs = min(t_obs, max(tl, gap_since))  # nothing is held across a gap
    obs = [*s, [t_obs, temp]] if t_obs - tl >= CFG["hold_min_s"] else s
    amb_ok = (
        st["ambient_off_since"] is None
        and st.get("ambient_gap_since") is None
        and st["ambient_last_obs"] is not None
        and (
            st["held_is_observed"]
            or (not st["ambient_await_live"] and now - st["ambient_last_obs"] <= CFG["stale_s"])
        )
    )
    # Newton needs the pit continuously since win_min_s before the fit window opens.
    gap_at = st["ambient_gap_at"]
    amb_ready = amb_ok and (gap_at is None or t_obs - gap_at >= CFG["win_min_s"])

    def amb_from(span: float) -> float:
        return max(t_obs - span, gap_at if gap_at is not None else -math.inf)

    amb = ambient_median(st, amb_from(CFG["amb_fast_s"]), t_obs) if amb_ok else None
    amb60 = ambient_median(st, amb_from(CFG["amb_slow_s"]), t_obs) if amb_ok else None
    a = out["attrs"]
    a.update(
        {
            "temperature": _r1(temp),
            "target": target if valid(target) else None,
            "ambient": _r1(amb),
            "session_start": _iso(p["session"]["start"]),
            "start_temp": _r1(p["session"]["start_temp"]),
            "elapsed_min": _js_round((t_obs - p["session"]["start"]) / 60),
            "last_sample": _iso(tl),
            "observed_at": _iso(t_obs),
        }
    )
    if not valid(target):
        out["status"] = "no_target"
        return out
    if gap_since is not None:
        out["status"] = "stale"
        a["reason"] = "probe unavailable"
        return out
    if p["await_live"] or now - t_obs > CFG["stale_s"]:
        out["status"] = "stale"
        a["reason"] = "waiting for a reading" if p["await_live"] else "no recent reading"
        return out

    # A setting that changed, as opposed to the first evaluation of a session (a key not
    # yet set, JavaScript's undefined).
    cfg = _alert_settings(alerts)
    retarget = "target" in p["notified"] and p["notified"]["target"] != target
    if "target" not in p["notified"] or p["notified"]["target"] != target:
        p["notified"] = {"target": target, "approach_pending": retarget}
        p["done"] = False
    notified = p["notified"]
    if "warn_s" in notified and notified["warn_s"] != cfg["warn_s"]:
        notified["approach_pending"] = True
    notified["warn_s"] = cfg["warn_s"]

    # The second alert temperature keeps its own flags, so a new target does not re-send it.
    # Its heads-up needs the model, which stops at the target: at or above the target the
    # second temperature alerts only when reached. A new target can change that, so it
    # re-establishes the heads-up like a changed lead.
    alert_temp = cfg["alert_temp"]
    retemp = "temp" in p["alert"] and p["alert"]["temp"] != alert_temp
    if "temp" not in p["alert"] or p["alert"]["temp"] != alert_temp:
        p["alert"] = {"temp": alert_temp, "soon_pending": retemp}
    elif "lead_s" not in p["alert"] or p["alert"]["lead_s"] != cfg["alert_lead_s"] or retarget:
        p["alert"]["soon_pending"] = True
    p["alert"]["lead_s"] = cfg["alert_lead_s"]
    if alert_temp is not None:
        a["alert_temp"] = alert_temp
        if temp >= alert_temp and not p["alert"].get("reached"):
            p["alert"]["reached"] = True
            if not retemp:
                p["alert"]["reached_at"] = tl
                out["events"].append("alert")
        elif p["alert"].get("reached") and temp < alert_temp - CFG["done_rearm_c"]:
            p["alert"] = {"temp": alert_temp, "lead_s": cfg["alert_lead_s"]}
        if p["alert"].get("reached_at") is not None:
            a["alert_reached_at"] = _iso(p["alert"]["reached_at"])

    if temp >= target or (p["done"] and temp > target - CFG["done_hyst_c"]):
        if not p["done"]:
            p["done"] = True
            p["done_at"] = tl
        out["status"] = "done"
        a["reached_at"] = _iso(p["done_at"])
        if not p["notified"].get("done"):
            p["notified"]["done"] = True
            if not retarget:
                out["events"].append("done")
        return out
    p["done"] = False
    if p["notified"].get("done") and temp < target - CFG["done_rearm_c"]:
        p["notified"]["done"] = False

    from_ = max(p["session"]["start"], p["fit_from"])
    slope_now = _slope(_held(obs, max(from_, t_obs - CFG["slope_s"])))
    a["rate"] = None if slope_now is None else _r1(slope_now * 3600)  # °C/h
    if p["moved_at"] is not None and t_obs - p["moved_at"] < CFG["win_min_s"]:
        out["status"] = "unstable"
        a["reason"] = "probe moved"
        return out
    if p["ever_predicting"] and p["gap_at"] is not None and t_obs - p["gap_at"] < CFG["win_min_s"]:
        out["status"] = "unstable"
        a["reason"] = "readings resumed"
        return out
    # The fit never spans a pit gap: simulate() would carry the pre-gap pit through it.
    fit_start = max(from_, gap_at if gap_at is not None else -math.inf)
    win = _pick_window(_held(obs, fit_start), fit_start)
    span = t_obs - win[0][0]
    lowest = math.inf
    for x in win:
        lowest = min(lowest, x[1])
    # The warm-up gate holds only until the first prediction: later a flat window is a
    # stall or a plateau to report, not a cook that has not started.
    if not p["ever_predicting"] and (
        t_obs - p["session"]["start"] < CFG["warmup_s"]
        or span < CFG["win_min_s"] / 2
        or temp - lowest < CFG["min_rise_c"]
    ):
        out["status"] = "warming"
        return out

    mid = (win[0][0] + t_obs) / 2
    s1 = _slope([x for x in win if x[0] <= mid])
    s2 = _slope([x for x in win if x[0] >= mid])
    accelerating = (
        s1 is not None and s2 is not None and s2 > 0 and (s1 <= 0 or s2 > CFG["accel_ratio"] * s1)
    )
    amb_dropped = amb is not None and amb60 is not None and amb < amb60 - CFG["amb_drop_c"]

    eta_lin = (target - temp) / slope_now if slope_now is not None and slope_now > 0 else None
    fit = None
    eta_newton = None
    if amb_ready and amb is not None and len(win) >= 5 and span >= CFG["win_min_s"] / 2:
        fit = fit_k(win, st["ambient"])
        if not fit["at_bound"] and fit["rmse"] <= CFG["fit_rmse_max_c"] and amb > target + 0.5:
            eta_newton = log((amb - temp) / (amb - target)) / fit["k"]
        a["k"] = _precision3(fit["k"])
        a["fit_rmse"] = _r2(fit["rmse"])
    a["window_min"] = _js_round(span / 60)

    # Stall: the core rises far slower than the session's best k expects at this
    # ambient-to-core gap. The best k, not a trailing one: a trailing reference slides
    # down with a gradual stall and never sees it. (How the best k is learned: below.)
    k_ref = p["k_peak"]
    ratio = (
        slope_now / (k_ref * (_num(amb) - temp))
        if k_ref and amb_ready and _num(amb) > temp and slope_now is not None
        else None
    )
    a["stall_ratio"] = _r2(ratio)
    if p["stall"] is not None:
        if ratio is not None and ratio > CFG["stall_exit"]:
            p["stall"] = None
            p["stall_cand"] = None
            # fit only the recovered stretch
            p["fit_from"] = max(p["fit_from"], t_obs - CFG["slope_s"] / 2)
            out["events"].append("stall_end")
    elif (
        ratio is not None
        and ratio < CFG["stall_enter"]
        and temp >= CFG["stall_min_temp_c"]
        and not amb_dropped
    ):
        if p["stall_cand"] is None:
            p["stall_cand"] = t_obs
        if t_obs - p["stall_cand"] >= CFG["stall_min_s"]:
            p["stall"] = {"since": p["stall_cand"], "temp": temp}
            p["stall_seen"] = True
            out["events"].append("stall")
    else:
        p["stall_cand"] = None

    eta = None
    model = None
    if amb is None:
        eta = eta_lin
        model = None if eta_lin is None else "linear"
        a["reason"] = "no ambient probe"
    elif amb <= target + 0.5:
        a["reason"] = "ambient below target"
    elif not amb_ready:
        eta = eta_lin
        model = None if eta_lin is None else "linear"
        a["reason"] = "pit readings resumed"
    elif eta_newton is not None:
        eta = eta_newton
        model = "newton"
    elif eta_lin is not None:
        eta = eta_lin
        model = "linear"
        a["reason"] = "fit failed"
    else:
        a["reason"] = "not rising"

    confident = model == "newton" and not accelerating and fit["rmse"] <= CFG["fit_rmse_ok_c"]
    if amb_dropped:
        out["status"] = "unstable"
        a["reason"] = "ambient dropped"
    elif p["stall"] is not None:
        out["status"] = "stall"
    elif not p["ever_predicting"] and (accelerating or (amb is not None and not confident)):
        out["status"] = "warming"
    else:
        out["status"] = "predicting"
    if out["status"] == "predicting":
        p["ever_predicting"] = True

    a["model"] = model
    a["confidence"] = "ok" if confident else "low"
    a["eta_newton_min"] = None if eta_newton is None else _js_round(eta_newton / 60)
    a["eta_linear_min"] = None if eta_lin is None else _js_round(eta_lin / 60)

    if out["status"] == "stall":
        a["stall_since"] = _iso(p["stall"]["since"])
        a["stall_temp"] = _r1(p["stall"]["temp"])
        if k_ref and _num(amb) > target + 0.5:
            a["eta_after_stall_min"] = _js_round(
                log((_num(amb) - temp) / (_num(amb) - target)) / k_ref / 60
            )
    elif out["status"] == "predicting" and eta is not None and math.isfinite(eta) and eta >= 0:
        # Warming keeps its model estimates as diagnostics only (eta_newton_min /
        # eta_linear_min).
        a["eta_min"] = _js_round(eta / 60)
        a["done_at"] = _iso(t_obs + eta)

    a["stall_ahead"] = (
        not p["stall_seen"]
        and out["status"] != "stall"
        and temp < CFG["stall_ahead_below_c"]
        and target > CFG["stall_ahead_target_c"]
        and amb is not None
        and amb < CFG["stall_ahead_pit_c"]
    )

    # The stall reference learns from good fits, one entry per observation time however often
    # evaluate() is called, once the core is k_learn_frac of the way from its start to the
    # pit. An empirical gate, not a physical guarantee: on the 2026-10-11 brisket the fitted k
    # fell for hours (probe 2, likely off-centre, from 1.4e-4 to 4.5e-5), and a reference
    # from the first hour made steady heating look like a stall. The share was tuned on that
    # cook and the slab sims (stalls there come 7-13 min later than with no gate). Whether an
    # ETA to the target exists does not matter here, so a changed target mid-cook does not
    # change what is learned while the target stays above the readings.
    last_k = p["k_hist"][-1] if p["k_hist"] else None
    good_fit = (
        fit is not None
        and not fit["at_bound"]
        and fit["rmse"] <= CFG["fit_rmse_ok_c"]
        and not accelerating
        and amb_ready
    )
    start_temp = p["session"]["start_temp"]
    learning = amb is not None and temp - start_temp >= CFG["k_learn_frac"] * (amb - start_temp)
    if (
        out["status"] in ("predicting", "warming")
        and good_fit
        and learning
        and p["stall_cand"] is None
        and (not last_k or t_obs > last_k[0])
    ):
        p["k_hist"].append([t_obs, fit["k"]])
        while p["k_hist"] and p["k_hist"][0][0] < t_obs - CFG["k_smooth_s"]:
            p["k_hist"].pop(0)
        k30 = _median([k for _, k in p["k_hist"]])
        if t_obs - p["k_hist"][0][0] >= CFG["k_smooth_s"] / 2:
            p["k_peak"] = max(p["k_peak"] if p["k_peak"] is not None else 0, k30)

    if out["status"] == "predicting" and a.get("eta_min") is not None:
        _heads_up(p["notified"], "approach", eta, cfg["warn_s"], confident, out, "approach")

    # Time to the second alert temperature, on the same model as the ETA.
    if (
        alert_temp is not None
        and alert_temp < target
        and not p["alert"].get("reached")
        and alert_temp > temp
        and out["status"] == "predicting"
    ):
        eta_x = None
        if model == "newton" and amb > alert_temp + 0.5:
            eta_x = log((amb - temp) / (amb - alert_temp)) / fit["k"]
        elif model == "linear" and slope_now is not None and slope_now > 0:
            eta_x = (alert_temp - temp) / slope_now
        if eta_x is not None and math.isfinite(eta_x) and eta_x >= 0:
            a["alert_eta_min"] = _js_round(eta_x / 60)
            a["alert_at"] = _iso(t_obs + eta_x)
            _heads_up(p["alert"], "soon", eta_x, cfg["alert_lead_s"], confident, out, "alert_soon")
    return out


def replay(
    st: State,
    rows: list[list[Any]],
    target_of: Callable[[str, float], float | None],
    grid_s: float,
    on_eval: Callable[[str, float, Result], None] | None = None,
    cover_end: float | None = None,
    alerts_of: Callable[[str, float], dict[str, Any] | None] | None = None,
) -> None:
    """Replay recorded rows ([probe key or "ambient", t, value], sorted by t) the way the
    live node would have seen them (a value of GAP: nothing known from t, see mark_gap;
    any other invalid value: disconnected): held-values mode, every probe evaluated on a fixed grid
    so flat stretches age too, up to the newest recorded row and no further. Alerts are not
    sent for the past; on_eval(key, t, result) sees every evaluation. Afterwards the state
    is live again and each probe waits for a real reading (await_live).

    cover_end is when the history query was made: recorded coverage runs to there, held
    values included, even if the newest row is older. Without it coverage ends at the
    newest row. The first live reading more than stale_s after coverage is a gap.
    target_of(key, t) and alerts_of(key, t) give the settings in force at evaluation time t
    (the JavaScript passed the key only), so alerts crossed in the past count as sent.
    """
    keys = _js_keys(st["probes"])
    end = max(rows[-1][1] if rows else -math.inf, cover_end if cover_end is not None else -math.inf)
    if not math.isfinite(end):
        return
    st["held_is_observed"] = True
    cursor = math.ceil(rows[0][1] / grid_s) * grid_s if rows else end
    evaluated = -math.inf

    def eval_at(t: float) -> None:
        nonlocal evaluated
        for k in keys:
            target = target_of(k, t)
            r = evaluate(st, k, t, target, alerts_of(k, t) if alerts_of is not None else None)
            if on_eval is not None:
                on_eval(k, t, r)
        evaluated = t

    for key, t, v in rows:
        while cursor < t:
            eval_at(cursor)
            cursor += grid_s
        if key == "ambient":
            if v is GAP:
                mark_ambient_gap(st, t)
                continue
            end_ambient_gap(st, t)
            ingest_ambient(st, t, v)
        elif key in st["probes"]:
            if v is GAP:
                mark_gap(st, key, t)
                continue
            end_gap(st, key, t)
            ingest_probe(st, key, t, v)
    while cursor <= end:
        eval_at(cursor)
        cursor += grid_s
    if evaluated < end:
        eval_at(end)  # the last partial interval: a crossing in it is history too
    st["held_is_observed"] = False
    st["replay_end"] = end
    for k in keys:
        if st["probes"][k]["session"] is not None:
            st["probes"][k]["await_live"] = True
    st["ambient_await_live"] = len(st["ambient"]) > 0
