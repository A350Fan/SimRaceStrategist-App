
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtWidgets, QtGui

from app.config import load_config, save_config, AppConfig
from app.watcher import FolderWatcher
from app.overtake_csv import parse_overtake_csv, lap_summary
from app.db import upsert_lap, latest_laps, lap_counts_by_track, distinct_tracks, laps_for_track
from app.strategy import generate_placeholder_cards
from app.f1_udp import F1UDPListener, F1LiveState
from app.logging_util import AppLogger
from app.strategy_model import LapRow, estimate_degradation_for_track_tyre, pit_window_one_stop, pit_windows_two_stop, recommend_rain_pit, RainPitAdvice
from app.rain_engine import RainEngine

import re
import time



class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SimRaceStrategist – Prototype")
        self.resize(1100, 700)

        self.cfg: AppConfig = load_config()
        self.watcher: Optional[FolderWatcher] = None
        self.udp: Optional[F1UDPListener] = None
        self._dedupe_mtime = {}  # src_path -> last_mtime_ns

        self._build_ui()
        self.logger = AppLogger(ui_sink=self._append_log_threadsafe)
        self.logger.info("App started.")
        self._apply_cfg_to_ui()
        self._refresh_db_views()
        self._refresh_track_combo()
        self._start_services_if_possible()
        self._live_state: F1LiveState = F1LiveState()
        self.rain_engine = RainEngine()

        self._your_last_lap_s = None
        self._your_last_tyre = None
        self._your_last_track = None

    def _detect_session(self, src: Path) -> str:
        n = src.stem.lower()

        # Race
        if re.search(r"(^|_)r($|_)", n):
            return "R"

        # Qualifying
        if re.search(r"(^|_)q($|_)", n) or re.search(r"(^|_)q[123]($|_)", n):
            return "Q"

        # Practice
        if re.search(r"(^|_)p($|_)", n) or re.search(r"(^|_)p[123]($|_)", n):
            return "P"

        return ""

    def _on_estimate_deg(self):
        track = self.cmbTrack.currentText().strip()
        tyre = self.cmbTyre.currentText().strip()
        if not track or not tyre:
            return

        thr = float(self.spinWearThr.value())
        race_laps = int(self.spinRaceLaps.value())

        rows_raw = laps_for_track(track, limit=5000)

        rows = []
        for r in rows_raw:
            rows.append(LapRow(
                created_at=r[0], session=r[1] or "", track=r[2] or "", tyre=r[3] or "",
                weather=r[4] or "", lap_time_s=r[5], fuel_load=r[6],
                wear_fl=r[7], wear_fr=r[8], wear_rl=r[9], wear_rr=r[10]
            ))

        est = estimate_degradation_for_track_tyre(rows, track=track, tyre=tyre, wear_threshold=thr)

        # If not enough data, show note and stop
        if est.predicted_laps_to_threshold is None:
            self.lblDeg.setText(f"{tyre} @ {track}\n{est.notes}")
            return

        # Pit window (1-stop)
        max_from_fresh = getattr(est, "max_stint_from_fresh_laps", None)
        pw = None
        if isinstance(max_from_fresh, (int, float)) and max_from_fresh > 0:
            pw = pit_window_one_stop(race_laps, max_from_fresh, min_stint_laps=5)

        pw2 = None
        if isinstance(max_from_fresh, (int, float)) and max_from_fresh > 0:
            pw2 = pit_windows_two_stop(race_laps, max_from_fresh, min_stint_laps=5)

        pit_txt = "pit window (1-stop): —"
        if pw is not None:
            pit_txt = f"pit window (1-stop): lap {pw[0]} – {pw[1]}"

        pit2_txt = "pit windows (2-stop): —"
        if pw2 is not None:
            pit2_txt = f"pit windows (2-stop): stop1 lap {pw2[0]} – {pw2[1]}, stop2 lap {pw2[2]} – {pw2[3]}"

        max_txt = ""
        if isinstance(max_from_fresh, (int, float)):
            max_txt = f"max stint to {thr:.0f}% ≈ {max_from_fresh:.1f} laps\n"

        self.lblDeg.setText(
            f"{tyre} @ {track}\n"
            f"n={est.n_laps_used} | wear/lap ≈ {est.wear_per_lap_pct:.2f}%\n"
            f"pace loss ≈ {est.pace_loss_per_pct_s:.3f}s per 1% wear\n"
            f"{max_txt}"
            f"{pit_txt}\n"
            f"{pit2_txt}\n"
            f"{est.notes}"
        )

    def _build_ui(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QGridLayout(root)

        # Top bar
        self.lblFolder = QtWidgets.QLabel("Telemetry Folder: (not set)")
        self.btnPick = QtWidgets.QPushButton("Pick Folder…")
        self.btnPick.clicked.connect(self.pick_folder)

        self.chkUdp = QtWidgets.QCheckBox("UDP enabled (F1 SC/Wetter)")
        self.spinPort = QtWidgets.QSpinBox()
        self.spinPort.setRange(1024, 65535)
        self.spinPort.setSingleStep(1)

        self.btnApply = QtWidgets.QPushButton("Apply Settings")
        self.btnApply.clicked.connect(self.apply_settings)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.lblFolder, 1)
        top.addWidget(self.btnPick)
        top.addSpacing(12)
        top.addWidget(self.chkUdp)
        top.addWidget(QtWidgets.QLabel("Port:"))
        top.addWidget(self.spinPort)
        top.addWidget(self.btnApply)
        layout.addLayout(top, 0, 0, 1, 2)

        # Live state panel
        self.grpLive = QtWidgets.QGroupBox("Live (F1 UDP)")
        liveLayout = QtWidgets.QHBoxLayout(self.grpLive)
        self.lblSC = QtWidgets.QLabel("SC/VSC: n/a")
        self.lblWeather = QtWidgets.QLabel("Weather: n/a")
        self.lblRain = QtWidgets.QLabel("Rain(next): n/a")
        for w in (self.lblSC, self.lblWeather, self.lblRain):
            w.setMinimumWidth(200)
        liveLayout.addWidget(self.lblSC)
        liveLayout.addWidget(self.lblWeather)
        liveLayout.addWidget(self.lblRain)
        self.lblRainAdvice = QtWidgets.QLabel("Rain pit: n/a")
        self.lblRainAdvice.setStyleSheet("font-weight: 700;")
        self.lblRainAdvice.setMinimumWidth(320)
        liveLayout.addWidget(self.lblRainAdvice)
        liveLayout.addStretch(1)
        layout.addWidget(self.grpLive, 1, 0, 1, 2)
        self.lblFieldShare = QtWidgets.QLabel("Field: Inter/Wet share: n/a")
        self.lblFieldDelta = QtWidgets.QLabel("Field: Δpace (I-S): n/a")
        self.lblFieldShare.setMinimumWidth(240)
        self.lblFieldDelta.setMinimumWidth(240)
        liveLayout.addWidget(self.lblFieldShare)
        liveLayout.addWidget(self.lblFieldDelta)

        # Strategy cards
        self.grpStrat = QtWidgets.QGroupBox("Strategy Cards (Prototype)")
        stratLayout = QtWidgets.QHBoxLayout(self.grpStrat)

        self.cardWidgets = []
        cards = generate_placeholder_cards()
        for c in cards:
            box = QtWidgets.QFrame()
            box.setFrameShape(QtWidgets.QFrame.StyledPanel)
            v = QtWidgets.QVBoxLayout(box)
            title = QtWidgets.QLabel(f"<b>{c.name}</b>")
            desc = QtWidgets.QLabel(c.description)
            plan = QtWidgets.QLabel(f"Tyre plan: {c.tyre_plan}")
            v.addWidget(title)
            v.addWidget(desc)
            v.addWidget(plan)
            v.addStretch(1)
            stratLayout.addWidget(box, 1)
            self.cardWidgets.append(box)

        layout.addWidget(self.grpStrat, 2, 0, 1, 2)

        # DB views
        self.tbl = QtWidgets.QTableWidget()
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setColumnCount(15)
        self.tbl.setHorizontalHeaderLabels(["lap","created_at","game","track","session","session_uid","tyre","weather","lap_time_s","fuel","wear_FL","wear_FR","wear_RL","wear_RR","lap_tag"])
        self.tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.tbl, 3, 0, 1, 2)

        # Log view
        self.logBox = QtWidgets.QPlainTextEdit()
        self.logBox.setReadOnly(True)
        self.logBox.setMaximumBlockCount(2000)  # keeps memory low
        layout.addWidget(self.logBox, 4, 0, 1, 2)

        self.grpDeg = QtWidgets.QGroupBox("Degradation model")
        degLayout = QtWidgets.QGridLayout(self.grpDeg)

        self.cmbTrack = QtWidgets.QComboBox()
        self.cmbTyre = QtWidgets.QComboBox()
        self.cmbTyre.addItems(["C1","C2","C3","C4","C5","C6","INTER","WET"])

        # NEW: race laps + wear threshold
        self.spinRaceLaps = QtWidgets.QSpinBox()
        self.spinRaceLaps.setRange(1, 200)
        self.spinRaceLaps.setValue(50)

        self.spinWearThr = QtWidgets.QSpinBox()
        self.spinWearThr.setRange(40, 99)
        self.spinWearThr.setValue(70)

        self.btnDeg = QtWidgets.QPushButton("Estimate")
        self.lblDeg = QtWidgets.QLabel("—")

        degLayout.addWidget(QtWidgets.QLabel("Track"), 0, 0)
        degLayout.addWidget(self.cmbTrack, 0, 1)

        degLayout.addWidget(QtWidgets.QLabel("Tyre"), 1, 0)
        degLayout.addWidget(self.cmbTyre, 1, 1)

        degLayout.addWidget(QtWidgets.QLabel("Race laps"), 2, 0)
        degLayout.addWidget(self.spinRaceLaps, 2, 1)

        degLayout.addWidget(QtWidgets.QLabel("Wear threshold (%)"), 3, 0)
        degLayout.addWidget(self.spinWearThr, 3, 1)

        degLayout.addWidget(self.btnDeg, 4, 0, 1, 2)
        degLayout.addWidget(self.lblDeg, 5, 0, 1, 2)

        layout.addWidget(self.grpDeg, 5, 0, 1, 2)
        self.btnDeg.clicked.connect(self._on_estimate_deg)


        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)

    def _apply_cfg_to_ui(self):
        if self.cfg.telemetry_root:
            self.lblFolder.setText(f"Telemetry Folder: {self.cfg.telemetry_root}")
        else:
            self.lblFolder.setText("Telemetry Folder: (not set)")
        self.chkUdp.setChecked(self.cfg.udp_enabled)
        self.spinPort.setValue(self.cfg.udp_port)

    def pick_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select telemetry root folder")
        if d:
            self.cfg.telemetry_root = d
            self._apply_cfg_to_ui()

    def apply_settings(self):
        self.cfg.udp_enabled = self.chkUdp.isChecked()
        self.cfg.udp_port = int(self.spinPort.value())
        save_config(self.cfg)
        self.status.showMessage("Settings saved. Restarting services…", 3000)
        self._restart_services()

    def _restart_services(self):
        self._stop_services()
        self._start_services_if_possible()

    def _start_services_if_possible(self):
        # watcher
        if self.cfg.telemetry_root:
            root = Path(self.cfg.telemetry_root)
            if root.exists():
                self.watcher = FolderWatcher(root, self._on_new_csv)
                self.watcher.start()
                self.status.showMessage("Folder watcher started.", 2500)
            else:
                self.status.showMessage("Telemetry root folder does not exist.", 4000)

        # udp
        if self.cfg.udp_enabled:
            self.udp = F1UDPListener(self.cfg.udp_port, self._on_live_state)
            self.udp.start()

    def _stop_services(self):
        if self.watcher:
            try: self.watcher.stop()
            except Exception: pass
            self.watcher = None
        if self.udp:
            try: self.udp.stop()
            except Exception: pass
            self.udp = None

    @QtCore.Slot(object)
    def _on_live_state(self, state: F1LiveState):
        self._live_state = state
        QtCore.QMetaObject.invokeMethod(
            self,
            "_update_live_labels",
            QtCore.Qt.QueuedConnection,
        )

    @QtCore.Slot()
    def _update_live_labels(self):
        state = getattr(self, "_live_state", F1LiveState())

        sc = -1 if state.safety_car_status is None else int(state.safety_car_status)
        weather = -1 if state.weather is None else int(state.weather)

        rain_fc = getattr(state, "rain_fc_pct", None)
        rain_now = getattr(state, "rain_now_pct", None)

        rain_fc_i = -1 if rain_fc is None else int(rain_fc)
        rain_now_i = -1 if rain_now is None else int(rain_now)

        sc_text = {0: "Green", 1: "Safety Car", 2: "VSC", 3: "Formation"}.get(sc, "–")
        sc_line = f"SC/VSC: {sc_text}"
        if self.lblSC.text() != sc_line:
            self.lblSC.setText(sc_line)

        weather_line = f"Weather(enum): {weather if weather >= 0 else 'n/a'}"
        if self.lblWeather.text() != weather_line:
            self.lblWeather.setText(weather_line)

        def _fc_at(series, tmin):
            if not series:
                return None
            for t, r, _w in series:
                if t >= tmin:
                    return int(r)
            return int(series[-1][1])

        series = getattr(state, "rain_fc_series", None) or []
        r3 = _fc_at(series, 3)
        r5 = _fc_at(series, 5)
        r10 = _fc_at(series, 10)
        r15 = _fc_at(series, 15)
        r20 = _fc_at(series, 20)

        fc_txt = "n/a"
        if any(x is not None for x in (r3, r5, r10, r15, r20)):
            fc_txt = (
                f"{r3 if r3 is not None else '-'} / "
                f"{r5 if r5 is not None else '-'} / "
                f"{r10 if r10 is not None else '-'} / "
                f"{r15 if r15 is not None else '-'} / "
                f"{r20 if r20 is not None else '-'}"
            )

        rain_line = f"Rain: {rain_now_i if rain_now_i >= 0 else 'n/a'} | FC(3/5/10/15/20): {fc_txt}"
        if self.lblRain.text() != rain_line:
            self.lblRain.setText(rain_line)

        # Rain pit advice
        try:
            rn = float(rain_fc_i) if rain_fc_i >= 0 else 0.0
        except Exception:
            rn = 0.0

        current_tyre = self.cmbTyre.currentText().strip() or "C4"
        laps_remaining = int(self.spinRaceLaps.value())
        pit_loss_s = 22.0

        track = self.cmbTrack.currentText().strip()

        # Tyre für Engine: wenn du willst, nimm “deinen letzten CSV-Reifen” statt UI:
        current_tyre = (self._your_last_tyre or self.cmbTyre.currentText().strip() or "C4")

        laps_remaining = int(self.spinRaceLaps.value())
        pit_loss_s = 22.0  # später dynamisch (SC/VSC) wenn du willst

        db_rows = None
        if track:
            try:
                db_rows = laps_for_track(track, limit=5000)
            except Exception:
                db_rows = None

        out = self.rain_engine.update(
            state,
            track=track,
            current_tyre=current_tyre,
            laps_remaining=laps_remaining,
            pit_loss_s=pit_loss_s,
            db_rows=db_rows,
            your_last_lap_s=self._your_last_lap_s,  # <-- jetzt aktiv!
        )

        ad = out.advice
        self.lblRainAdvice.setText(
            f"Rain pit: {ad.action} → {ad.target_tyre or 'n/a'} | "
            f"wet={out.wetness:.2f} conf={out.confidence:.2f} | {ad.reason}"
        )
        self.status.showMessage(out.debug)

        # Field metrics (aus state, den du in f1_udp.py befüllst)
        if state.inter_share is None or state.inter_count is None or state.slick_count is None:
            self.lblFieldShare.setText("Field: Inter/Wet share: n/a")
        else:
            total = state.inter_count + state.slick_count
            self.lblFieldShare.setText(
                f"Field: Inter/Wet share: {state.inter_share * 100:.0f}% ({state.inter_count}/{total})")

        # Field delta
        if state.pace_delta_inter_vs_slick_s is None:
            field_line = "Field: Δpace (I-S): n/a"
        else:
            field_line = f"Field: Δpace (I-S): {state.pace_delta_inter_vs_slick_s:+.2f}s"

        # Your delta (learned)
        rc = getattr(state, "your_ref_counts", None) or "S:0 I:0 W:0"
        yd = getattr(state, "your_delta_inter_vs_slick_s", None)
        yw = getattr(state, "your_delta_wet_vs_slick_s", None)

        if yd is None and yw is None:
            your_line = f"Your: Δ(I-S): n/a ({rc})"
        else:
            parts = []
            if yd is not None:
                parts.append(f"Δ(I-S) {yd:+.2f}s")
            if yw is not None:
                parts.append(f"Δ(W-S) {yw:+.2f}s")
            your_line = f"Your: " + ", ".join(parts) + f" ({rc})"

        txt = field_line + "\n" + your_line
        if self.lblFieldDelta.text() != txt:
            self.lblFieldDelta.setText(txt)

    def _on_new_csv(self, src: Path, cached: Path):
        # ---- DEDUPE: gleiche Datei (create/modify) nur 1x verarbeiten ----
        try:
            stat = cached.stat()
            sig = (stat.st_size, stat.st_mtime_ns)
        except Exception:
            return

        key = str(src)
        last_sig = self._dedupe_mtime.get(key)

        if last_sig == sig:
            return

        self._dedupe_mtime[key] = sig

        # Cooldown entfernt: watcher.copy_to_cache() wartet bereits auf stabile Dateigröße.
        # Doppel-Events werden durch (size, mtime_ns) dedupe abgefangen.
        # now = time.time()
        # last_t = getattr(self, "_dedupe_time_sec", {}).get(key, 0)
        #
        # if now - last_t < 1.0:   # 1 Sekunde Cooldown
        #     return
        #
        # self._dedupe_time_sec = getattr(self, "_dedupe_time_sec", {})
        # self._dedupe_time_sec[key] = now

        # ------------------------------------------------------------------

        # NEVER process files from our own cache folder (prevents infinite loops)

        # try:
        #     cache_dir = Path(app_data_dir()) / "cache"
        #     if cache_dir in src.resolve().parents:
        #         return
        # except Exception:
        #     pass

        #-------------------------------------------------------------------
        
        name = src.stem.lower()
        if "_tt_" in name or name.endswith("_tt") or name.startswith("tt_"):
            self.logger.info(f"Skipped (time trial): {src.name}")
            return

        session = self._detect_session(src)
        if session == "Q":
            self.logger.info(f"Skipped (qualifying not used for strategy): {src.name}")
            return

        
        self.logger.info(f"CSV seen: {src}")
        self.logger.info(f"Cached as: {cached}")

        try:
            parsed = parse_overtake_csv(cached)
            summ = lap_summary(parsed)
            try:
                self._your_last_lap_s = float(summ.get("lap_time_s")) if summ.get("lap_time_s") is not None else None
            except Exception:
                self._your_last_lap_s = None

            self._your_last_tyre = (summ.get("tyre") or None)
            self._your_last_track = (summ.get("track") or None)
            self.logger.info(
                f"[PLAYER] last_lap={self._your_last_lap_s} tyre={self._your_last_tyre} track={self._your_last_track}")

            if not isinstance(summ, dict):
                raise ValueError("lap_summary did not return a dict")
            summ["session"] = session

            # attach current UDP session uid (run id), fallback if UDP not ready yet
            sess_uid = None
            try:
                if self.udp and getattr(self.udp, "state", None):
                    sess_uid = self.udp.state.session_uid
            except Exception:
                sess_uid = None

            if sess_uid is None:
                # Fallback: use file timestamp (seconds) as run id so new race doesn't merge into NULL bucket
                # This is stable enough to separate sessions and avoids "lap continues from previous race".
                sess_uid = int(stat.st_mtime_ns // 1_000_000_000)

            summ["session_uid"] = str(sess_uid)

            upsert_lap(str(src), summ)

            self.logger.info(
                f"Imported: track={summ.get('track')} tyre={summ.get('tyre')} "
                f"weather={summ.get('weather')} lap_time_s={summ.get('lap_time_s')}"
            )

            QtCore.QMetaObject.invokeMethod(self, "_after_db_update", QtCore.Qt.QueuedConnection)

        except Exception as e:
            self.logger.error(f"IMPORT FAILED for {src.name}: {type(e).__name__}: {e}")
            QtCore.QMetaObject.invokeMethod(
                self.status, "showMessage", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, f"CSV import failed: {src.name} ({type(e).__name__}: {e})"),
                QtCore.Q_ARG(int, 12000),
            )

    @QtCore.Slot()
    def _after_db_update(self):
        self._refresh_track_combo()
        self._refresh_db_views()
        self.status.showMessage("DB updated from new CSV.", 2500)


    def _refresh_db_views(self):
        rows = latest_laps(800)

        def wear_avg(row):
            vals = [row[9], row[10], row[11], row[12]]
            vals = [v for v in vals if v is not None]
            return (sum(vals) / len(vals)) if vals else None

        # Gruppieren nach (game, track, session) → damit Boxenstopp über Tyre-Wechsel erkannt wird
        by_group = {}
        for i, row in enumerate(rows):
            key = (row[1], row[2], row[3], row[4])  # (game, track, session, session_uid)

            by_group.setdefault(key, []).append(i)

        lapno = {}  # row_index -> lap number
       
        tags = ["OK"] * len(rows)

        WEAR_DROP_THR = 2.0
        OUTLIER_SEC = 2.0

        # ---- Compute lap numbers + tags per (game, track, session, session_uid) ----
        for idxs in by_group.values():
            # rows sind newest-first → für Lapnummern umdrehen
            idxs = sorted(idxs, key=lambda j: rows[j][0])

            # Lapnummern
            for n, j in enumerate(idxs, start=1):
                lapno[j] = n

            # Wear-Drop → IN / OUT
            def wear_avg_idx(j):
                vals = [rows[j][9], rows[j][10], rows[j][11], rows[j][12]]
                vals = [v for v in vals if v is not None]
                return (sum(vals) / len(vals)) if vals else None

            w = [wear_avg_idx(j) for j in idxs]

            for k in range(1, len(idxs)):
                if w[k - 1] is None or w[k] is None:
                    continue
                if (w[k - 1] - w[k]) > WEAR_DROP_THR:
                    tags[idxs[k - 1]] = "IN"
                    tags[idxs[k]] = "OUT"

        # Tabelle rendern
        self.tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            # col 0: lap number
            lap_item = QtWidgets.QTableWidgetItem(str(lapno.get(r, "")))
            self.tbl.setItem(r, 0, lap_item)

            # cols 1..: original db columns (BUT hide session_uid from display)
            # row indices now: 0 created_at,1 game,2 track,3 session,4 session_uid,5 tyre,6 weather,7 lap_time_s,8 fuel,9..12 wear
            # display_row = row[:4] + row[5:]  # remove session_uid
            display_row = row[:5] + row[5:]

            for c, val in enumerate(display_row):
                item = QtWidgets.QTableWidgetItem("" if val is None else str(val))
                self.tbl.setItem(r, c + 1, item)

            # last col: lap_tag
            tag_item = QtWidgets.QTableWidgetItem(tags[r])
            self.tbl.setItem(r, len(display_row) + 1, tag_item)

    def _refresh_track_combo(self):
        try:
            tracks = distinct_tracks()
        except Exception:
            tracks = []
        self.cmbTrack.clear()
        self.cmbTrack.addItems(tracks)


    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._stop_services()
        super().closeEvent(event)

    @QtCore.Slot(str)
    def _append_log(self, line: str):
        self.logBox.appendPlainText(line)

    def _append_log_threadsafe(self, line: str):
        QtCore.QMetaObject.invokeMethod(
            self,
            "_append_log",
            QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, line),
        )


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
