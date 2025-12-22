\
# SimRaceStrategist (Prototype) – Overtake Telemetry Tool CSV + F1 UDP (SC/Wetter)

Dieses Projekt ist ein **laufeinfaches Grundgerüst**:
- **Read-only Ordner-Watcher**: beobachtet die CSVs vom Overtake Telemetry Tool und kopiert sie in einen Cache (damit nichts „angefasst“ wird).
- **CSV-Parser**: liest Meta/Game/Track/Setup-Blöcke + den Telemetrie-Block als Tabelle.
- **SQLite Datenbank**: speichert pro CSV ein „Lap“-Summary (LapTime, Tyre, Weather, Fuel, Wear-Ende etc.).
- **Mini UDP Listener (F1 25)**: nur **Safety Car/VSC** + **Wetter/Forecast** (separat vom CSV-Teil).

> Hinweis: Das ist **noch kein perfekter Strategierechner**. Es ist die stabile Basis, auf die wir die Strategie-Logik aufbauen können (Plan A/B/C, SC-Calls, Regen-Crossover).

---

## 1) Installation (Windows)

1. Python 3.11 oder 3.12 installieren.
2. In den Projektordner wechseln und Abhängigkeiten installieren:

```powershell
cd SimRaceStrategist
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Start:

```powershell
python -m app.main
```

---

## 2) Ordner einstellen (Overtake Telemetry Tool)

In der App:
- **Settings → Telemetry Folder** auswählen (dein Root-Ordner, z.B. `F:\OneDrive\Dokumente\11 Gaming-PC\SimRacingTelemetrie`)
- Die App sucht dann automatisch in Unterordnern nach CSVs (z.B. `lapdata\f1_2025\...`)

Die App arbeitet **read-only**:
- Sie kopiert neue/aktualisierte CSVs in `%LOCALAPPDATA%\SimRaceStrategist\cache` (unter Windows)
- und parst dann nur noch diese Kopien.

---

## 3) F1 25 UDP (Safety Car + Wetter)

Im F1 Spiel:
- UDP Telemetry aktivieren
- IP: deine PC-IP (oder 127.0.0.1)
- Port (Standard): 20777

In der App:
- **Settings → UDP Port** (Standard 20777)

> Aktuell wird nur minimal geparst (SC/VSC & Weather/Forecast). Das reicht für „Instant Calls“.

---

## 4) Wo liegen Daten?

- Config: `%LOCALAPPDATA%\SimRaceStrategist\config.json`
- Cache:  `%LOCALAPPDATA%\SimRaceStrategist\cache\`
- DB:     `%LOCALAPPDATA%\SimRaceStrategist\data.db`

---

## 5) Nächste Ausbaustufe (wenn du willst)

- Plan A/B/C Generator aus DB (Degradation-Model)
- SC Decision Panel: „Box/Stay/Opposite“ mit Delta-Schätzung
- Regen-Crossover: Slick vs Inter vs Wet aus historischen Laps + Forecast

