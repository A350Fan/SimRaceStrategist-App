from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
import math
import datetime as dt

import numpy as np


@dataclass
class LapRow:
    created_at: str
    session: str
    track: str
    tyre: str
    weather: str
    lap_time_s: Optional[float]
    fuel_load: Optional[float]
    wear_fl: Optional[float]
    wear_fr: Optional[float]
    wear_rl: Optional[float]
    wear_rr: Optional[float]


@dataclass
class StintPoint:
    lap_idx: int
    t: dt.datetime
    lap_time_s: float
    wear_avg: float


@dataclass
class DegradationEstimate:
    n_laps_used: int
    wear_per_lap_pct: float           # positive: % remaining lost per lap
    pace_loss_per_pct_s: float        # seconds per 1% wear remaining lost
    predicted_laps_to_threshold: Optional[float]  # from current wear to threshold
    notes: str
    max_stint_from_fresh_laps: Optional[float] = None

def normalize_tyre(t: str) -> str:
    if not t:
        return ""
    s = t.strip()
    su = s.upper()

    # Rain compounds aus CSV -> UI-Enum
    if su == "INTERMEDIATE":
        return "INTER"
    if su == "WET":
        return "WET"

    # Slicks: C1..C6 unverändert (egal ob groß/klein)
    if su in ("C1", "C2", "C3", "C4", "C5", "C6"):
        return su

    # fallback
    return su



def _parse_dt(s: str) -> Optional[dt.datetime]:
    # created_at is sqlite datetime('now') => 'YYYY-MM-DD HH:MM:SS'
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

# FUTURE/WIP: Helper for averaging per-corner tyre wear (FL/FR/RL/RR).
# Currently unused in the project, but useful for future stint-quality / degradation heuristics.
def _wear_avg_off(row: LapRow) -> Optional[float]:
    vals = [row.wear_fl, row.wear_fr, row.wear_rl, row.wear_rr]
    vals = [v for v in vals if isinstance(v, (int, float)) and not math.isnan(v)]
    if len(vals) < 2:
        return None
    return float(sum(vals) / len(vals))


def build_stints(rows: List[LapRow], max_gap_min: int = 12) -> List[List[StintPoint]]:
    """
    Heuristic stint builder:
    - sort by time
    - same track + tyre required
    - new stint if wear jumps UP (reset) or time gap too large or lap_time missing
    """
    rows = [r for r in rows if r.lap_time_s is not None]
    rows = sorted(rows, key=lambda r: r.created_at)

    stints: List[List[StintPoint]] = []
    current: List[StintPoint] = []

    last_track = None
    last_tyre = None
    last_time: Optional[dt.datetime] = None
    last_wear: Optional[float] = None

    for i, r in enumerate(rows):
        t = _parse_dt(r.created_at)
        if t is None:
            continue

        w = _wear_avg(r)
        if w is None:
            continue

        # only learn from Practice/Race
        if r.session not in ("P", "R"):
            continue

        # decide if stint breaks
        new_stint = False
        if last_track is None:
            new_stint = True
        else:
            if r.track != last_track or r.tyre != last_tyre:
                new_stint = True

            if last_time is not None:
                gap = (t - last_time).total_seconds() / 60.0
                if gap > max_gap_min:
                    new_stint = True

            # Tyre wear [%] should generally INCREASE over a stint (0=new -> higher=more worn).
            # If it DECREASES notably -> likely new tyres / pit stop / reset.
            if last_wear is not None and (last_wear - w) > 2.0:
                new_stint = True

        if new_stint:
            if len(current) >= 3:
                stints.append(current)
            current = []

        current.append(StintPoint(lap_idx=i, t=t, lap_time_s=float(r.lap_time_s), wear_avg=w))

        last_track = r.track
        last_tyre = r.tyre
        last_time = t
        last_wear = w

    if len(current) >= 3:
        stints.append(current)

    return stints

def mark_in_outlaps_in_points(points: list[StintPoint],
                              wear_drop_thr: float = 2.0,
                              outlier_sec: float = 2.5) -> list[dict]:
    """
    Like mark_in_outlaps_in_stint(), but works on StintPoint (already has wear_avg).
    Returns list of dicts:
      wear_avg, lap_time_s, inlap, outlap, clean
    """
    if not points:
        return []

    w = [p.wear_avg for p in points]

    times = sorted([p.lap_time_s for p in points if p.lap_time_s is not None])
    med = times[len(times) // 2] if times else None

    inlap = [False] * len(points)
    outlap = [False] * len(points)

    # Wear% normally INCREASES; a big DROP indicates pit/new tyres/reset
    for i in range(1, len(points)):
        if (w[i-1] - w[i]) > wear_drop_thr:
            inlap[i-1] = True
            outlap[i] = True

    clean = [True] * len(points)
    lap_tag = ["OK"] * len(points)

    if med is not None:
        for i, p in enumerate(points):
            if inlap[i]:
                clean[i] = False
                lap_tag[i] = "IN"
                continue
            if outlap[i]:
                clean[i] = False
                lap_tag[i] = "OUT"
                continue

            if p.lap_time_s is not None and p.lap_time_s > (med + outlier_sec):
                clean[i] = False
                lap_tag[i] = "SLOW"

    out = []
    for i, p in enumerate(points):
        out.append({
            "wear_avg": w[i],
            "lap_time_s": p.lap_time_s,
            "inlap": inlap[i],
            "outlap": outlap[i],
            "clean": clean[i],
            "lap_tag": lap_tag[i],
        })

    return out

def _wear_avg(l: "LapRow") -> float:
    vals = [l.wear_fl, l.wear_fr, l.wear_rl, l.wear_rr]
    vals = [v for v in vals if v is not None]
    return float(sum(vals) / len(vals)) if vals else 0.0

def mark_in_outlaps_in_stint(laps: list["LapRow"], wear_drop_thr: float = 2.0,
                             outlier_sec: float = 2.5) -> list[dict]:
    """
    Returns list of dicts with:
      lap: LapRow
      wear_avg: float
      inlap: bool
      outlap: bool
      clean: bool
    """

    if not laps:
        return []

    # Precompute wear avg
    w = [_wear_avg(x) for x in laps]

    # Median lap time (robust baseline)
    times = sorted([x.lap_time_s for x in laps if x.lap_time_s is not None])
    med = times[len(times)//2] if times else None

    inlap = [False] * len(laps)
    outlap = [False] * len(laps)

    # 1) Detect pit/reset via WEAR DROP (wear% should usually increase; a drop means new tyres/reset)
    for i in range(1, len(laps)):
        if (w[i-1] - w[i]) > wear_drop_thr:
            # lap i-1 is inlap, lap i is outlap
            inlap[i-1] = True
            outlap[i] = True

    # 2) Fallback: detect outliers by lap time (only if we have median)
    # WIP: Hook for future classification of "slow laps" (e.g. yellow flags, traffic, mistakes).
    # Currently, we DON'T force in/out laps here; we only later exclude slow outliers from "clean" laps.
    if med is not None:
        for i, lap in enumerate(laps):
            if lap.lap_time_s is None:
                continue
            if lap.lap_time_s > (med + outlier_sec):
                # If we already know it's in/out -> keep that.
                # Otherwise: reserved for future tagging (kept intentionally empty).
                pass

    # Clean lap definition: not inlap/outlap and not a big slow outlier
    clean = [True] * len(laps)
    if med is not None:
        for i, lap in enumerate(laps):
            if inlap[i] or outlap[i]:
                clean[i] = False
                continue
            if lap.lap_time_s is not None and lap.lap_time_s > (med + outlier_sec):
                clean[i] = False

    out = []
    for i, lap in enumerate(laps):
        out.append({
            "lap": lap,
            "wear_avg": w[i],
            "inlap": inlap[i],
            "outlap": outlap[i],
            "clean": clean[i],
        })
    return out

def estimate_degradation_for_track_tyre(
    rows: List[LapRow],
    track: str,
    tyre: str,
    wear_threshold: float = 70.0
) -> DegradationEstimate:
    # filter
    tyre_norm = normalize_tyre(tyre)

    filt = [
        r for r in rows
        if (
                r.track == track
                and normalize_tyre(r.tyre) == tyre_norm
                and r.session in ("P", "R")
        )
    ]

    if not filt:
        return DegradationEstimate(
            n_laps_used=0,
            wear_per_lap_pct=0.0,
            pace_loss_per_pct_s=0.0,
            predicted_laps_to_threshold=None,
            notes=f"No data for track={track}, tyre={tyre_norm}."
        )

    stints = build_stints(filt)
    if not stints:
        return DegradationEstimate(
            n_laps_used=0,
            wear_per_lap_pct=0.0,
            pace_loss_per_pct_s=0.0,
            predicted_laps_to_threshold=None,
            notes="No stints could be built from filtered laps."
        )

    # flatten stint-to-stint deltas
    wear_deltas = []
    pace_vs_wear = []  # (wear_avg, lap_time_s)

    for stint in stints:
        marked = mark_in_outlaps_in_points(stint, wear_drop_thr=2.0, outlier_sec=2.5)

    # wear per lap: only between CLEAN consecutive laps (and not across a wear drop reset)
    for a, b in zip(marked, marked[1:]):
        if not a["clean"] or not b["clean"]:
            continue
        dw = b["wear_avg"] - a["wear_avg"]  # wear% increase per lap
        if 0.0 < dw < 10.0:
            wear_deltas.append(dw)

    # pace fit: only CLEAN laps
    for m in marked:
        if not m["clean"]:
            continue
        pace_vs_wear.append((m["wear_avg"], m["lap_time_s"]))

    if len(wear_deltas) < 3 or len(pace_vs_wear) < 6:
        return DegradationEstimate(
            n_laps_used=len(pace_vs_wear),
            wear_per_lap_pct=0.0,
            pace_loss_per_pct_s=0.0,
            predicted_laps_to_threshold=None,
            notes="Not enough data yet (need a few consecutive laps on same compound)."
        )

    wear_per_lap = float(np.median(wear_deltas))

    # pace loss per 1% wear lost:
    # We model lap_time = a + b*(100 - wear_avg) so b is seconds per 1% wear lost
    x = np.array([w for (w, t) in pace_vs_wear], dtype=float) #wear%
    y = np.array([t for (w, t) in pace_vs_wear], dtype=float)

    # robust-ish: simple linear fit
    b, a = np.polyfit(x, y, 1)  # y = b*x + a
    pace_loss_per_pct = float(max(0.0, b))

    # predict laps to threshold from last wear in latest stint (best effort)
    last_wears = [st[-1].wear_avg for st in stints if len(st) > 0]
    current_wear = float(np.median(last_wears)) if last_wears else None
    laps_to_thr = None
    if current_wear is not None and wear_per_lap > 0.0:
        if current_wear < wear_threshold:
            laps_to_thr = (wear_threshold - current_wear) / wear_per_lap
        else:
            laps_to_thr = 0.0

    max_from_fresh = None
    if wear_per_lap > 0.0:
        max_from_fresh = (wear_threshold) / wear_per_lap
        max_from_fresh = max(0.0, max_from_fresh)


    return DegradationEstimate(
        n_laps_used=len(pace_vs_wear),
        wear_per_lap_pct=wear_per_lap,
        pace_loss_per_pct_s=pace_loss_per_pct,
        predicted_laps_to_threshold=laps_to_thr,
        notes=f"Built from {len(stints)} stint(s).",
        max_stint_from_fresh_laps=max_from_fresh
    )

def pit_window_one_stop(race_laps: int, max_stint_laps: float, min_stint_laps: int = 5):
    """
    Returns (earliest_lap, latest_lap) for a 1-stop race such that:
      - first stint <= max_stint_laps
      - second stint <= max_stint_laps
      - both stints >= min_stint_laps (to avoid nonsense)
    """
    if race_laps <= 0 or max_stint_laps <= 0:
        return None

    max_stint_int = int(max_stint_laps)  # conservative: floor
    earliest = max(min_stint_laps, race_laps - max_stint_int)
    latest = min(max_stint_int, race_laps - min_stint_laps)

    if earliest > latest:
        return None
    return earliest, latest

def pit_windows_two_stop(race_laps: int, max_stint_laps: float, min_stint_laps: int = 5):
    """
    Returns 2-stop windows as:
      (stop1_earliest, stop1_latest, stop2_earliest, stop2_latest)
    where stop1 is the lap you pit first, stop2 is the lap you pit second.

    Conditions:
      - 3 stints: a=stop1, b=stop2-stop1, c=race_laps-stop2
      - each stint in [min_stint_laps, floor(max_stint_laps)]
    """
    if race_laps <= 0 or max_stint_laps <= 0:
        return None

    max_int = int(max_stint_laps)  # conservative
    if max_int < min_stint_laps:
        return None

    feasible = []

    # stop1 must leave room for 2 more stints (min each)
    stop1_min = min_stint_laps
    stop1_max = min(max_int, race_laps - 2 * min_stint_laps)

    for s1 in range(stop1_min, stop1_max + 1):
        # stop2 must be at least min after stop1 and leave min for last stint
        s2_min = s1 + min_stint_laps
        s2_max = min(s1 + max_int, race_laps - min_stint_laps)

        for s2 in range(s2_min, s2_max + 1):
            a = s1
            b = s2 - s1
            c = race_laps - s2

            if b < min_stint_laps or b > max_int:
                continue
            if c < min_stint_laps or c > max_int:
                continue

            feasible.append((s1, s2))

    if not feasible:
        return None

    s1_earliest = min(x[0] for x in feasible)
    s1_latest   = max(x[0] for x in feasible)
    s2_earliest = min(x[1] for x in feasible)
    s2_latest   = max(x[1] for x in feasible)

    return (s1_earliest, s1_latest, s2_earliest, s2_latest)

@dataclass
class RainPitAdvice:
    action: str              # "BOX NOW", "BOX IN N", "STAY OUT"
    target_tyre: Optional[str]
    laps_until: Optional[int]
    reason: str


def recommend_rain_pit(
    current_tyre: str,
    rain_next_pct: float,
    laps_remaining: int,
    pit_loss_s: float,
    # thresholds (tunable)
    slick_to_inter_on: float = 50.0,
    inter_to_wet_on: float = 80.0,
    inter_to_slick_off: float = 30.0,
    wet_to_inter_off: float = 65.0,
    # how many laps of "lead time" we allow before calling it
    lead_laps: int = 2,
) -> RainPitAdvice:
    """
    Pure threshold-based advice using rain_next_pct (0..100).
    Hysteresis avoids flapping.
    """

    t = (current_tyre or "").upper().strip()
    rn = max(0.0, min(100.0, float(rain_next_pct)))
    lr = max(0, int(laps_remaining))

    def box_now(target: str, why: str):
        return RainPitAdvice("BOX NOW", target, 0, why)

    def box_in(n: int, target: str, why: str):
        n = max(1, int(n))
        return RainPitAdvice(f"BOX IN {n}", target, n, why)

    def stay(why: str):
        return RainPitAdvice("STAY OUT", None, None, why)

    # If race is basically over, don't do weird things
    if lr <= 1:
        return stay("≤1 lap remaining.")

    # SLICKs (C1..C6)
    if t.startswith("C") or t in ("SLICK", "DRY"):
        if rn >= slick_to_inter_on:
            # if it's already clearly coming, box now; otherwise "box in"
            if rn >= slick_to_inter_on + 10:
                return box_now("INTER", f"Rain(next)={rn:.0f}% ≥ {slick_to_inter_on:.0f}%.")
            return box_in(min(lead_laps, lr-1), "INTER", f"Rain(next) trending up ({rn:.0f}%).")
        return stay(f"Rain(next)={rn:.0f}% below Inter trigger ({slick_to_inter_on:.0f}%).")

    # INTERs
    if "INTER" in t or t in ("I",):
        if rn >= inter_to_wet_on:
            return box_now("WET", f"Rain(next)={rn:.0f}% ≥ {inter_to_wet_on:.0f}%.")
        if rn <= inter_to_slick_off:
            return box_now("SLICK", f"Rain(next)={rn:.0f}% ≤ {inter_to_slick_off:.0f}%.")
        return stay(f"Rain(next)={rn:.0f}% in Inter band.")

    # WETs
    if "WET" in t or t in ("W",):
        if rn <= wet_to_inter_off:
            return box_now("INTER", f"Rain(next)={rn:.0f}% ≤ {wet_to_inter_off:.0f}%.")
        return stay(f"Rain(next)={rn:.0f}% still high.")

    # Unknown tyre label
    return stay(f"Unknown tyre '{current_tyre}'.")
