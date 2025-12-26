from __future__ import annotations
import sqlite3
from typing import Dict, Any, List, Tuple
from .paths import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT UNIQUE,
    created_at TEXT DEFAULT (datetime('now')),
    game TEXT,
    track TEXT,
    session TEXT,
    session_uid TEXT,
    weather TEXT,
    tyre TEXT,
    lap_time_s REAL,
    fuel_load REAL,
    wear_fl REAL,
    wear_fr REAL,
    wear_rl REAL,
    wear_rr REAL
);
"""

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute(SCHEMA)
    con.execute("CREATE INDEX IF NOT EXISTS idx_laps_track ON laps(track);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_laps_created ON laps(created_at);")

    # --- migrations (single table_info read) ---
    cols = {row[1] for row in con.execute("PRAGMA table_info(laps);").fetchall()}

    # TEXT to avoid uint64 overflow + keep DB compatible
    if "session_uid" not in cols:
        con.execute("ALTER TABLE laps ADD COLUMN session_uid TEXT;")
        cols.add("session_uid")

    if "session" not in cols:
        con.execute("ALTER TABLE laps ADD COLUMN session TEXT;")
        cols.add("session")

    con.commit()
    return con


def upsert_lap(source_file: str, summary: Dict[str, Any]) -> None:
    con = connect()
    with con:
        con.execute(
            """
            INSERT INTO laps (
                source_file,
                game,
                track,
                session,
                session_uid,
                weather,
                tyre,
                lap_time_s,
                fuel_load,
                wear_fl,
                wear_fr,
                wear_rl,
                wear_rr
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,?)
            ON CONFLICT(source_file) DO UPDATE SET
                game = excluded.game,
                track = excluded.track,
                session = excluded.session,
                session_uid = excluded.session_uid,
                weather = excluded.weather,
                tyre = excluded.tyre,
                lap_time_s = excluded.lap_time_s,
                fuel_load = excluded.fuel_load,
                wear_fl = excluded.wear_fl,
                wear_fr = excluded.wear_fr,
                wear_rl = excluded.wear_rl,
                wear_rr = excluded.wear_rr;
            """,
            (
                source_file,
                summary.get("game"),
                summary.get("track"),
                summary.get("session"),
                summary.get("session_uid"),
                summary.get("weather"),
                summary.get("tyre"),
                summary.get("lap_time_s"),
                summary.get("fuel_load"),
                summary.get("wear_fl"),
                summary.get("wear_fr"),
                summary.get("wear_rl"),
                summary.get("wear_rr"),
            ),
        )

def latest_laps(limit: int = 50) -> List[Tuple]:
    con = connect()
    cur = con.execute(
        """
        SELECT
            created_at,
            game,
            track,
            session,
            session_uid,
            tyre,
            weather,
            lap_time_s,
            fuel_load,
            wear_fl,
            wear_fr,
            wear_rl,
            wear_rr
        FROM laps
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()

def lap_counts_by_track() -> List[Tuple[str, int]]:
    con = connect()
    cur = con.execute(
        """
        SELECT
            COALESCE(track, '') AS track,
            COUNT(*)
        FROM laps
        GROUP BY track
        ORDER BY COUNT(*) DESC
        """
    )
    return cur.fetchall()

def laps_for_track(track: str, limit: int = 2000):
    con = connect()
    cur = con.execute(
        """
        SELECT
            created_at, session, track, tyre, weather,
            lap_time_s, fuel_load, wear_fl, wear_fr, wear_rl, wear_rr
        FROM laps
        WHERE track = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (track, limit),
    )
    return cur.fetchall()

def distinct_tracks():
    con = connect()
    cur = con.execute("SELECT DISTINCT COALESCE(track,'') FROM laps WHERE COALESCE(track,'') <> '' ORDER BY 1;")
    return [r[0] for r in cur.fetchall()]
