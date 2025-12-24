from __future__ import annotations
import socket
import statistics
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class F1LiveState:
    safety_car_status: Optional[int] = None  # 0 none, 1 SC, 2 VSC, 3 formation
    weather: Optional[int] = None            # enum (best-effort)
    rain_percent_next: Optional[int] = None  # 0..100 best-effort

    inter_share: Optional[float] = None
    pace_delta_inter_vs_slick_s: Optional[float] = None
    inter_count: Optional[int] = None
    slick_count: Optional[int] = None

    track_temp_c: Optional[float] = None
    air_temp_c: Optional[float] = None
    # optional: einfache Trends (aus letzten n Samples)
    track_temp_trend_c_per_min: Optional[float] = None
    rain_trend_pct_per_min: Optional[float] = None


def _read_header(data: bytes):
    # <HBBBBBQfIBB  (24 bytes)
    if len(data) < 24:
        return None
    try:
        u = struct.unpack_from("<HBBBBBQfIBB", data, 0)
        return {"packetFormat": u[0], "gameYear": u[1], "packetId": u[5]}
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
    def __init__(self, port: int, on_state: Callable[[F1LiveState], None]):
        self.port = port
        self.on_state = on_state
        self.state = F1LiveState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._deb_sc = _Debounce(n=6, max_age_s=0.7)
        self._deb_weather = _Debounce(n=6, max_age_s=0.7)
        self._deb_rain = _Debounce(n=6, max_age_s=0.7)

        self._last_lap_ms = [None] * 22
        self._tyre_cat = [None] * 22  # "SLICK" / "INTER" / "WET"

        self._emit_interval_s = 0.5  # 2 Hz
        self._last_emit_t = 0.0
        self._dirty = False  # merken: es gab neue Daten seit letztem Emit
        self._dirty_session = False
        self._tyre_last_seen = [0.0] * 22
        self._tyre_timeout_s = 2.5
        self._pit_status = [0] * 22  # 0 none, 1 pitting, 2 in pit area
        self._pending_tyre = [None] * 22  # Reifenwahl während Pit (wird erst beim Exit übernommen)

        self._tyre_actual = [None] * 22
        self._tyre_visual = [None] * 22

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
                print("RX", len(data))
            except socket.timeout:
                continue
            except OSError:
                break

            hdr = _read_header(data)
            if not hdr:
                continue

            # DEBUG: Packet IDs zählen/anzeigen
            print(
                f"RX len={len(data)} fmt={hdr.get('packetFormat')} year={hdr.get('gameYear')} pid={hdr.get('packetId')}")

            # Trust Session packet (packetId == 1, 2, 7)
            pid = hdr.get("packetId")
            if pid not in (1, 2, 7):
                continue

            if pid == 1:
                # basic size sanity check
                if len(data) < 150:
                    continue

                base = 24  # after PacketHeader

                # --- Session packet fields (F1 25 spec) ---
                weather_raw = data[base + 0]  # 0..5

                safety_car_off = base + 19 + (21 * 5)
                if safety_car_off + 3 >= len(data):
                    continue

                sc_raw = data[safety_car_off]          # 0..3
                num_fc = data[safety_car_off + 2]
                fc_off = safety_car_off + 3

                rain_next_raw = None
                if num_fc and (fc_off + 8) <= len(data):
                    rain_next_raw = data[fc_off + 7]

                changed = False

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

                # Rain forecast (chance)
                if rain_next_raw is not None and 0 <= rain_next_raw <= 100:
                    r = self._deb_rain.update(int(rain_next_raw))
                    if r is not None and r != self.state.rain_percent_next:
                        self.state.rain_percent_next = r
                        changed = True

                if changed:
                    self._dirty = True




            elif pid == 2:

                base = 24

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

                    # Finde den wahrscheinlichsten LapTime-Field im CarBlock:
                    # - plausibel (40..360s)
                    # - NICHT extrem glatt (z.B. vielfaches von 256 ist verdächtig)
                    # - bevorzugt Wert, der sich gegenüber dem gespeicherten _last_lap_ms ändert

                    # --- normaler Pfad: nur plausible Zeiten übernehmen ---
                    if 40_000 <= last_ms <= 360_000:
                        if i == 0:
                            print("[LAP DEBUG] car0 last_ms", last_ms, "cat", self._tyre_cat[0])

                        if self._last_lap_ms[i] != last_ms:
                            self._last_lap_ms[i] = last_ms
                            changed = True

                    if i == 0 and do_scan:

                        # erst grob schätzen (nur debug)
                        car_size_guess = (len(data) - base) // 22
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
                            print(f"[LAP SCAN] car{car_index} hits:", hits)

                        scan_car(0)
                        scan_car(1)

                if changed:
                    self._dirty = True


            elif pid == 7:

                base = 24

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

                        actual = data[off + 29]

                        visual = data[off + 30]

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
                        if self._tyre_cat[i] != tyre_cat:
                            self._tyre_cat[i] = tyre_cat
                            changed = True

                # DEBUG: nach dem Verarbeiten aller 22 Autos einmal ausgeben (sonst spam)
                interwet = []
                for j in range(22):
                    if self._tyre_cat[j] in ("INTER", "WET"):
                        interwet.append(
                            (j, self._tyre_cat[j], self._last_lap_ms[j], self._tyre_actual[j], self._tyre_visual[j]))
                print("[TYRE DEBUG] inter/wet cars:", interwet)

                if changed:
                    self._dirty = True

            self._maybe_emit()

        sock.close()

    def _update_field_metrics_and_emit(self):

        unknown = sum(1 for x in self._tyre_cat if x is None)
        print("[TYRE DEBUG] unknown cats:", unknown, "sample0:", self._tyre_cat[0])

        inter = 0
        slick = 0
        inter_laps = []
        slick_laps = []

        for i in range(22):
            cat = self._tyre_cat[i]
            ms = self._last_lap_ms[i]

            if cat in ("INTER", "WET"):
                inter += 1
                if isinstance(ms, int) and ms > 0:
                    inter_laps.append(ms / 1000.0)

            elif cat == "SLICK":
                slick += 1
                if isinstance(ms, int) and ms > 0:
                    slick_laps.append(ms / 1000.0)

        total = inter + slick
        self.state.inter_share = (inter / total) if total > 0 else 0.0
        self.state.inter_count = inter
        self.state.slick_count = slick

        # Delta Pace I-S: Median(inter/wet) - Median(slick)
        if len(inter_laps) >= 1 and len(slick_laps) >= 2:   # inter_laps Wert standard=2, für test 1
            try:
                inter_med = statistics.median(inter_laps)
                slick_med = statistics.median(slick_laps)
                self.state.pace_delta_inter_vs_slick_s = inter_med - slick_med
            except Exception:
                self.state.pace_delta_inter_vs_slick_s = None
        else:
            self.state.pace_delta_inter_vs_slick_s = None

        # emit
        try:
            self.on_state(self.state)
        except Exception:
            pass

        print("[DELTA DEBUG] inter_laps", len(inter_laps), "slick_laps", len(slick_laps),
              "delta", self.state.pace_delta_inter_vs_slick_s)

    def _maybe_emit(self):
        if not getattr(self, "_dirty", False):
            return

        now = time.monotonic()
        if (now - getattr(self, "_last_emit_t", 0.0)) < getattr(self, "_emit_interval_s", 0.5):
            return

        self._last_emit_t = now
        self._dirty = False
        self._update_field_metrics_and_emit()
