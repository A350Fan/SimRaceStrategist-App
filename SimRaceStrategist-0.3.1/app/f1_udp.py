from __future__ import annotations
import socket
import statistics
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from collections import deque


@dataclass
class F1LiveState:
    safety_car_status: Optional[int] = None  # 0 none, 1 SC, 2 VSC, 3 formation
    weather: Optional[int] = None            # enum (best-effort)

    rain_now_pct: Optional[int] = None       # 0..100 (current rain)
    rain_fc_pct: Optional[int] = None        # 0..100 (forecast next sample)

    # Forecast samples: list of (time_offset_min, rain_pct, weather_enum)
    rain_fc_series: Optional[list[tuple[int, int, int]]] = None

    track_temp_c: Optional[float] = None
    air_temp_c: Optional[float] = None

    # optional: einfache Trends (aus letzten n Samples)
    track_temp_trend_c_per_min: Optional[float] = None
    rain_trend_pct_per_min: Optional[float] = None

    # NOTE: you assign this as str in the listener; keep it consistent with DB (TEXT).
    session_uid: Optional[str] = None

    # Player meta (you already set these in _update_field_metrics_and_emit)
    player_car_index: Optional[int] = None
    player_tyre_cat: Optional[str] = None   # "SLICK" / "INTER" / "WET"

    # NOTE: inter_share bleibt aus Kompatibilitätsgründen = Anteil (INTER+WET) von (SLICK+INTER+WET)
    inter_share: Optional[float] = None
    # Neue, getrennte Werte (Inter vs Wet)
    inter_only_share: Optional[float] = None
    wet_share: Optional[float] = None

    pace_delta_inter_vs_slick_s: Optional[float] = None
    # Neue, getrennte Pace-Deltas (Field)
    pace_delta_wet_vs_inter_s: Optional[float] = None
    pace_delta_wet_vs_slick_s: Optional[float] = None

    # NOTE: inter_count bleibt aus Kompatibilitätsgründen = Anzahl (INTER+WET)
    inter_count: Optional[int] = None
    inter_only_count: Optional[int] = None
    wet_count: Optional[int] = None
    slick_count: Optional[int] = None

    # --- Your (player) learned reference deltas ---
    your_delta_inter_vs_slick_s: Optional[float] = None
    your_delta_wet_vs_slick_s: Optional[float] = None
    your_delta_wet_vs_inter_s: Optional[float] = None
    your_ref_counts: Optional[str] = None  # z.B. "S:3 I:2 W:0"




def _read_header(data: bytes):
    # F1 25 header is 29 bytes:
    # <HBBBBBQfIIBB
    if len(data) < 29:
        return None
    try:
        u = struct.unpack_from("<HBBBBBQfIIBB", data, 0)
        return {
            "packetFormat": u[0],
            "gameYear": u[1],
            "packetId": u[5],
            "sessionUID": u[6],
            "playerCarIndex": u[10],
            "headerSize": 29,
        }
    except Exception:
        return None




class _Debounce:
    """Only accept a value if it stays the same for N updates or T seconds."""
    def __init__(self, n: int = 5, max_age_s: float = 1.0):
        self.n = n
        self.max_age_s = max_age_s
        self._candidate = None
        self._count = 0
        self._t0 = 0.0

    def update(self, value):
        now = time.time()
        if value != self._candidate:
            self._candidate = value
            self._count = 1
            self._t0 = now
            return None

        self._count += 1
        if self._count >= self.n or (now - self._t0) >= self.max_age_s:
            return self._candidate
        return None


class F1UDPListener:
    def __init__(self, port: int, on_state: Callable[[F1LiveState], None], *, debug: bool = True):
        self.port = port
        self.on_state = on_state
        self.debug = bool(debug)
        self.state = F1LiveState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._deb_sc = _Debounce(n=6, max_age_s=0.7)
        self._deb_weather = _Debounce(n=6, max_age_s=0.7)
        self._deb_rain_now = _Debounce(n=6, max_age_s=0.7)
        self._deb_rain_fc = _Debounce(n=6, max_age_s=0.7)

        self._last_lap_ms = [None] * 22
        self._tyre_cat = [None] * 22  # "SLICK" / "INTER" / "WET"

        self._emit_interval_s = 0.5  # 2 Hz

        # Outlap-Erkennung: nur wenn Lap deutlich langsamer als vorherige ist
        self._outlap_slow_ms = 8000  # 8s langsamer als vorherige Lap => sehr wahrscheinlich Outlap


        # --- Outlier filter (your reference laps) ---
        self._your_outlier_sec = 2.5      # akzeptiere nur ±2.5s um Median
        self._your_outlier_min_n = 3      # erst ab 3 vorhandenen Laps filtern
        self._your_lap_min_s = 20.0       # harte Plausi-Grenzen
        self._your_lap_max_s = 400.0

        self._last_emit_t = 0.0
        self._dirty = False  # merken: es gab neue Daten seit letztem Emit
        self._dirty_session = False
        self._tyre_last_seen = [0.0] * 22
        self._tyre_timeout_s = 2.5
        self._pit_status = [0] * 22  # 0 none, 1 pitting, 2 in pit area
        self._pit_cycle = [0] * 22  # 0 none, 1 saw pit start, 2 expect outlap
        self._pending_tyre = [None] * 22  # Reifenwahl während Pit (wird erst beim Exit übernommen)

        self._tyre_actual = [None] * 22
        self._tyre_visual = [None] * 22

        # --- Lap quality ---
        self._ignore_next_lap = [False] * 22   # True => nächste LapTime wird verworfen (Outlap nach Reifenwechsel)
        self._last_tyre_cat = [None] * 22      # Merken, ob Reifenklasse gewechselt hat
        self._lap_valid = [True] * 22          # Valid-Flag für "letzte Runde" pro Auto

        # --- Player tracking ---
        self._player_idx: Optional[int] = None
        self._session_uid: Optional[int] = None
        self._last_session_uid: Optional[int] = None

        # last N laps for YOU per tyre category (seconds)
        self._your_laps = {
            "SLICK": deque(maxlen=5),
            "INTER": deque(maxlen=5),
            "WET": deque(maxlen=5),
        }

        # rolling lap history per car and tyre cat (seconds)
        self._car_laps = [
            {
                "SLICK": deque(maxlen=5),
                "INTER": deque(maxlen=5),
                "WET": deque(maxlen=5),
            }
            for _ in range(22)
        ]

        self._lap_flag = ["OK"] * 22

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.settimeout(0.5)
        
        while not self._stop.is_set():
            try:
                data, _addr = sock.recvfrom(2048)
                # DEBUG: zeigen ob überhaupt UDP ankommt
                if self.debug:
                    print("RX", len(data))

            except socket.timeout:
                continue
            except OSError:
                break

            hdr = _read_header(data)
            if not hdr:
                continue

            # remember player index + session
            self._player_idx = int(hdr.get("playerCarIndex", 0))
            self._session_uid = hdr.get("sessionUID")
            self.state.session_uid = str(self._session_uid) if self._session_uid is not None else None


            # reset player ref buffers on session change (prevents mixing sessions)
            if self._session_uid != self._last_session_uid:
                self._last_session_uid = self._session_uid
                for k in self._your_laps:
                    self._your_laps[k].clear()

            # DEBUG: Packet IDs zählen/anzeigen
            if self.debug:
                print(
                    f"RX len={len(data)} fmt={hdr.get('packetFormat')} year={hdr.get('gameYear')} pid={hdr.get('packetId')}"
                )

            # Trust Session packet (packetId == 1, 2, 7)
            pid = hdr.get("packetId")
            #if pid not in (0, 1, 2, 7):
            #    continue

            if pid == 1:
                if self.debug:
                    print("[PID1] Session packet received len=", len(data))

                # basic size sanity check
                if len(data) < 150:
                    continue

                base = int(hdr.get("headerSize", 29))  # after PacketHeader

                # --- Session packet fields (F1 25 spec) ---
                weather_raw = data[base + 0]  # 0..5

                if self.debug:
                    print("[SESSION] weather_raw", weather_raw, "trackTemp",
                          int.from_bytes(data[base + 1:base + 2], "little", signed=True))

                safety_car_off = base + 19 + (21 * 5)
                if safety_car_off + 3 >= len(data):
                    continue

                sc_raw = data[safety_car_off]          # 0..3
                num_fc = data[safety_car_off + 2]
                fc_off = safety_car_off + 3

                changed = False

                # --- Rain: current + forecast (from forecast samples) ---
                rain_now_raw = None
                rain_fc_raw = None
                fc_series = []
                self.state.rain_fc_series = None  # reset each session packet unless we fill it

                #print("[RAIN RAW]", "now", rain_now_raw, "fc", rain_fc_raw, "n_fc", int(num_fc))

                # fc_dbg = "fc:none"

                stride = 8
                if isinstance(num_fc, int) and num_fc > 0:
                    need = fc_off + (num_fc * stride)
                    if need <= len(data):
                        for j in range(num_fc):
                            o = fc_off + j * stride
                            time_off_min = int(data[o + 1])  # usually minutes into future
                            weather_fc = int(data[o + 2])  # 0..5
                            rain_fc = int(data[o + 7])  # 0..100
                            # guard
                            if 0 <= time_off_min <= 240 and 0 <= weather_fc <= 5 and 0 <= rain_fc <= 100:
                                fc_series.append((time_off_min, rain_fc, weather_fc))

                        # sort + dedupe by time offset
                        fc_series.sort(key=lambda x: x[0])
                        dedup = []
                        seen = set()
                        for t, r, w in fc_series:
                            if t in seen:
                                continue
                            seen.add(t)
                            dedup.append((t, r, w))
                        fc_series = dedup

                        # "rain_fc_pct" = first sample (nearest future)
                        if fc_series:
                            # rain_now = sample with timeOffset==0 if present, else use the earliest sample as best-effort
                            now_samples = [r for (t, r, w) in fc_series if t == 0]
                            if now_samples:
                                rain_now_raw = now_samples[0]

                            # rain_fc = nearest FUTURE sample (>0). If none, fall back to first.
                            future = [(t, r) for (t, r, w) in fc_series if t > 0]
                            if future:
                                future.sort(key=lambda x: x[0])
                                rain_fc_raw = future[0][1]
                            else:
                                rain_fc_raw = fc_series[0][1]

                    # publish series (None if empty)
                    self.state.rain_fc_series = fc_series if fc_series else None

                # Rain NOW
                if rain_now_raw is not None:
                    try:
                        rain_now_i = int(rain_now_raw)
                    except Exception:
                        rain_now_i = None

                    if rain_now_i is not None and 0 <= rain_now_i <= 100:
                        r_now = self._deb_rain_now.update(rain_now_i)
                        if r_now is not None and r_now != self.state.rain_now_pct:
                            self.state.rain_now_pct = r_now
                            changed = True

                # Rain FORECAST
                if rain_fc_raw is not None and 0 <= rain_fc_raw <= 100:
                    r_fc = self._deb_rain_fc.update(int(rain_fc_raw))
                    if r_fc is not None and r_fc != self.state.rain_fc_pct:
                        self.state.rain_fc_pct = r_fc
                        changed = True

                # Weather
                if 0 <= weather_raw <= 5:
                    w = self._deb_weather.update(int(weather_raw))
                    if w is not None and w != self.state.weather:
                        self.state.weather = w
                        changed = True

                # Safety Car
                if sc_raw in (0, 1, 2, 3):
                    sc = self._deb_sc.update(int(sc_raw))
                    if sc is not None and sc != self.state.safety_car_status:
                        self.state.safety_car_status = sc
                        changed = True

                if changed:
                    self._dirty = True


            elif pid == 2:

                base = int(hdr.get("headerSize", 29))

                car_size_guess = (len(data) - base) // 22

                car_size = 57

                if len(data) < base + 22 * car_size:
                    continue

                changed = False

                if not hasattr(self, "_last_lap_scan_t"):
                    self._last_lap_scan_t = 0.0
                nowt = time.monotonic()
                do_scan = (nowt - self._last_lap_scan_t) >= 1.0
                if do_scan:
                    self._last_lap_scan_t = nowt

                for i in range(22):

                    off = base + i * car_size

                    last_ms = struct.unpack_from("<I", data, off + 48)[0]

                    # --- store last lap time per car (ms) ---
                    if not hasattr(self, "_last_lap_ms_by_car"):
                        self._last_lap_ms_by_car = [None] * 22

                    # 0 / very large garbage => ignore
                    if last_ms and last_ms < 10_000_000:
                        self._last_lap_ms_by_car[i] = int(last_ms)

                    # Finde den wahrscheinlichsten LapTime-Field im CarBlock:
                    # - plausibel (40..360s)
                    # - NICHT extrem glatt (z.B. vielfaches von 256 ist verdächtig)
                    # - bevorzugt Wert, der sich gegenüber dem gespeicherten _last_lap_ms ändert

                    # --- normaler Pfad: nur plausible Zeiten übernehmen ---
                    if 40_000 <= last_ms <= 360_000:
                        if i == 0:
                            if self.debug:
                                print("[LAP DEBUG] car0 last_ms", last_ms, "cat", self._tyre_cat[0])

                        if self._last_lap_ms[i] != last_ms:
                            prev_ms = self._last_lap_ms[i]  # OLD value (before update)

                            self._last_lap_ms[i] = last_ms
                            changed = True

                            # --- Lap quality (valid/invalid + flag) ---
                            valid = True
                            lap_flag = "OK"

                            # IN nur dann setzen, wenn du wirklich ein IN-Signal hast.
                            # Da pit_status in Practice/Session-Übergängen unzuverlässig sein kann,
                            # machen wir IN sehr konservativ: nur wenn Lap extrem langsam ist.
                            if self._pit_status[i] != 0 and last_ms >= 200_000:
                                valid = False
                                lap_flag = "IN"

                            # Outlap-ignore nur, wenn es vorher durch echten Reifenwechsel "armed" wurde.
                            if self._ignore_next_lap[i]:
                                looks_like_outlap = False

                                if isinstance(prev_ms, int) and prev_ms > 0:
                                    if (last_ms - prev_ms) >= self._outlap_slow_ms:
                                        looks_like_outlap = True

                                # Safety net: extrem langsam => sehr wahrscheinlich Outlap
                                if last_ms >= 200_000:
                                    looks_like_outlap = True

                                if looks_like_outlap:
                                    valid = False
                                    lap_flag = "OUT"
                                else:
                                    # Game liefert oft keine Outlap-Zeit -> erste normale Lap nicht verwerfen
                                    valid = True
                                    lap_flag = "OK"

                                # Flag immer nach dem ersten LapTime-Event verbrauchen
                                self._ignore_next_lap[i] = False

                            self._lap_valid[i] = valid
                            self._lap_flag[i] = lap_flag

                            # --- Per-car history for field delta ---
                            cat = self._tyre_cat[i]
                            if valid and cat in ("SLICK", "INTER", "WET"):
                                lap_s = last_ms / 1000.0
                                buf = self._car_laps[i][cat]
                                if self._robust_accept_lap(buf, lap_s):
                                    buf.append(lap_s)

                            if i == self._player_idx:
                                if self.debug:
                                    print("[LAP FLAG]", i, lap_flag, "ms", last_ms, "ignore_next", self._ignore_next_lap[i],
                                          "pit", self._pit_status[i])

                            # --- YOUR reference laps (with outlier filter) ---
                            if self._player_idx is not None and i == self._player_idx:
                                cat = self._tyre_cat[i] or "SLICK"
                                if cat in ("SLICK", "INTER", "WET") and self._lap_valid[i]:
                                    lap_s = last_ms / 1000.0

                                    # hard plausibility guard
                                    if self._your_lap_min_s <= lap_s <= self._your_lap_max_s:
                                        # Nur saubere Laps als Referenz zulassen
                                        if self._lap_flag[i] == "OK" and self._lap_valid[0]:
                                            buf = self._your_laps[cat]

                                            if self._robust_accept_lap(buf, lap_s):
                                                buf.append(lap_s)
                                            else:
                                                # Optional: Debug nur wenn du willst
                                                # print(f"[REF DROP] {cat} lap {lap_s:.3f}s (outlier vs median)")
                                                pass

                    if i == 0 and do_scan:

                        # erst grob schätzen (nur debug)
                        car_size_guess = (len(data) - base) // 22
                        if self.debug:
                            print("[LAP SCAN] car_size_guess:", car_size_guess, "packet_len:", len(data))

                        def scan_car(car_index: int):
                            start = base + car_index * car_size_guess
                            end = min(len(data), start + car_size_guess)
                            blk = data[start:end]
                            hits = []
                            for k in range(0, min(len(blk), 120) - 4, 4):
                                v = struct.unpack_from("<I", blk, k)[0]
                                if 40_000 <= v <= 360_000:
                                    hits.append((k, v))
                            if self.debug:
                                print(f"[LAP SCAN] car{car_index} hits:", hits)

                        scan_car(0)
                        scan_car(1)

                if changed:
                    self._dirty = True


            elif pid == 7:

                base = int(hdr.get("headerSize", 29))

                remaining = len(data) - base

                car_size = 55
                if remaining < 22 * car_size:
                    continue

                #car_size = remaining // 22  # bei dir i.d.R. 55

                changed = False

                for i in range(22):

                    off = base + i * car_size

                    if off + car_size > len(data):
                        break

                    try:

                        actual = data[off + 25]

                        visual = data[off + 26]

                        self._tyre_actual[i] = actual
                        self._tyre_visual[i] = visual

                    except IndexError:

                        continue

                    if visual == 8:

                        tyre_cat = "WET"

                    elif visual == 7:

                        tyre_cat = "INTER"

                    else:

                        tyre_cat = "SLICK"

                    now = time.monotonic()
                    self._tyre_last_seen[i] = now

                    pit = self._pit_status[i]

                    # Während Pit nur "merken" (damit du es nicht VOR dem Stopp siehst)
                    if pit in (1, 2):
                        self._pending_tyre[i] = tyre_cat
                    else:
                        # auf Strecke: normal aktualisieren (z.B. Start, SC, etc.)
                        prev_cat = self._tyre_cat[i]
                        if prev_cat != tyre_cat:
                            self._tyre_cat[i] = tyre_cat
                            changed = True

                            # WICHTIG:
                            # Reifenklasse wechselt oft VOR dem nächsten LapTime-Event.
                            # Dann würde die letzte Slick-Zeit fälschlich als Inter/Wet gezählt werden.
                            self._last_lap_ms[i] = None
                            self._lap_valid[i] = False
                            self._lap_flag[i] = "TYRE_SWAP"

                            # Arm outlap-ignore ONLY if this looks like a real pit tyre change:
                            if prev_cat is not None:
                                self._pit_cycle[i] = 2
                                self._ignore_next_lap[i] = True

                            self._last_tyre_cat[i] = tyre_cat

                # DEBUG: nach dem Verarbeiten aller 22 Autos einmal ausgeben (sonst spam)
                interwet = []
                for j in range(22):
                    if self._tyre_cat[j] in ("INTER", "WET"):
                        interwet.append(
                            (j, self._tyre_cat[j], self._last_lap_ms[j], self._tyre_actual[j], self._tyre_visual[j]))
                if self.debug:
                    print("[TYRE DEBUG] inter/wet cars:", interwet)

                if changed:
                    self._dirty = True

            self._maybe_emit()

        sock.close()

    def _update_field_metrics_and_emit(self):

        unknown = sum(1 for x in self._tyre_cat if x is None)
        if self.debug:
            print("[TYRE DEBUG] unknown cats:", unknown, "sample0:", self._tyre_cat[0])

        inter = 0
        wet = 0
        slick = 0

        # counts from current tyre state (not from lap history)
        for i in range(22):
            cat = self._tyre_cat[i]
            if cat == "INTER":
                inter += 1
            elif cat == "WET":
                wet += 1
            elif cat == "SLICK":
                slick += 1

        total = inter + wet + slick
        interwet = inter + wet

        # compat: inter_share == (INTER+WET)/TOTAL
        self.state.inter_share = (interwet / total) if total > 0 else 0.0
        self.state.inter_only_share = (inter / total) if total > 0 else 0.0
        self.state.wet_share = (wet / total) if total > 0 else 0.0

        # compat: inter_count == (INTER+WET)
        self.state.inter_count = interwet
        self.state.inter_only_count = inter
        self.state.wet_count = wet
        self.state.slick_count = slick

        # --- Player tyre category (ALWAYS use real playerCarIndex) ---
        try:
            pidx = int(self._player_idx) if self._player_idx is not None else 0
        except Exception:
            pidx = 0

        if not (0 <= pidx < 22):
            pidx = 0

        self.state.player_car_index = pidx
        self.state.player_tyre_cat = self._tyre_cat[pidx] if (0 <= pidx < len(self._tyre_cat)) else None

        # --- Field Δ(I-S) computed per-driver (prevents Norris vs Gasly bias) ---
        deltas = []
        for i in range(22):
            slick = list(self._car_laps[i]["SLICK"])
            interwet = list(self._car_laps[i]["INTER"]) + list(self._car_laps[i]["WET"])

            # IMPORTANT: require 2+ samples each side to avoid outlap / stale values dominating
            if len(slick) >= 2 and len(interwet) >= 2:
                try:
                    d = statistics.median(interwet) - statistics.median(slick)

                    # reject insane deltas (spins/outlaps)
                    if -10.0 < d < 10.0:
                        deltas.append(d)
                except Exception:
                    pass

        # WIP/TELEMETRY SIGNAL:
        # Field-level delta is derived from live lap samples (median across cars).
        # It can fluctuate (sample size, outlaps, traffic), so it should be treated as an input
        # signal for advice/visualization, not as a hard pit trigger.
        if len(deltas) >= 3:
            self.state.pace_delta_inter_vs_slick_s = statistics.median(deltas)
        else:
            self.state.pace_delta_inter_vs_slick_s = None

        # --- Field Δ(W-I) and Δ(W-S) (separately) ---
        deltas_wi = []
        deltas_ws = []
        for i in range(22):
            slick_i = list(self._car_laps[i]["SLICK"])
            inter_i = list(self._car_laps[i]["INTER"])
            wet_i = list(self._car_laps[i]["WET"])

            # require 2+ samples each side
            if len(wet_i) >= 2 and len(inter_i) >= 2:
                try:
                    d = statistics.median(wet_i) - statistics.median(inter_i)
                    if -10.0 < d < 10.0:
                        deltas_wi.append(d)
                except Exception:
                    pass

            if len(wet_i) >= 2 and len(slick_i) >= 2:
                try:
                    d = statistics.median(wet_i) - statistics.median(slick_i)
                    if -10.0 < d < 10.0:
                        deltas_ws.append(d)
                except Exception:
                    pass

        self.state.pace_delta_wet_vs_inter_s = statistics.median(deltas_wi) if len(deltas_wi) >= 3 else None
        self.state.pace_delta_wet_vs_slick_s = statistics.median(deltas_ws) if len(deltas_ws) >= 3 else None


        # --- Your delta (learned from your own laps) ---
        s = list(self._your_laps["SLICK"])
        i_ = list(self._your_laps["INTER"])
        w = list(self._your_laps["WET"])

        self.state.your_ref_counts = f"S:{len(s)} I:{len(i_)} W:{len(w)}"

        if len(s) >= 2 and len(i_) >= 2:
            self.state.your_delta_inter_vs_slick_s = statistics.median(i_) - statistics.median(s)
        else:
            self.state.your_delta_inter_vs_slick_s = None

        if len(s) >= 2 and len(w) >= 2:
            self.state.your_delta_wet_vs_slick_s = statistics.median(w) - statistics.median(s)
        else:
            self.state.your_delta_wet_vs_slick_s = None

        if len(i_) >= 2 and len(w) >= 2:
            self.state.your_delta_wet_vs_inter_s = statistics.median(w) - statistics.median(i_)
        else:
            self.state.your_delta_wet_vs_inter_s = None


        # emit
        try:
            self.on_state(self.state)
        except Exception:
            pass

        if self.debug:
            print("[DELTA DEBUG] percar_deltas", len(deltas), "field_delta", self.state.pace_delta_inter_vs_slick_s)

    def _maybe_emit(self):
        if not getattr(self, "_dirty", False):
            return

        now = time.monotonic()
        if (now - getattr(self, "_last_emit_t", 0.0)) < getattr(self, "_emit_interval_s", 0.5):
            return

        self._last_emit_t = now
        self._dirty = False
        self._update_field_metrics_and_emit()

    def _robust_accept_lap(self, buf: list[float], lap_s: float) -> bool:
        """
        Robust outlier gate for reference laps:
        - needs some history
        - uses MAD (median absolute deviation) if possible
        - falls back to absolute threshold (self._your_outlier_sec)
        """
        # minimal history gate
        if len(buf) < self._your_outlier_min_n:
            return True

        try:
            med = statistics.median(buf)
            devs = [abs(x - med) for x in buf]
            mad = statistics.median(devs)

            # MAD->sigma approx (normal dist): sigma ~= 1.4826 * MAD
            sigma = 1.4826 * mad

            # dynamic threshold:
            # - at least your fixed threshold
            # - or 3.5 sigma (robust)
            dyn_thr = max(self._your_outlier_sec, 3.5 * sigma)

            return abs(lap_s - med) <= dyn_thr
        except Exception:
            # safest fallback
            try:
                med = statistics.median(buf)
                return abs(lap_s - med) <= self._your_outlier_sec
            except Exception:
                return True

# FUTURE/WIP: Robust parser for "rain next" extraction from Session packets without fixed offsets.
# Currently unused (not wired into the live pipeline), but kept as a fallback strategy if offsets change
# across game versions / patches.
def _find_rain_next_from_session_packet(data: bytes, base: int = 24):
    """
    Robust: sucht im Session-Packet nach dem Forecast-Array, ohne feste Offsets.
    Erwartet WeatherForecastSample-Strides von 8 bytes (F1 üblich).
    Gibt (rain_next_pct, debug_str) zurück.
    """
    best = None  # (score, offset_num, n, rain_next, layout)

    # wir scannen nach einer Stelle, wo ein plausibles 'numForecastSamples' steht
    for off_num in range(base, len(data) - 1):
        n = data[off_num]
        if not (1 <= n <= 56):
            continue

        start = off_num + 1
        stride = 8
        end = start + n * stride
        if end > len(data):
            continue

        # Layout A: weather at +2, trackTemp +3 (int8), airTemp +4 (int8), rainPct +7
        score = 0
        for j in range(n):
            o = start + j * stride
            weather = data[o + 2]
            rain = data[o + 7]
            track_temp = int.from_bytes(bytes([data[o + 3]]), "little", signed=True)
            air_temp = int.from_bytes(bytes([data[o + 4]]), "little", signed=True)
            time_offset = data[o + 1]

            if 0 <= weather <= 5:
                score += 1
            if 0 <= rain <= 100:
                score += 1
            if -30 <= track_temp <= 80:
                score += 1
            if -30 <= air_temp <= 80:
                score += 1
            if 0 <= time_offset <= 240:
                score += 1

        rain_next = data[start + 7]  # first sample rainPercentage
        cand = (score, off_num, n, rain_next, "A")
        if best is None or cand[0] > best[0]:
            best = cand

    if best is None:
        return None, "forecast_not_found"

    _, off_num, n, rain_next, layout = best
    return float(rain_next), f"forecast_found off_num={off_num} n={n} layout={layout}"




