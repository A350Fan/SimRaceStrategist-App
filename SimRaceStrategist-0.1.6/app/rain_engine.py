from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Deque, Optional, Tuple, List
import time
import statistics

from .f1_udp import F1LiveState
from .strategy_model import RainPitAdvice


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _median(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    try:
        return statistics.median(xs)
    except Exception:
        return None


@dataclass
class RainEngineOutput:
    advice: RainPitAdvice
    wetness: float          # 0..1
    confidence: float       # 0..1
    debug: str


class RainEngine:
    """
    Zustandsbehaftete Entscheidungslogik:
    - fused wetness score aus:
      inter_share, delta(I-S), rain_next_pct, optional baseline_loss
    - Hysterese + "hold"-Timer gegen Flackern
    """

    def __init__(
        self,
        window_s: float = 20.0,         # rolling window length
        min_samples: int = 4,           # min samples before trusting much
        on_th: float = 0.65,            # switch-to-inter threshold
        off_th: float = 0.35,           # switch-back threshold
        hold_on_updates: int = 2,       # require N consecutive updates for ON
        hold_off_updates: int = 3,      # require N consecutive updates for OFF
        # full-wet mode thresholds (Inter -> Wet)
        wet_on_th: float = 0.78,
        wet_off_th: float = 0.55,
        wet_hold_on_updates: int = 2,
        wet_hold_off_updates: int = 3,

    ):
        self.window_s = float(window_s)
        self.min_samples = int(min_samples)
        self.on_th = float(on_th)
        self.off_th = float(off_th)
        self.hold_on_updates = int(hold_on_updates)
        self.hold_off_updates = int(hold_off_updates)
        self.wet_on_th = float(wet_on_th)
        self.wet_off_th = float(wet_off_th)
        self.wet_hold_on_updates = int(wet_hold_on_updates)
        self.wet_hold_off_updates = int(wet_hold_off_updates)

        # rolling samples: (t, value)
        self._inter_share: Deque[Tuple[float, float]] = deque()
        self._delta_is: Deque[Tuple[float, float]] = deque()
        self._rain_next: Deque[Tuple[float, float]] = deque()
        self._track_temp: Deque[Tuple[float, float]] = deque()
        self._weather: Deque[Tuple[float, float]] = deque()
        self._wet_share: Deque[Tuple[float, float]] = deque()
        self._delta_wi: Deque[Tuple[float, float]] = deque()

        # hysteresis state
        self._is_wet_mode = False
        self._on_counter = 0
        self._off_counter = 0
        self._is_fullwet_mode = False
        self._wet_on_counter = 0
        self._wet_off_counter = 0


        # cache baseline pace (track, tyre) -> (t, median_pace)
        self._baseline_cache: dict[Tuple[str, str], Tuple[float, float]] = {}



    def _push(self, dq: Deque[Tuple[float, float]], t: float, v: Optional[float]):
        if v is None:
            return
        dq.append((t, float(v)))
        # prune old
        cutoff = t - self.window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def update(
        self,
        state: F1LiveState,
        *,
        track: str,
        current_tyre: str,
        laps_remaining: int,
        pit_loss_s: float,
        # DB rows from laps_for_track(track)
        db_rows: Optional[list] = None,
        your_last_lap_s: Optional[float] = None,
    ) -> RainEngineOutput:

        fc_series = getattr(state, "rain_fc_series", None) or []

        now = time.time()

        self._push(self._inter_share, now, getattr(state, "inter_share", None))
        self._push(self._delta_is, now, getattr(state, "pace_delta_inter_vs_slick_s", None))
        self._push(self._rain_next, now, getattr(state, "rain_fc_pct", None))
        self._push(self._track_temp, now, getattr(state, "track_temp_c", None))
        self._push(self._weather, now, getattr(state, "weather", None))
        self._push(self._wet_share, now, getattr(state, "wet_share", None))
        self._push(self._delta_wi, now, getattr(state, "pace_delta_wet_vs_inter_s", None))



        inter_share_med = _median([v for _, v in self._inter_share])
        delta_is_med = _median([v for _, v in self._delta_is])          # I - S (sec); negative = inter faster
        rain_next_med = _median([v for _, v in self._rain_next])        # 0..100
        track_temp_med = _median([v for _, v in self._track_temp])
        weather_med = _median([v for _, v in self._weather])
        wet_share_med = _median([v for _, v in self._wet_share])
        delta_wi_med = _median([v for _, v in self._delta_wi])          # W - I (sec); negative = wet faster

        # --- Forecast-derived features ---
        # pick a few horizons (minutes) that matter in-race
        fc_at = self._fc_window_stats(fc_series, [3, 5, 10, 15, 20])

        rain_3 = fc_at[3]
        rain_5 = fc_at[5]
        rain_10 = fc_at[10]
        rain_15 = fc_at[15]
        rain_20 = fc_at[20]

        # "drying soon" if forecast drops below ~25% within 10-15 min
        t_dry = self._fc_time_to_below(fc_series, threshold=25)
        drying_soon = (t_dry is not None and t_dry <= 15)

        # "heavy rain incoming" if forecast reaches >=60% soon
        # (use 10 min horizon, but 3/5 are useful for fast ramps)
        t_heavy = self._fc_time_to_above(fc_series, threshold=60)
        heavy_incoming = (t_heavy is not None and t_heavy <= 10)

        # --- Baseline: expected slick pace (minimal) ---
        expected_pace = None
        if db_rows is not None and track and current_tyre:
            expected_pace = self._expected_pace_from_rows(track, current_tyre, db_rows)

        baseline_loss = None
        if expected_pace is not None and your_last_lap_s is not None:
            baseline_loss = float(your_last_lap_s) - float(expected_pace)

        # --- Scoring ---
        # s0: weather enum (hard hint; helps early wetness before deltas exist)
        s0 = None
        if weather_med is not None:
            w = int(weather_med)
            # 0 clear, 1 light cloud, 2 overcast, 3 light rain, 4 heavy rain, 5 storm
            if w <= 2:
                s0 = 0.0
            elif w == 3:
                s0 = 0.45
            elif w == 4:
                s0 = 0.80
            else:  # 5
                s0 = 0.95

        # s1: field share
        # start caring at ~15%, strong at ~50%
        s1 = None
        if inter_share_med is not None:
            s1 = _clamp01((inter_share_med - 0.15) / 0.35)

        # s2: delta I-S (strongest)
        # if delta_is <= -0.5 (inter faster by 0.5s) -> strong wetness
        s2 = None
        if delta_is_med is not None:
            # map: delta_is_med = +2s -> 0, delta_is_med = 0 -> ~0.2, delta_is_med = -0.5 -> ~0.5, delta_is_med = -2.5 -> 1
            s2 = _clamp01(((-delta_is_med) - 0.5) / 2.0)

        # s3: forecast
        s3 = None
        if rain_next_med is not None:
            s3 = _clamp01((rain_next_med - 35.0) / 35.0)

        # s4: your baseline loss (optional)
        s4 = None
        if baseline_loss is not None:
            s4 = _clamp01((baseline_loss - 0.7) / 2.0)

        # temperature modifier (optional): colder track => earlier switch
        temp_boost = 0.0
        if track_temp_med is not None:
            # below ~22C slightly more slippery in drizzle, cap boost
            temp_boost = _clamp01((22.0 - track_temp_med) / 18.0) * 0.08  # max +0.08

        # Weighted fusion (ignore missing signals gracefully)
        parts = []
        weights = []

        def add(sig: Optional[float], w: float):
            if sig is None:
                return
            parts.append(sig)
            weights.append(w)

        add(s0, 0.30)
        add(s2, 0.35)
        add(s1, 0.25)
        add(s3, 0.20)
        add(s4, 0.20)

        if parts and weights:
            wsum = sum(weights)
            wetness = sum(p * w for p, w in zip(parts, weights)) / max(1e-9, wsum)
        else:
            wetness = 0.0

        wetness = _clamp01(wetness + temp_boost)

        # hard floor: if game says it's raining, wetness must not be below that hint
        if s0 is not None:
            wetness = max(wetness, float(s0))

        # --- Separate "full wet" score (Inter -> Wet) ---
        fw0 = None
        if weather_med is not None:
            w = int(weather_med)
            if w <= 3:
                fw0 = 0.0
            elif w == 4:
                fw0 = 0.75
            else:
                fw0 = 0.95

        fw1 = None
        if wet_share_med is not None:
            fw1 = _clamp01((wet_share_med - 0.05) / 0.25)

        fw2 = None
        if delta_wi_med is not None:
            fw2 = _clamp01(((-delta_wi_med) - 0.20) / 1.30)

        fw3 = None
        if rain_next_med is not None:
            fw3 = _clamp01((rain_next_med - 60.0) / 30.0)

        fw_parts, fw_weights = [], []

        def fw_add(sig: Optional[float], w: float):
            if sig is None:
                return
            fw_parts.append(sig)
            fw_weights.append(w)

        fw_add(fw0, 0.35)
        fw_add(fw2, 0.35)
        fw_add(fw1, 0.25)
        fw_add(fw3, 0.20)

        if fw_parts and fw_weights:
            fw_wsum = sum(fw_weights)
            fullwet = sum(p * w for p, w in zip(fw_parts, fw_weights)) / max(1e-9, fw_wsum)
        else:
            fullwet = 0.0

        if fw0 is not None:
            fullwet = max(fullwet, float(fw0) * 0.85)
        fullwet = _clamp01(fullwet)

        if heavy_incoming:
            fullwet = min(1.0, fullwet + 0.10)

        wet_score = fullwet  # used later in advice/debug

        # Confidence: more signals + enough samples -> higher confidence
        n_signals = sum(x is not None for x in (s1, s2, s3, s4))
        n_samples = len(self._rain_next) + len(self._delta_is) + len(self._inter_share)
        conf = _clamp01(0.15 + 0.20 * n_signals + 0.15 * _clamp01(n_samples / (self.min_samples * 3)))

        # SC/VSC: allow earlier pit call
        sc = getattr(state, "safety_car_status", None)
        under_sc = sc in (1, 2)
        if under_sc:
            wetness = _clamp01(wetness + 0.06)
            conf = _clamp01(conf + 0.05)

        # --- Hysteresis mode ---
        # define "wet-mode" = inter recommended
        if wetness >= self.on_th:
            self._on_counter += 1
            self._off_counter = 0
        elif wetness <= self.off_th:
            self._off_counter += 1
            self._on_counter = 0
        else:
            # in between: decay counters slightly
            self._on_counter = max(0, self._on_counter - 1)
            self._off_counter = max(0, self._off_counter - 1)

        if not self._is_wet_mode and self._on_counter >= self.hold_on_updates:
            self._is_wet_mode = True
        if self._is_wet_mode and self._off_counter >= self.hold_off_updates:
            self._is_wet_mode = False

        # --- Hysteresis: full-wet-mode (Wet recommended) ---
        if self._is_wet_mode:
            if fullwet >= self.wet_on_th:
                self._wet_on_counter += 1
                self._wet_off_counter = 0
            elif fullwet <= self.wet_off_th:
                self._wet_off_counter += 1
                self._wet_on_counter = 0
            else:
                self._wet_on_counter = max(0, self._wet_on_counter - 1)
                self._wet_off_counter = max(0, self._wet_off_counter - 1)

            if not self._is_fullwet_mode and self._wet_on_counter >= self.wet_hold_on_updates:
                self._is_fullwet_mode = True
            if self._is_fullwet_mode and self._wet_off_counter >= self.wet_hold_off_updates:
                self._is_fullwet_mode = False
        else:
            self._is_fullwet_mode = False
            self._wet_on_counter = 0
            self._wet_off_counter = 0

        # --- Advice ---
        tyre = (current_tyre or "").strip().upper()
        lr = max(0, int(laps_remaining))

        def stay(reason: str) -> RainPitAdvice:
            return RainPitAdvice("STAY OUT", None, None, reason)

        def box_in(n: int, target: str, reason: str) -> RainPitAdvice:
            n = max(1, int(n))
            return RainPitAdvice(f"BOX IN {n}", target, n, reason)

        if lr <= 1:
            advice = stay("≤1 lap remaining.")
        else:
            # Decision: Slick <-> Inter, plus Inter <-> Wet.
            is_slick = tyre.startswith("C") or tyre in ("SLICK", "DRY")
            is_inter = ("INTER" in tyre) or (tyre == "INTERMEDIATE") or (tyre == "INTER")
            is_wet = ("WET" in tyre)

            if is_slick:
                if self._is_wet_mode:
                    if delta_is_med is not None and delta_is_med < -0.30:
                        advice = box_in(1, "Intermediate", "Δpace(I-S) says Inter already faster.")
                    else:
                        n = 1 if wetness > 0.80 else 2
                        if under_sc:
                            n = 1
                        advice = box_in(n, "Intermediate", "Wetness trend suggests switching to Inter.")
                else:
                    advice = stay("Wetness not high enough for Inter.")
            else:
                # you are on Inter/Wet: decide between Wet, Inter, or back to slick
                if is_inter and self._is_fullwet_mode:
                    if delta_wi_med is not None and delta_wi_med < -0.20:
                        advice = box_in(1, "Wet", "Δpace(W-I) says Wet already faster.")
                    else:
                        n = 1 if wet_score > 0.85 else 2
                        if under_sc:
                            n = 1
                        advice = box_in(n, "Wet", "Rain intensity suggests switching to Full Wet.")
                elif is_wet and (not self._is_fullwet_mode) and self._is_wet_mode:
                    advice = box_in(1, "Intermediate", "Conditions easing: Wet-mode yes, Full-wet-mode no.")
                else:
                    if not self._is_wet_mode and not (weather_med is not None and int(weather_med) >= 3):
                        if (rain_next_med is not None and rain_next_med < 25.0) and (
                                inter_share_med is not None and inter_share_med < 0.20):
                            advice = box_in(1, "C4", "Drying: low rain forecast + low Inter/Wet share.")
                        else:
                            advice = box_in(2, "C4", "Drying trend suggests slick soon.")
                    else:
                        if is_wet:
                            advice = stay("Stay on Wet: wet-mode still active.")
                        else:
                            advice = stay("Stay on Inter: wet-mode still active.")

                if drying_soon and not under_sc and lr > 3:
                    advice = stay("Forecast: drying soon → avoid unnecessary tyre refresh.")

                if is_slick and self._is_wet_mode and drying_soon and (wetness < 0.80) and not under_sc:
                    advice = stay("Forecast: rain phase short → try to stay out on slick.")

        dbg = (
            f"wetness={wetness:.2f} conf={conf:.2f} mode={'INTER' if self._is_wet_mode else 'DRY'} "
            f"fullwet={'ON' if self._is_fullwet_mode else 'OFF'} wetScore={wet_score:.2f} | "
            f"share(I+W)={None if inter_share_med is None else round(inter_share_med, 3)} "
            f"share(W)={None if wet_share_med is None else round(wet_share_med, 3)} "
            f"ΔI-S={None if delta_is_med is None else round(delta_is_med, 2)} "
            f"ΔW-I={None if delta_wi_med is None else round(delta_wi_med, 2)} "
            f"rainNext={None if rain_next_med is None else round(rain_next_med, 1)} "
            f"trackT={None if track_temp_med is None else round(track_temp_med, 1)} "
            f"baseLoss={None if baseline_loss is None else round(baseline_loss, 2)}"
        )

        return RainEngineOutput(advice=advice, wetness=wetness, confidence=conf, debug=dbg)

    def _fc_value_at(self, fc_series, t_min: int) -> Optional[int]:
        """Return rain% at/after t_min using nearest sample >= t_min (stepwise)."""
        if not fc_series:
            return None
        for t, r, _w in fc_series:
            if t >= t_min:
                return int(r)
        return int(fc_series[-1][1])  # beyond horizon: last known

    def _fc_window_stats(self, fc_series, mins: list[int]) -> dict[int, Optional[int]]:
        return {m: self._fc_value_at(fc_series, m) for m in mins}

    def _fc_time_to_below(self, fc_series, threshold: int) -> Optional[int]:
        """First minute where rain <= threshold."""
        if not fc_series:
            return None
        for t, r, _w in fc_series:
            if int(r) <= threshold:
                return int(t)
        return None

    def _fc_time_to_above(self, fc_series, threshold: int) -> Optional[int]:
        """First minute where rain >= threshold."""
        if not fc_series:
            return None
        for t, r, _w in fc_series:
            if int(r) >= threshold:
                return int(t)
        return None

    def _expected_pace_from_rows(self, track: str, tyre: str, rows: list) -> Optional[float]:
        """
        rows = laps_for_track(track) tuples:
        (created_at, session, track, tyre, weather, lap_time_s, fuel_load, wear_fl, wear_fr, wear_rl, wear_rr)
        """
        key = (track.strip(), tyre.strip().upper())
        now = time.time()
        cached = self._baseline_cache.get(key)
        if cached and (now - cached[0]) < 10.0:  # refresh max every 10s
            return cached[1]

        t = tyre.strip().upper()
        times: List[float] = []
        for r in rows:
            try:
                r_tyre = str(r[3]).strip().upper()
                lap_time = float(r[5])
            except Exception:
                continue

            if r_tyre != t:
                continue

            # ignore obviously broken laps
            if lap_time <= 10.0 or lap_time >= 400.0:
                continue
            times.append(lap_time)

        med = _median(times)
        if med is not None:
            self._baseline_cache[key] = (now, med)
        return med
