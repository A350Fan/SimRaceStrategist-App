# SimRaceStrategist (Early Prototype) – basierend auf CSVs aus Iko Reins Telemetry Tool + F1 UDP (SC/Wetter/Gegner)

Dieses Projekt ist ein **laufeinfaches Grundgerüst**:
- **Read-only Ordner-Watcher**: beobachtet die CSVs vom Overtake Telemetry Tool und kopiert sie in einen Cache (damit nichts „angefasst“ wird).
- **CSV-Parser**: liest Meta/Game/Track/Setup-Blöcke + den Telemetrie-Block als Tabelle.
- **SQLite Datenbank**: speichert pro CSV ein „Lap“-Summary (LapTime, Tyre, Weather, Fuel, Wear-Ende etc.).
- **Mini UDP Listener (F1 25)**: nur **Safety Car/VSC** + **Wetter/Forecast** (separat vom CSV-Teil).

> Hinweis: Das ist **noch kein perfekter Strategierechner**. Es ist die stabile Basis, auf der die Strategie-Logik aufbauen wird (Plan A/B/C, SC-Calls, Regen-Crossover).

---

## 1) Einrichtung (Windows)

1. Python 3.11 oder 3.12 installieren.
2. In den Projektordner wechseln und Abhängigkeiten z. B. per Powershell installieren:

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
4. Iko Reins Telemetrie Tool herunterladen und einrichten (https://www.overtake.gg/downloads/telemetry-tool-for-f1-25-and-many-other-games.77557/)

---

## 2) Ordner einstellen (Overtake Telemetry Tool)

In der App:
- **Settings → Telemetry Folder** auswählen (Root-Ordner, in welchem die CSVs von Iko Reins Tool gespeichert werden, z.B. `C:\Dokumente\SimRacingTelemetrie`)
- Die App sucht dann automatisch in Unterordnern nach CSVs (z.B. `lapdata\f1_2025\...`)

Die App arbeitet **read-only**:
- Sie kopiert neue/aktualisierte CSVs in `%LOCALAPPDATA%\SimRaceStrategist\cache` (unter Windows)
- und parst dann nur noch diese Kopien.

---

## 3) F1 25 UDP (Safety Car + Wetter)

Im F1 Spiel:
- UDP Telemetry aktivieren
- Broadcast für bessere Kompatibilität aktivieren
- IP: deine PC-IP (oder 127.0.0.1)
- Port (Standard): 20777

In der App:
- **Settings → UDP Port** (Standard 20777)

> Aktuell wird nur minimal geparst (SC/VSC & Weather/Forecast,AI Gegner).

---

## 4) Wo liegen Daten?

- Config: `%LOCALAPPDATA%\SimRaceStrategist\config.json`
- Cache:  `%LOCALAPPDATA%\SimRaceStrategist\cache\`
- DB:     `%LOCALAPPDATA%\SimRaceStrategist\data.db`
- Log:	  `%LOCALAPPDATA%\SimRaceStrategist\app.log`

---

## 5) Nächste Ausbaustufe

- Regen-Crossover: Slick vs Inter vs Wet aus historischen Laps (z. B. Shifting Lap Times), aktuellem Wetter, Wetterbericht, Temperatur, AI-Gegner
- Plan A/B/C Generator aus DB (Degradation-Model+Lap Times
- SC Decision Panel: „Box/Stay/Opposite“ mit Delta-Schätzung