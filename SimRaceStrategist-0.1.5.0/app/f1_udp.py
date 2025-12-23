from __future__ import annotations
import socket
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
                    self.on_state(self.state)


            elif pid == 2:

                base = 24

                remaining = len(data) - base

                if remaining < 22 * 4:
                    continue

                car_size = remaining // 22  # bei dir i.d.R. 57

                changed = False

                for i in range(22):

                    off = base + i * car_size

                    if off + 4 > len(data):
                        break

                    (ms,) = struct.unpack_from("<I", data, off)  # lastLapTimeInMS liegt am Anfang

                    if 10_000 <= ms <= 300_000:  # 10s..300s plausibel

                        if self._last_lap_ms[i] != ms:
                            self._last_lap_ms[i] = ms

                            changed = True

                if changed:
                    self._dirty = True

                pit = data[off + 34]  # m_pitStatus
                self._pit_status[i] = pit

                # Wenn Fahrer wieder draußen ist, übernehmen wir den Reifen, der während des Stops gemeldet wurde
                if pit == 0 and self._pending_tyre[i] is not None:
                    if self._tyre_cat[i] != self._pending_tyre[i]:
                        self._tyre_cat[i] = self._pending_tyre[i]
                        changed = True
                    self._pending_tyre[i] = None






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


                        if i == 0:
                            block = data[off:off + 55]  # car 0
                            hits = [(idx, b) for idx, b in enumerate(block) if b in (7, 8)]
                            print("[TYRE SCAN] indices with 7/8:", hits)
                            print("[TYRE SCAN] bytes 20..40:", list(enumerate(block[20:41], start=20)))

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

                if changed:
                    self._dirty = True

            self._maybe_emit()

        sock.close()

    def _update_field_metrics_and_emit(self):
        # counts
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

        denom = inter + slick
        self.state.inter_count = inter if denom else None
        self.state.slick_count = slick if denom else None
        self.state.inter_share = (inter / denom) if denom else None

        # Δpace (Inter - Slick): robust über Median
        def median(xs):
            xs = sorted(xs)
            n = len(xs)
            if n == 0:
                return None
            mid = n // 2
            return xs[mid] if (n % 2 == 1) else (xs[mid - 1] + xs[mid]) / 2.0

        mi = median(inter_laps)
        ms = median(slick_laps)

        if mi is not None and ms is not None and len(inter_laps) >= 2 and len(slick_laps) >= 2:
            self.state.pace_delta_inter_vs_slick_s = mi - ms
        else:
            self.state.pace_delta_inter_vs_slick_s = None

        # push to UI
        self.on_state(self.state)

    def _maybe_emit(self):
        if not getattr(self, "_dirty", False):
            return

        now = time.monotonic()
        if (now - getattr(self, "_last_emit_t", 0.0)) < getattr(self, "_emit_interval_s", 0.5):
            return

        self._last_emit_t = now
        self._dirty = False
        self._update_field_metrics_and_emit()
