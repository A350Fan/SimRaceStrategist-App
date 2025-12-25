from __future__ import annotations

import datetime
from pathlib import Path
from typing import Callable, Optional
from .paths import app_dir

def log_file_path() -> Path:
    return app_dir() / "app.log"

class AppLogger:
    def __init__(self, ui_sink: Optional[Callable[[str], None]] = None):
        self.ui_sink = ui_sink

    def _stamp(self) -> str:
        return datetime.datetime.now().strftime("%H:%M:%S")

    def write(self, level: str, msg: str) -> None:
        line = f"[{self._stamp()}] {level}: {msg}"
        # UI
        if self.ui_sink:
            try:
                self.ui_sink(line)
            except Exception:
                pass
        # File
        try:
            lf = log_file_path()
            with lf.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def info(self, msg: str) -> None:
        self.write("INFO", msg)

    # FUTURE/WIP: WARN level is currently not used by the app (only INFO/ERROR are emitted),
    # but keeping it makes it easy to add structured warnings later (e.g. telemetry anomalies).
    def warn(self, msg: str) -> None:
        self.write("WARN", msg)

    def error(self, msg: str) -> None:
        self.write("ERROR", msg)
