from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict
import pandas as pd

def _parse_two_line_block(header_line: str, data_line: str) -> Dict[str, str]:
    header = next(csv.reader([header_line]))
    data = next(csv.reader([data_line]))
    if len(data) < len(header):
        data += [""] * (len(header) - len(data))
    return {h.strip(): d.strip() for h, d in zip(header, data)}

def parse_overtake_csv(path: Path) -> Dict[str, Any]:
    """
    Parse the Overtake Telemetry Tool CSV format (multi-block).
    Returns dict with: player, game, track, setup, telemetry (DataFrame)
    """
    lines = path.read_text(errors="replace").splitlines()
    if len(lines) < 10:
        raise ValueError(f"CSV too short: {path}")

    player_row = next(csv.reader([lines[0]]))
    player = {
        "raw": player_row,
        "tag": player_row[0] if len(player_row) > 0 else "",
        "tool_version": player_row[1] if len(player_row) > 1 else "",
        "driver": player_row[2] if len(player_row) > 2 else "",
        "timestamp": player_row[4] if len(player_row) > 4 else "",
    }

    game = _parse_two_line_block(lines[1], lines[2])
    track = _parse_two_line_block(lines[3], lines[4])
    setup = _parse_two_line_block(lines[5], lines[6])

    start_idx = None
    for i, l in enumerate(lines):
        if l.startswith("LapDistance"):
            start_idx = i
            break
    if start_idx is None:
        raise ValueError(f"Telemetry header not found in: {path}")

    telemetry_text = "\n".join(lines[start_idx:])
    df = pd.read_csv(io.StringIO(telemetry_text))

    return {"player": player, "game": game, "track": track, "setup": setup, "telemetry": df}

def lap_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Create a compact summary row for the database."""
    game = parsed.get("game") or {}
    track = parsed.get("track") or {}
    setup = parsed.get("setup") or {}
    df: pd.DataFrame = parsed.get("telemetry")

    # Lap time (best-effort)
    lap_time_s = None
    for k in ("LapTime [s]", "LapTime[s]", "laptime [s]", "laptime[s]", "LapTime"):
        if k in game:
            try:
                lap_time_s = float(game[k])
            except Exception:
                lap_time_s = None
            break

    tyre = track.get("Tyre [txt]", track.get("Tyre", ""))
    weather = track.get("Weather [txt]", track.get("Weather", ""))
    track_name = game.get("Track", game.get("track", "")) or track.get("Track", "")
    game_name = game.get("Game", game.get("game", ""))

    # fuel = None
    # for k in ("FuelLoad", "FuelLoad [kg]", "Fuel Load", "FuelLoad[kg]"):
    #     if k in setup:
    #         try:
    #             fuel = float(setup[k])
    #         except Exception:
    #             fuel = None
    #         break

    # Fuel: prefer "remaining fuel" from telemetry (changes each lap), fallback to setup fuel load
    fuel = None

    # 1) Try telemetry columns (end-of-lap value)
    if isinstance(df, pd.DataFrame) and not df.empty:
        for col in (
            "FuelInTank [kg]",
            "Fuel in tank [kg]",
            "FuelInTank",
            "FuelRemaining [kg]",
            "Fuel Remaining [kg]",
            "FuelRemaining",
            "Fuel [kg]",
            "Fuel",
        ):
            if col in df.columns:
                try:
                    fuel = float(df[col].iloc[-1])
                except Exception:
                    fuel = None
                break

    # 2) Fallback: setup fuel (static)
    if fuel is None:
        for k in ("FuelLoad", "FuelLoad [kg]", "Fuel Load", "FuelLoad[kg]"):
            if k in setup:
                try:
                    fuel = float(setup[k])
                except Exception:
                    fuel = None
                break

    # Wear: last-row values if columns exist
    wear_map = [
        ("TyreWearFrontLeft [%]", "wear_fl"),
        ("TyreWearFrontRight [%]", "wear_fr"),
        ("TyreWearRearLeft [%]", "wear_rl"),
        ("TyreWearRearRight [%]", "wear_rr"),
    ]
    wear: Dict[str, Any] = {k: None for _, k in wear_map}

    if isinstance(df, pd.DataFrame) and not df.empty:
        for col, out in wear_map:
            if col in df.columns:
                try:
                    wear[out] = float(df[col].iloc[-1])
                except Exception:
                    wear[out] = None

    # ✅ IMPORTANT: Always return a dict (never None)
    return {
        "game": game_name,
        "track": track_name,
        "weather": weather,
        "tyre": tyre,
        "lap_time_s": lap_time_s,
        "fuel_load": fuel,
        **wear,
    }
