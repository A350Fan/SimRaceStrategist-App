\
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtWidgets, QtGui

from .config import load_config, save_config, AppConfig
from .watcher import FolderWatcher
from .overtake_csv import parse_overtake_csv, lap_summary
from .db import upsert_lap, latest_laps, lap_counts_by_track
from .strategy import generate_placeholder_cards
from .f1_udp import F1UDPListener, F1LiveState
from .logging_util import AppLogger
from .strategy_model import LapRow, estimate_degradation_for_track_tyre
from .db import distinct_tracks, laps_for_track

import re


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

        rows_raw = laps_for_track(track, limit=5000)

        rows = []
        for r in rows_raw:
            rows.append(LapRow(
                created_at=r[0], session=r[1] or "", track=r[2] or "", tyre=r[3] or "",
                weather=r[4] or "", lap_time_s=r[5], fuel_load=r[6],
                wear_fl=r[7], wear_fr=r[8], wear_rl=r[9], wear_rr=r[10]
            ))

        est = estimate_degradation_for_track_tyre(rows, track=track, tyre=tyre, wear_threshold=70.0)

        if est.predicted_laps_to_threshold is None:
            self.lblDeg.setText(f"{tyre} @ {track}\n{est.notes}")
            return

        self.lblDeg.setText(
            f"{tyre} @ {track}\n"
            f"n={est.n_laps_used} | wear/lap ≈ {est.wear_per_lap_pct:.2f}%\n"
            f"pace loss ≈ {est.pace_loss_per_pct_s:.3f}s per 1% wear\n"
            f"laps to 70% ≈ {est.predicted_laps_to_threshold:.1f}\n"
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
        self.lblSC = QtWidgets.QLabel("SC/VSC: –")
        self.lblWeather = QtWidgets.QLabel("Weather: –")
        self.lblRain = QtWidgets.QLabel("Rain(next): –")
        for w in (self.lblSC, self.lblWeather, self.lblRain):
            w.setMinimumWidth(200)
        liveLayout.addWidget(self.lblSC)
        liveLayout.addWidget(self.lblWeather)
        liveLayout.addWidget(self.lblRain)
        liveLayout.addStretch(1)
        layout.addWidget(self.grpLive, 1, 0, 1, 2)

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
        self.tbl.setColumnCount(12)
        self.tbl.setHorizontalHeaderLabels(["created_at","game","track","session","tyre","weather","lap_time_s","fuel","wear_FL","wear_FR","wear_RL","wear_RR"])
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

        self.btnDeg = QtWidgets.QPushButton("Estimate")
        self.lblDeg = QtWidgets.QLabel("—")

        degLayout.addWidget(QtWidgets.QLabel("Track"), 0, 0)
        degLayout.addWidget(self.cmbTrack, 0, 1)
        degLayout.addWidget(QtWidgets.QLabel("Tyre"), 1, 0)
        degLayout.addWidget(self.cmbTyre, 1, 1)
        degLayout.addWidget(self.btnDeg, 2, 0, 1, 2)
        degLayout.addWidget(self.lblDeg, 3, 0, 1, 2)

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
        # called from background thread: marshal to UI thread
        QtCore.QMetaObject.invokeMethod(
            self,
            "_update_live_labels",
            QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(int, -1 if state.safety_car_status is None else state.safety_car_status),
            QtCore.Q_ARG(int, -1 if state.weather is None else state.weather),
            QtCore.Q_ARG(int, -1 if state.rain_percent_next is None else state.rain_percent_next),
        )

    @QtCore.Slot(int, int, int)
    def _update_live_labels(self, sc: int, weather: int, rain: int):
        sc_text = {0:"Green",1:"Safety Car",2:"VSC",3:"Formation"}.get(sc, "–")
        self.lblSC.setText(f"SC/VSC: {sc_text}")
        self.lblWeather.setText(f"Weather(enum): {weather if weather>=0 else '–'}")
        self.lblRain.setText(f"Rain(next): {rain if rain>=0 else '–'}")

    def _on_new_csv(self, src: Path, cached: Path):
        # ---- DEDUPE: gleiche Datei (create/modify) nur 1x verarbeiten ----
        try:
            mtime_ns = src.stat().st_mtime_ns
        except Exception:
            mtime_ns = None

        key = str(src)
        if mtime_ns is not None:
            last = self._dedupe_mtime.get(key)
            if last == mtime_ns:
                return
            self._dedupe_mtime[key] = mtime_ns
        # ------------------------------------------------------------------

        
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
            if not isinstance(summ, dict):
                raise ValueError("lap_summary did not return a dict")
            summ["session"] = session


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
        rows = latest_laps(50)
        self.tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QtWidgets.QTableWidgetItem("" if val is None else str(val))
                self.tbl.setItem(r, c, item)

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
