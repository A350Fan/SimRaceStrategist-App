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
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.5)

        while not self._stop.is_set():
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                continue

            hdr = _read_header(data)
            if not hdr:
                continue

            # Still best-effort, but with strong stabilization:
            try:
                # weather often near offset 24 in session-like packets; read if plausible
                weather_raw = data[24] if len(data) > 25 else None
                # safety car status: search a small band for 0..3
                sc_raw = None
                for off in range(40, min(len(data), 90)):
                    v = data[off]
                    if v in (0, 1, 2, 3):
                        sc_raw = v
                        break

                # rain%: look for 0..100; keep first plausible
                rain_raw = None
                for off in range(90, min(len(data), 500)):
                    v = data[off]
                    if 0 <= v <= 100:
                        rain_raw = v
                        break

                changed = False

                if weather_raw is not None and 0 <= weather_raw <= 20:
                    w = self._deb_weather.update(int(weather_raw))
                    if w is not None and w != self.state.weather:
                        self.state.weather = w
                        changed = True

                if sc_raw is not None:
                    sc = self._deb_sc.update(int(sc_raw))
                    if sc is not None and sc != self.state.safety_car_status:
                        self.state.safety_car_status = sc
                        changed = True

                if rain_raw is not None:
                    r = self._deb_rain.update(int(rain_raw))
                    if r is not None and r != self.state.rain_percent_next:
                        self.state.rain_percent_next = r
                        changed = True

                if changed:
                    self.on_state(self.state)
            except Exception:
                continue
