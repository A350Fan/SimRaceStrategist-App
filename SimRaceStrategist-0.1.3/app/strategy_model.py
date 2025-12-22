from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
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


def _parse_dt(s: str) -> Optional[dt.datetime]:
    # created_at is sqlite datetime('now') => 'YYYY-MM-DD HH:MM:SS'
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _wear_avg(row: LapRow) -> Optional[float]:
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

            # wear should generally decrease over laps (remaining % goes down).
            # If it increases notably -> likely new tyres/pit/reset.
            if last_wear is not None and (w - last_wear) > 2.0:
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


def estimate_degradation_for_track_tyre(
    rows: List[LapRow],
    track: str,
    tyre: str,
    wear_threshold: float = 70.0
) -> DegradationEstimate:
    # filter
    filt = [r for r in rows if (r.track == track and r.tyre == tyre and r.session in ("P", "R"))]
    stints = build_stints(filt)

    # flatten stint-to-stint deltas
    wear_deltas = []
    pace_vs_wear = []  # (wear_avg, lap_time_s)

    for stint in stints:
        # wear per lap: deltas between consecutive laps (remaining % goes down)
        for a, b in zip(stint, stint[1:]):
            dw = (a.wear_avg - b.wear_avg)  # positive if remaining decreased
            if 0.0 < dw < 10.0:  # sanity
                wear_deltas.append(dw)

        for p in stint:
            pace_vs_wear.append((p.wear_avg, p.lap_time_s))

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
    x = np.array([100.0 - w for (w, t) in pace_vs_wear], dtype=float)
    y = np.array([t for (w, t) in pace_vs_wear], dtype=float)

    # robust-ish: simple linear fit
    b, a = np.polyfit(x, y, 1)  # y = b*x + a
    pace_loss_per_pct = float(max(0.0, b))

    # predict laps to threshold from last wear in latest stint (best effort)
    last_wears = [st[-1].wear_avg for st in stints if len(st) > 0]
    current_wear = float(np.median(last_wears)) if last_wears else None
    laps_to_thr = None
    if current_wear is not None and wear_per_lap > 0.0:
        if current_wear > wear_threshold:
            laps_to_thr = (current_wear - wear_threshold) / wear_per_lap
        else:
            laps_to_thr = 0.0

    return DegradationEstimate(
        n_laps_used=len(pace_vs_wear),
        wear_per_lap_pct=wear_per_lap,
        pace_loss_per_pct_s=pace_loss_per_pct,
        predicted_laps_to_threshold=laps_to_thr,
        notes=f"Built from {len(stints)} stint(s)."
    )
