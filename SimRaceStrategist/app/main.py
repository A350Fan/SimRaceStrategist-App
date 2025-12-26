
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
from app.translator import Translator


import re


_RE_RACE = re.compile(r"(^|_)r($|_)")
_RE_QUALI = re.compile(r"(^|_)q($|_)|(^|_)q[123]($|_)")
_RE_PRACTICE = re.compile(r"(^|_)p($|_)|(^|_)p[123]($|_)")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SimRaceStrategist – Prototype")
        self.resize(1100, 700)

        self.cfg: AppConfig = load_config()

        # i18n
        self.tr = Translator(self.cfg.language)

        self.watcher: Optional[FolderWatcher] = None

        self.udp: Optional[F1UDPListener] = None
        self._dedupe_mtime = {}  # src_path -> last_mtime_ns

        self._build_ui()
        self._retranslate_ui()

        # Log bleibt im File aktiv, aber kein UI-Debugfenster mehr.
        # (ui_sink=None => nur app.log, kein QPlainTextEdit-Spam)
        self.logger = AppLogger(ui_sink=None)
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

    # NOTE: Legacy implementation of _detect_session() (inline regex calls) was replaced
    # by precompiled regex constants (_RE_RACE/_RE_QUALI/_RE_PRACTICE) for readability and speed.
    # The logic is unchanged; see _detect_session() below.

    def _detect_session(self, src: Path) -> str:
        n = src.stem.lower()

        if _RE_RACE.search(n):
            return "R"

        if _RE_QUALI.search(n):
            return "Q"

        if _RE_PRACTICE.search(n):
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

        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        # --- Tabs (reduces clutter massively) ---
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs, 1)

        # =========================
        # TAB 1: LIVE
        # =========================
        tab_live = QtWidgets.QWidget()
        self.tabs.addTab(tab_live, "Live")

        live_outer = QtWidgets.QVBoxLayout(tab_live)
        live_outer.setContentsMargins(10, 10, 10, 10)
        live_outer.setSpacing(10)

        # Live group (same widgets as before, just arranged cleaner)
        self.grpLive = QtWidgets.QGroupBox(self.tr.t("live.group_title", "Live (F1 UDP)"))
        live_outer.addWidget(self.grpLive)

        liveLayout = QtWidgets.QGridLayout(self.grpLive)
        liveLayout.setColumnStretch(0, 0)
        liveLayout.setColumnStretch(1, 1)
        liveLayout.setColumnStretch(2, 1)
        liveLayout.setColumnStretch(3, 2)

        self.lblSC = QtWidgets.QLabel(
            self.tr.t("live.sc_fmt", "SC/VSC: {status}").format(status=self.tr.t("common.na", "n/a")))
        self.lblWeather = QtWidgets.QLabel(self.tr.t("live.weather_na", "Weather: n/a"))
        self.lblRain = QtWidgets.QLabel(self.tr.t("live.rain_na", "Rain(next): n/a"))

        for w in (self.lblSC, self.lblWeather, self.lblRain):
            w.setMinimumWidth(220)

        # Advice prominent
        self.lblRainAdvice = QtWidgets.QLabel(self.tr.t("live.rain_pit_na", "Rain pit: n/a"))
        self.lblRainAdvice.setStyleSheet("font-weight: 700;")
        self.lblRainAdvice.setMinimumWidth(360)

        self.lblFieldShare = QtWidgets.QLabel(self.tr.t("live.field_share_na", "Field: Inter/Wet share: n/a"))
        self.lblFieldDelta = QtWidgets.QLabel(self.tr.t("live.field_delta_na", "Field: Δpace (I-S): n/a"))
        self.lblFieldShare.setMinimumWidth(240)
        self.lblFieldDelta.setMinimumWidth(240)

        liveLayout.addWidget(self.lblSC, 0, 0)
        liveLayout.addWidget(self.lblWeather, 0, 1)
        liveLayout.addWidget(self.lblRain, 0, 2)
        liveLayout.addWidget(self.lblRainAdvice, 0, 3)

        liveLayout.addWidget(self.lblFieldShare, 1, 0, 1, 2)
        liveLayout.addWidget(self.lblFieldDelta, 1, 2, 1, 2)

        # Strategy cards (same as before)
        self.grpStrat = QtWidgets.QGroupBox(self.tr.t("cards.group_title", "Strategy Cards (Prototype)"))
        live_outer.addWidget(self.grpStrat)

        stratLayout = QtWidgets.QHBoxLayout(self.grpStrat)
        stratLayout.setSpacing(10)

        self.cardWidgets = []
        cards = generate_placeholder_cards()
        for c in cards:
            w = QtWidgets.QGroupBox(c.name)
            v = QtWidgets.QVBoxLayout(w)
            lbl_desc = QtWidgets.QLabel(c.description)
            lbl_desc.setWordWrap(True)
            lbl_plan = QtWidgets.QLabel(f"Tyres: {c.tyre_plan}")
            lbl_plan.setStyleSheet("font-weight: 700;")
            v.addWidget(lbl_desc)
            v.addWidget(lbl_plan)

            # Optional fields preserved if you already had them in your prototype
            if c.next_pit_lap is not None:
                v.addWidget(QtWidgets.QLabel(f"Next pit: Lap {c.next_pit_lap}"))
            v.addStretch(1)

            w.setMinimumWidth(260)
            stratLayout.addWidget(w)
            self.cardWidgets.append(w)

        live_outer.addStretch(1)

        # =========================
        # TAB 2: LAPS / DB
        # =========================
        tab_db = QtWidgets.QWidget()
        self.tabs.addTab(tab_db, "Laps / DB")

        db_outer = QtWidgets.QVBoxLayout(tab_db)
        db_outer.setContentsMargins(10, 10, 10, 10)
        db_outer.setSpacing(10)

        # Small toolbar (optional, but helps usability)
        db_bar = QtWidgets.QHBoxLayout()
        db_outer.addLayout(db_bar)

        self.btnRefreshDb = QtWidgets.QPushButton("Refresh")
        self.btnRefreshDb.clicked.connect(self._refresh_db_views)
        db_bar.addWidget(self.btnRefreshDb)

        db_bar.addStretch(1)

        self.tbl = QtWidgets.QTableWidget()
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setColumnCount(15)
        self.tbl.setHorizontalHeaderLabels([
            "lap",
            "created_at",
            "game",
            "track",
            "session",
            "session_uid",
            "tyre",
            "weather",
            "lap_time_s",
            "fuel",
            "wear_FL",
            "wear_FR",
            "wear_RL",
            "wear_RR",
            "lap_tag",
        ])
        self.tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        db_outer.addWidget(self.tbl, 1)

        # =========================
        # TAB 3: MODEL
        # =========================
        tab_model = QtWidgets.QWidget()
        self.tabs.addTab(tab_model, "Model")

        model_outer = QtWidgets.QVBoxLayout(tab_model)
        model_outer.setContentsMargins(10, 10, 10, 10)
        model_outer.setSpacing(10)

        self.grpDeg = QtWidgets.QGroupBox("Degradation model")
        model_outer.addWidget(self.grpDeg)

        degLayout = QtWidgets.QGridLayout(self.grpDeg)

        self.cmbTrack = QtWidgets.QComboBox()
        self.cmbTyre = QtWidgets.QComboBox()
        self.cmbTyre.addItems(["C1", "C2", "C3", "C4", "C5", "C6", "INTER", "WET"])

        self.spinRaceLaps = QtWidgets.QSpinBox()
        self.spinRaceLaps.setRange(1, 200)
        self.spinRaceLaps.setValue(50)

        self.spinWearThr = QtWidgets.QSpinBox()
        self.spinWearThr.setRange(40, 99)
        self.spinWearThr.setValue(70)

        self.btnDeg = QtWidgets.QPushButton("Estimate degradation + pit windows")
        self.lblDeg = QtWidgets.QLabel("—")
        self.lblDeg.setWordWrap(True)

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

        self.btnDeg.clicked.connect(self._on_estimate_deg)
        model_outer.addStretch(1)

        # =========================
        # TAB 4: SETTINGS
        # =========================
        tab_settings = QtWidgets.QWidget()
        self.tabs.addTab(tab_settings, self.tr.t("tab.settings", "Settings"))

        s_outer = QtWidgets.QVBoxLayout(tab_settings)
        s_outer.setContentsMargins(10, 10, 10, 10)
        s_outer.setSpacing(10)

        grp_cfg = QtWidgets.QGroupBox("Telemetry + UDP")
        s_outer.addWidget(grp_cfg)
        s = QtWidgets.QGridLayout(grp_cfg)

        self.grpLang = QtWidgets.QGroupBox(self.tr.t("settings.language_group", "Language"))
        s_outer.addWidget(self.grpLang)
        gl = QtWidgets.QGridLayout(self.grpLang)

        self.lblLang = QtWidgets.QLabel(self.tr.t("settings.language_label", "Language"))
        self.cmbLang = QtWidgets.QComboBox()

        # populate from lang/*.json
        langs = self.tr.available_languages() or ["en", "de"]
        names = Translator.language_display_names()

        # build (label, code) pairs
        items = []
        for code in langs:
            label = names.get(code, code)
            items.append((label, code))

        # sort by label (case-insensitive, Unicode-safe)
        items.sort(key=lambda x: x[0].casefold())

        self.cmbLang.clear()
        for label, code in items:
            self.cmbLang.addItem(label, code)

        # restore selection from config
        cur_lang = (self.cfg.language or "en").strip()
        idx = self.cmbLang.findData(cur_lang)
        if idx >= 0:
            self.cmbLang.setCurrentIndex(idx)

        self.btnApplyLang = QtWidgets.QPushButton(self.tr.t("settings.language_apply", "Apply language"))
        self.btnApplyLang.clicked.connect(self.apply_language)

        gl.addWidget(self.lblLang, 0, 0)
        gl.addWidget(self.cmbLang, 0, 1)
        gl.addWidget(self.btnApplyLang, 1, 0, 1, 2)


        self.lblFolder = QtWidgets.QLabel("Telemetry Folder: (not set)")
        self.btnPick = QtWidgets.QPushButton("Pick Folder…")
        self.btnPick.clicked.connect(self.pick_folder)

        self.chkUdp = QtWidgets.QCheckBox("UDP enabled (F1 SC/Wetter)")
        self.spinPort = QtWidgets.QSpinBox()
        self.spinPort.setRange(1024, 65535)
        self.spinPort.setSingleStep(1)

        self.btnApply = QtWidgets.QPushButton("Apply Settings")
        self.btnApply.clicked.connect(self.apply_settings)

        s.addWidget(self.lblFolder, 0, 0, 1, 3)
        s.addWidget(self.btnPick, 0, 3)

        s.addWidget(self.chkUdp, 1, 0, 1, 2)
        s.addWidget(QtWidgets.QLabel("Port:"), 1, 2)
        s.addWidget(self.spinPort, 1, 3)

        s.addWidget(self.btnApply, 2, 0, 1, 4)

        s_outer.addStretch(1)

        # Status bar (kept)
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

    def apply_language(self):
        new_lang = self.cmbLang.currentData() or "en"
        if not new_lang:
            return

        # persist
        self.cfg.language = new_lang
        save_config(self.cfg)

        # reload translator + update UI texts
        try:
            self.tr.load_language(new_lang)
        except Exception:
            # fallback to English if file missing/broken
            self.tr.load_language("en")
            self.cfg.language = "en"
            save_config(self.cfg)

        self._retranslate_ui()
        self.status.showMessage(self.tr.t("msg.language_applied", "Language applied."), 2500)

    def _retranslate_ui(self):
        # Window title
        self.setWindowTitle(self.tr.t("app.title", "SimRaceStrategist – Prototype"))

        # Tab titles
        self.tabs.setTabText(0, self.tr.t("tab.live", "Live"))
        self.tabs.setTabText(1, self.tr.t("tab.db", "Laps / DB"))
        self.tabs.setTabText(2, self.tr.t("tab.model", "Model"))
        self.tabs.setTabText(3, self.tr.t("tab.settings", "Settings"))

        # Group titles
        self.grpLive.setTitle(self.tr.t("live.group_title", "Live (F1 UDP)"))
        self.grpStrat.setTitle(self.tr.t("cards.group_title", "Strategy Cards (Prototype)"))
        self.grpDeg.setTitle(self.tr.t("model.deg_group", "Degradation model"))

        # DB tab
        self.btnRefreshDb.setText(self.tr.t("db.refresh", "Refresh"))

        # Model tab labels/button
        # NOTE: these are the static labels inside the layout (created inline).
        # For minimal change: only update the button + placeholder label.
        self.btnDeg.setText(self.tr.t("model.estimate_btn", "Estimate degradation + pit windows"))
        if self.lblDeg.text().strip() == "—":
            self.lblDeg.setText(self.tr.t("common.placeholder", "—"))

        # Settings tab
        self.btnPick.setText(self.tr.t("settings.pick_folder", "Pick Folder…"))
        self.chkUdp.setText(self.tr.t("settings.udp_enabled", "UDP enabled (F1 SC/Wetter)"))
        self.btnApply.setText(self.tr.t("settings.apply", "Apply Settings"))

        # Language group (if exists)
        if hasattr(self, "grpLang"):
            self.grpLang.setTitle(self.tr.t("settings.language_group", "Language"))
        if hasattr(self, "lblLang"):
            self.lblLang.setText(self.tr.t("settings.language_label", "Language"))
        if hasattr(self, "btnApplyLang"):
            self.btnApplyLang.setText(self.tr.t("settings.language_apply", "Apply language"))

        # Live labels: set defaults (actual content updated by _update_live_labels)
        self.lblSC.setText(self.tr.t("live.sc_na", "SC/VSC: n/a"))
        self.lblWeather.setText(self.tr.t("live.weather_na", "Weather: n/a"))
        self.lblRain.setText(self.tr.t("live.rain_na", "Rain(next): n/a"))
        self.lblRainAdvice.setText(self.tr.t("live.rain_pit_na", "Rain pit: n/a"))
        self.lblFieldShare.setText(self.tr.t("live.field_share_na", "Field: Inter/Wet share: n/a"))
        self.lblFieldDelta.setText(self.tr.t("live.field_delta_na", "Field: Δpace (I-S): n/a"))

    def _tr_keep(self, key: str, fallback: str) -> str:
        """
        Translate via JSON. If key does not exist, fallback is used.
        Use this for motorsport terms too (fallback may be English).
        """
        return self.tr.t(key, fallback)

    def _tr_action(self, action: str) -> str:
        """
        Maps engine action strings to i18n keys.
        Example: "STAY OUT" -> action.STAY_OUT
        If you want EN words in DE as well, set DE translation identical.
        """
        if not action:
            return self._tr_keep("common.na", "n/a")

        norm = action.strip().upper()
        # normalize spaces to underscore for keys
        key = "action." + "_".join(norm.split())
        return self._tr_keep(key, action)

    def _tr_tyre(self, tyre: str) -> str:
        """
        Maps tyre labels to i18n keys.
        Example: "INTER" -> tyre.INTER
        """
        if not tyre:
            return self._tr_keep("common.na", "n/a")

        norm = tyre.strip().upper()
        key = f"tyre.{norm}"
        return self._tr_keep(key, tyre)


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
            self.udp = F1UDPListener(self.cfg.udp_port, self._on_live_state, debug=self.cfg.udp_debug)
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
        try:
            # WIP/DEBUG UI:
            # This method formats near-raw telemetry + heuristic outputs into human-readable text.
            # The displayed values/format are not stable APIs and may change as the strategy logic matures.
            state = getattr(self, "_live_state", F1LiveState())

            sc = -1 if state.safety_car_status is None else int(state.safety_car_status)
            weather = -1 if state.weather is None else int(state.weather)

            rain_fc = getattr(state, "rain_fc_pct", None)
            rain_now = getattr(state, "rain_now_pct", None)

            rain_fc_i = -1 if rain_fc is None else int(rain_fc)
            rain_now_i = -1 if rain_now is None else int(rain_now)

            # --- SC/VSC ---
            sc_text = {
                0: self.tr.t("live.sc_green", "Green"),
                1: self.tr.t("live.sc_sc", "Safety Car"),
                2: self.tr.t("live.sc_vsc", "VSC"),
                3: self.tr.t("live.sc_formation", "Formation"),
            }.get(sc, "–")

            def _flag_text(v: int | None) -> str:
                if v is None:
                    return self.tr.t("common.na", "n/a")
                return {
                    -1: self.tr.t("common.na", "n/a"),
                    0: self.tr.t("live.flag_none", "None"),
                    1: self.tr.t("live.flag_green", "Green"),
                    2: self.tr.t("live.flag_blue", "Blue"),
                    3: self.tr.t("live.flag_yellow", "Yellow"),
                }.get(int(v), f"{self.tr.t('live.flag_unknown', 'Unknown')}({int(v)})")

            track_flag = getattr(state, "track_flag", None)
            player_flag = getattr(state, "player_fia_flag", None)

            flags_suffix = self.tr.t(
                "live.flags_short_fmt",
                " | Flags: Track={track} You={you}"
            ).format(track=_flag_text(track_flag), you=_flag_text(player_flag))

            sc_line = self.tr.t("live.sc_fmt", "SC/VSC: {status}").format(status=sc_text) + flags_suffix
            if self.lblSC.text() != sc_line:
                self.lblSC.setText(sc_line)

            # --- Weather enum (debug-style, raw) ---
            weather_line = self.tr.t("live.weather_enum_fmt", "Weather(enum): {w}").format(
                w=(weather if weather >= 0 else self.tr.t("common.na", "n/a"))
            )
            if self.lblWeather.text() != weather_line:
                self.lblWeather.setText(weather_line)

            # --- Forecast helper: stepwise lookup for forecast values at given horizons (minutes).
            # Only used for display text, not for final decision logic.
            def _fc_at(series, tmin):
                """
                Stepwise forecast lookup:
                returns the rain% of the nearest sample with timeOffset >= tmin.
                If all samples are < tmin, return the last available sample.
                """
                if not series:
                    return None

                # series: list[(min_from_now, rain_pct, weather_enum)] sorted by time
                best = None
                for tup in series:
                    try:
                        tm, pct, _w = tup
                    except Exception:
                        continue

                    try:
                        tm_i = int(tm)
                        pct_i = int(pct)
                    except Exception:
                        continue

                    # first sample at/after horizon
                    if tm_i >= int(tmin):
                        best = pct_i
                        break

                if best is not None:
                    return best

                # horizon beyond last sample -> last known
                try:
                    return int(series[-1][1])
                except Exception:
                    return None

            fc = getattr(state, "rain_fc_series", None) or []
            fc_list = []
            for t in (3, 5, 10, 15, 20):
                v = _fc_at(fc, t)
                if v is None:
                    fc_list.append(self.tr.t("common.na", "n/a"))
                else:
                    try:
                        fc_list.append(str(int(v)))
                    except Exception:
                        fc_list.append(self.tr.t("common.na", "n/a"))
            fc_txt = "/".join(fc_list)

            rain_now_txt = (rain_now_i if rain_now_i >= 0 else self.tr.t("common.na", "n/a"))
            rain_line = self.tr.t(
                "live.rain_line_fmt",
                "Rain: {now} | FC(3/5/10/15/20): {fc}"
            ).format(now=rain_now_txt, fc=fc_txt)
            if self.lblRain.text() != rain_line:
                self.lblRain.setText(rain_line)

            # --- Rain pit advice (WIP) ---
            try:
                rn = float(rain_fc_i) if rain_fc_i >= 0 else 0.0
            except Exception:
                rn = 0.0

            # Use LIVE tyre from UDP if available (prevents "Slicks unsafe" when you're actually on WET/INTER).
            current_tyre = None
            try:
                cat = (getattr(state, "player_tyre_cat", None) or "").upper().strip()  # "SLICK"/"INTER"/"WET"
                if cat in ("SLICK", "INTER", "WET"):
                    # Rain logic expects labels like "SLICK"/"INTER"/"WET"
                    current_tyre = cat
            except Exception:
                current_tyre = None

            # Fallback: UI selection (Model tab) if live value missing
            if not current_tyre:
                try:
                    current_tyre = self.cmbTyre.currentText()
                except Exception:
                    current_tyre = ""

            # --- fallbacks (weil die spinBoxes aktuell nicht existieren) ---
            laps_remaining = 25
            pit_loss_s = 20.0

            # best-effort: wenn du spinRaceLaps hast, nimm den als groben Placeholder
            if hasattr(self, "spinRaceLaps"):
                try:
                    laps_remaining = int(self.spinRaceLaps.value())
                except Exception:
                    pass

            # track name for DB lookup
            track = ""
            if hasattr(self, "cmbTrack"):
                try:
                    track = (self.cmbTrack.currentText() or "").strip()
                except Exception:
                    track = ""

            # DB rows: nicht jedes UI-Tick neu laden -> simple cache
            db_rows_list = None
            try:
                if track:
                    if getattr(self, "_db_cache_track", None) != track:
                        self._db_cache_track = track
                        self._db_cache_rows = laps_for_track(track, limit=5000)
                    db_rows_list = getattr(self, "_db_cache_rows", None)
            except Exception:
                db_rows_list = None

            # NEW RainEngine API
            out = self.rain_engine.update(
                state,
                track=track or "UNKNOWN",
                current_tyre=current_tyre,
                laps_remaining=laps_remaining,
                pit_loss_s=pit_loss_s,
                db_rows=db_rows_list,
                your_last_lap_s=self._your_last_lap_s,
            )

            # WIP/ADVISORY UI: heuristic suggestion only (no forced decisions).
            ad = out.advice

            action_ui = self._tr_action(ad.action)
            target_ui = self._tr_tyre(ad.target_tyre) if ad.target_tyre else self._tr_keep("common.na", "n/a")

            # NOTE: ad.reason is currently free text from the engine.
            # It is shown as-is (debug/heuristic transparency). If you want this fully localized,
            # we can move to reason_key + params later without breaking existing behavior.
            advice_line = self.tr.t(
                "live.rain_pit_advice_fmt",
                "Rain pit: {action} → {target} | wet={wet:.2f} conf={conf:.2f} | {reason}"
            ).format(
                action=action_ui,
                target=target_ui,
                wet=out.wetness,
                conf=out.confidence,
                reason=ad.reason
            )

            if self.lblRainAdvice.text() != advice_line:
                self.lblRainAdvice.setText(advice_line)

            # WIP/DEBUG UI: verbose internal diagnostics (can be noisy; shown for dev visibility).
            self.status.showMessage(out.debug)

            # --- Field signals (share + pace deltas) ---
            if state.inter_share is None or state.inter_count is None or state.slick_count is None:
                share_line = self.tr.t("live.field_share_na", "Field: Inter/Wet share: n/a")
            else:
                total = state.inter_count + state.slick_count
                share_line = self.tr.t(
                    "live.field_share_fmt",
                    "Field: Inter/Wet share: {pct:.0f}% ({inter}/{total})"
                ).format(pct=state.inter_share * 100.0, inter=state.inter_count, total=total)

            if self.lblFieldShare.text() != share_line:
                self.lblFieldShare.setText(share_line)

            # WIP/TELEMETRY SIGNAL (field-level pace):
            if state.pace_delta_inter_vs_slick_s is None:
                field_line = self.tr.t("live.field_delta_na", "Field: Δpace (I-S): n/a")
            else:
                field_line = self.tr.t(
                    "live.field_delta_fmt",
                    "Field: Δpace (I-S): {delta:+.2f}s"
                ).format(delta=state.pace_delta_inter_vs_slick_s)

            # WIP/LEARNING SIGNAL (player-specific):
            rc = getattr(state, "your_ref_counts", None) or "S:0 I:0 W:0"
            yd = getattr(state, "your_delta_inter_vs_slick_s", None)
            yw = getattr(state, "your_delta_wet_vs_slick_s", None)

            if yd is None and yw is None:
                your_line = self.tr.t("live.your_delta_na_fmt", "Your: Δ(I-S): n/a ({rc})").format(rc=rc)
            else:
                parts = []
                if yd is not None:
                    parts.append(self.tr.t("live.your_part_is_fmt", "Δ(I-S) {d:+.2f}s").format(d=yd))
                if yw is not None:
                    parts.append(self.tr.t("live.your_part_ws_fmt", "Δ(W-S) {d:+.2f}s").format(d=yw))
                your_line = self.tr.t("live.your_prefix", "Your: ") + ", ".join(parts) + f" ({rc})"

            txt = field_line + "\n" + your_line
            if self.lblFieldDelta.text() != txt:
                self.lblFieldDelta.setText(txt)

        except Exception as e:
            try:
                self.status.showMessage(f"UI update error: {type(e).__name__}: {e}", 12000)
            except Exception:
                pass
            print("[UI UPDATE ERROR]", type(e).__name__, e)

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
        #
        # Legacy optional cooldown (kept for reference):
        # - Was used to suppress duplicate FS events via a 1s time-based gate.
        # - Currently disabled because copy_to_cache() already waits for a stable file size
        #   and we additionally dedupe by (size, mtime_ns) above.
        #
        # Example (disabled):
        #   now = time.time()
        #   last_t = getattr(self, '_dedupe_time_sec', {}).get(key, 0)
        #   if now - last_t < 1.0: return
        #   self._dedupe_time_sec = getattr(self, '_dedupe_time_sec', {})
        #   self._dedupe_time_sec[key] = now

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

            # --- Time outliers → SHIFT / SLOW (per-tyre ONLY; no cross-tyre fallback!) ---
            SHIFT_SEC = 1.2  # moderate outlier vs normal pace on same tyre
            SLOW_SEC = 6.0  # big outlier (ERS recharge etc.) on same tyre

            # Collect lap times per tyre (ignore IN/OUT)
            times_by_tyre: dict[str, list[float]] = {}

            for j in idxs:
                if tags[j] in ("IN", "OUT"):
                    continue

                t = rows[j][7]  # lap_time_s
                tyre = rows[j][5]  # tyre
                if t is None or tyre is None:
                    continue

                try:
                    tf = float(t)
                except Exception:
                    continue

                if not (10.0 < tf < 400.0):
                    continue

                tyre_key = str(tyre).strip().upper()
                times_by_tyre.setdefault(tyre_key, []).append(tf)

            def median(ts: list[float]) -> float:
                s = sorted(ts)
                return s[len(s) // 2]

            # Baseline per tyre: if not enough samples, DON'T tag that tyre at all
            baseline: dict[str, float] = {}
            for tyre_key, ts in times_by_tyre.items():
                if len(ts) >= 3:
                    baseline[tyre_key] = median(ts)

            # Apply tagging per tyre
            for j in idxs:
                if tags[j] != "OK":
                    continue

                t = rows[j][7]
                tyre = rows[j][5]
                if t is None or tyre is None:
                    continue

                try:
                    tf = float(t)
                except Exception:
                    continue

                tyre_key = str(tyre).strip().upper()
                base = baseline.get(tyre_key)
                if base is None:
                    continue  # <3 laps on this tyre → do NOT tag (prevents "Inter slower than Slick" false SLOW)

                if tf > base + SLOW_SEC:
                    tags[j] = "SLOW"
                elif tf > base + SHIFT_SEC:
                    tags[j] = "SHIFT"

        # Tabelle rendern
        self.tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            # col 0: lap number
            lap_item = QtWidgets.QTableWidgetItem(str(lapno.get(r, "")))
            self.tbl.setItem(r, 0, lap_item)

            # cols 1...: original db columns (BUT hide session_uid from display)
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
        # UI log removed: show only the latest line in the status bar (optional)
        try:
            self.status.showMessage(line, 5000)  # 5s
        except Exception:
            pass

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
