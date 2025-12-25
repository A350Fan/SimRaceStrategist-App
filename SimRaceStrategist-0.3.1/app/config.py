from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from app.paths import config_path


@dataclass
class AppConfig:
    telemetry_root: str = ""     # e.g. F:\OneDrive\...\SimRacingTelemetrie
    udp_port: int = 20777
    udp_enabled: bool = True

    # Debug spam control (default True to preserve current behavior)
    udp_debug: bool = True

def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig(
            telemetry_root=str(data.get("telemetry_root", "")),
            udp_port=int(data.get("udp_port", 20777)),
            udp_enabled=bool(data.get("udp_enabled", True)),
            udp_debug=bool(data.get("udp_debug", True)),
        )

    except Exception:
        cfg = AppConfig()
        save_config(cfg)
        return cfg

def save_config(cfg: AppConfig) -> None:
    path = config_path()
    path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
