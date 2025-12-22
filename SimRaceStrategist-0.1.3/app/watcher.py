from __future__ import annotations

import time
import hashlib
import shutil
from pathlib import Path
from typing import Callable, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .paths import cache_dir


def _stable_file(path: Path, checks: int = 3, delay: float = 0.25) -> bool:
    """Wait until file size is stable (writer finished)."""
    last_size = None
    for _ in range(checks):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False

        if last_size is not None and size == last_size:
            return True

        last_size = size
        time.sleep(delay)

    return True


def _hash_path(path: Path) -> str:
    h = hashlib.sha1(str(path).encode("utf-8", errors="ignore")).hexdigest()
    return h[:16]


def copy_to_cache(src: Path) -> Optional[Path]:
    # ---- HARD SKIP: Time Trial & irrelevant data ----
    name = src.stem.lower()
    p = str(src).lower()

    if "\\fastest_laps\\" in p or "\\laptimes\\" in p:
        return None

    # Dein TT-Format: hungary_DRY_TT_79.111_mclaren
    if "_tt_" in name or name.endswith("_tt") or name.startswith("tt_"):
        return None
    # -----------------------------------------------

    if src.suffix.lower() != ".csv":
        return None

    if not src.exists():
        return None

    if not _stable_file(src):
        return None

    dst = cache_dir() / f"{_hash_path(src)}_{src.name}"
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


class CSVHandler(FileSystemEventHandler):
    def __init__(self, on_csv: Callable[[Path, Path], None]):
        super().__init__()
        self.on_csv = on_csv

    def on_created(self, event):
        if event.is_directory:
            return
        src = Path(event.src_path)
        cached = copy_to_cache(src)
        if cached:
            self.on_csv(src, cached)

    def on_modified(self, event):
        if event.is_directory:
            return
        src = Path(event.src_path)
        cached = copy_to_cache(src)
        if cached:
            self.on_csv(src, cached)


class FolderWatcher:
    def __init__(self, root: Path, on_csv: Callable[[Path, Path], None]):
        self.root = root
        self.on_csv = on_csv
        self.observer = Observer()

    def start(self):
        handler = CSVHandler(self.on_csv)
        self.observer.schedule(handler, str(self.root), recursive=True)
        self.observer.start()

    def stop(self):
        self.observer.stop()
        self.observer.join(timeout=2)
