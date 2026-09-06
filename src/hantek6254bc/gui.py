from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from .device import Capture, Hantek6254BC, RATE_TABLE, SUPPORTED_DEPTHS, SUPPORTED_VDIV, ScopeConfig


CHANNEL_COLORS = ("#ffd166", "#06d6a0", "#3ba7ff", "#ef476f")


def format_rate(rate: int) -> str:
    if rate >= 1_000_000:
        return f"{rate / 1_000_000:g} MS/s"
    return f"{rate / 1_000:g} kS/s"


def format_time(seconds: float) -> str:
    if seconds < 1e-6:
        return f"{seconds * 1e9:g} ns"
    if seconds < 1e-3:
        return f"{seconds * 1e6:g} µs"
    if seconds < 1:
        return f"{seconds * 1e3:g} ms"
    return f"{seconds:g} s"


def waveform_measurements(time_s: np.ndarray, volts: np.ndarray, vdiv: float) -> tuple[float, float | None]:
    """Return peak-to-peak voltage and a rising-crossing frequency estimate."""
    if len(volts) < 3:
        return 0.0, None
    vpp = float(np.ptp(volts))
    if vpp < vdiv * 0.25:
        return vpp, None
    centered = volts - (float(np.max(volts)) + float(np.min(volts))) / 2.0
    indices = np.flatnonzero((centered[:-1] <= 0) & (centered[1:] > 0))
    if len(indices) < 2:
        return vpp, None
    crossing_times = []
    for index in indices:
        y0, y1 = centered[index], centered[index + 1]
        fraction = 0.0 if y1 == y0 else float(-y0 / (y1 - y0))
        crossing_times.append(float(time_s[index] + fraction * (time_s[index + 1] - time_s[index])))
    periods = np.diff(crossing_times)
    periods = periods[periods > 0]
    if not len(periods):
        return vpp, None
    return vpp, float(1.0 / np.median(periods))


def run_gui(config: ScopeConfig, bus: int | None = None, address: int | None = None) -> int:
    try:
        import matplotlib
        matplotlib.use("qtagg")
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
        from matplotlib.figure import Figure
        from PyQt6.QtCore import QTimer
        from PyQt6.QtGui import QCloseEvent, QKeySequence, QShortcut
        from PyQt6.QtWidgets import (
            QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
            QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
            QMainWindow, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
        )
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError(
            "The oscilloscope GUI requires Qt; rerun ./install.sh from a Pop!_OS desktop terminal."
        ) from exc

    class ScopeWindow(QMainWindow):
        def __init__(self, scope: Hantek6254BC):
            super().__init__()
            self.scope = scope
            self.config = scope.config
            self.last_capture: Capture | None = None
            self.running = True
            self.closed = False
            self.capture_timer = QTimer(self)
            self.capture_timer.setSingleShot(True)
            self.capture_timer.timeout.connect(self._tick)
            self.setWindowTitle("Hantek 6254BC Oscilloscope")
            self.resize(1500, 900)

            root = QWidget()
            root_layout = QHBoxLayout(root)
            root_layout.setContentsMargins(6, 6, 6, 6)
            root_layout.addWidget(self._make_controls())

            plot_widget = QWidget()
            plot_layout = QVBoxLayout(plot_widget)
            plot_layout.setContentsMargins(0, 0, 0, 0)
            self.figure = Figure(facecolor="#15191e")
            self.canvas = FigureCanvasQTAgg(self.figure)
            self.axes = self.figure.add_subplot(111)
            self.toolbar = NavigationToolbar2QT(self.canvas, self)
            plot_layout.addWidget(self.toolbar)
            plot_layout.addWidget(self.canvas, 1)
            root_layout.addWidget(plot_widget, 1)
            self.setCentralWidget(root)

            self.lines = []
            self.trigger_line = None
            self._configure_axes()
            self._update_run_button()
            self.statusBar().showMessage("Connected — acquiring")

            QShortcut(QKeySequence("Space"), self, activated=self._toggle_run)
            QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_csv)
            self.capture_timer.start(0)

        def _make_controls(self):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMinimumWidth(285)
            scroll.setMaximumWidth(340)
            panel = QWidget()
            layout = QVBoxLayout(panel)

            acquisition = QGroupBox("Acquisition")
            acquisition_form = QFormLayout(acquisition)
            self.rate_combo = QComboBox()
            self.depth_combo = QComboBox()
            for depth in SUPPORTED_DEPTHS:
                self.depth_combo.addItem(f"{depth:,} samples/channel", depth)
            self.depth_combo.setCurrentIndex(self.depth_combo.findData(self.config.depth))
            self._fill_rate_combo(self.config.depth, self.config.sample_rate)
            self.depth_combo.currentIndexChanged.connect(self._depth_changed)
            acquisition_form.addRow("Time / division", self.rate_combo)
            acquisition_form.addRow("Memory", self.depth_combo)
            layout.addWidget(acquisition)

            self.channel_checks = []
            self.vdiv_combos = []
            self.position_spins = []
            for channel in range(4):
                group = QGroupBox(f"Channel {channel + 1}")
                group.setStyleSheet(f"QGroupBox {{ color: {CHANNEL_COLORS[channel]}; font-weight: bold; }}")
                form = QFormLayout(group)
                show = QCheckBox("Show trace")
                show.setChecked(channel == 0)
                show.stateChanged.connect(self._update_visibility)
                vdiv = QComboBox()
                for value in SUPPORTED_VDIV:
                    label = f"{value * 1000:g} mV/div" if value < 1 else f"{value:g} V/div"
                    vdiv.addItem(label, value)
                vdiv.setCurrentIndex(vdiv.findData(self.config.volts_per_div[channel]))
                position = QDoubleSpinBox()
                position.setRange(-4.0, 4.0)
                position.setSingleStep(0.25)
                position.setSuffix(" div")
                position.valueChanged.connect(self._redraw_last_capture)
                form.addRow(show)
                form.addRow("Scale", vdiv)
                form.addRow("Position", position)
                self.channel_checks.append(show)
                self.vdiv_combos.append(vdiv)
                self.position_spins.append(position)
                layout.addWidget(group)

            trigger = QGroupBox("Trigger")
            trigger_form = QFormLayout(trigger)
            self.trigger_source = QComboBox()
            for channel in range(4):
                self.trigger_source.addItem(f"CH{channel + 1}", channel)
            self.trigger_source.setCurrentIndex(self.config.trigger_source)
            self.trigger_level = QDoubleSpinBox()
            self.trigger_level.setDecimals(3)
            self.trigger_level.setSuffix(" V")
            self.trigger_slope = QComboBox()
            self.trigger_slope.addItem("Rising edge", "rising")
            self.trigger_slope.addItem("Falling edge", "falling")
            self.trigger_slope.setCurrentIndex(self.trigger_slope.findData(self.config.trigger_slope))
            self.trigger_sweep = QComboBox()
            self.trigger_sweep.addItem("Auto", "auto")
            self.trigger_sweep.addItem("Normal", "normal")
            self.trigger_sweep.setCurrentIndex(self.trigger_sweep.findData(self.config.trigger_sweep))
            self.trigger_source.currentIndexChanged.connect(self._update_trigger_range)
            for vdiv in self.vdiv_combos:
                vdiv.currentIndexChanged.connect(self._update_trigger_range)
            self._update_trigger_range()
            self.trigger_level.setValue(self.config.trigger_level)
            trigger_form.addRow("Source", self.trigger_source)
            trigger_form.addRow("Level", self.trigger_level)
            trigger_form.addRow("Slope", self.trigger_slope)
            trigger_form.addRow("Mode", self.trigger_sweep)
            layout.addWidget(trigger)

            buttons = QGridLayout()
            self.run_button = QPushButton()
            self.run_button.clicked.connect(self._toggle_run)
            single_button = QPushButton("Single")
            single_button.clicked.connect(self._single)
            apply_button = QPushButton("Apply settings")
            apply_button.clicked.connect(self._apply_settings)
            save_button = QPushButton("Save CSV…")
            save_button.clicked.connect(self._save_csv)
            buttons.addWidget(self.run_button, 0, 0)
            buttons.addWidget(single_button, 0, 1)
            buttons.addWidget(apply_button, 1, 0, 1, 2)
            buttons.addWidget(save_button, 2, 0, 1, 2)
            layout.addLayout(buttons)

            help_label = QLabel("Space: Run/Stop    Ctrl+S: Save CSV\nUse the plot toolbar to zoom or pan.")
            help_label.setWordWrap(True)
            help_label.setStyleSheet("color: #777;")
            layout.addWidget(help_label)
            layout.addStretch(1)
            scroll.setWidget(panel)
            return scroll

        def _fill_rate_combo(self, depth: int, selected_rate: int) -> None:
            self.rate_combo.clear()
            for rate in reversed(tuple(RATE_TABLE)):
                time_per_div = depth / rate / 10.0
                self.rate_combo.addItem(f"{format_time(time_per_div)}/div  ({format_rate(rate)})", rate)
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(selected_rate))

        def _depth_changed(self) -> None:
            selected_rate = int(self.rate_combo.currentData())
            self._fill_rate_combo(int(self.depth_combo.currentData()), selected_rate)

        def _update_trigger_range(self) -> None:
            source = int(self.trigger_source.currentData())
            vdiv = float(self.vdiv_combos[source].currentData())
            self.trigger_level.setDecimals(4 if vdiv < 0.1 else 3)
            self.trigger_level.setRange(-4.0 * vdiv, 4.0 * vdiv)
            self.trigger_level.setSingleStep(vdiv / 4.0)

        def _settings_from_controls(self, sweep: str | None = None) -> ScopeConfig:
            return ScopeConfig(
                sample_rate=int(self.rate_combo.currentData()),
                depth=int(self.depth_combo.currentData()),
                volts_per_div=tuple(float(combo.currentData()) for combo in self.vdiv_combos),
                trigger_source=int(self.trigger_source.currentData()),
                trigger_level=float(self.trigger_level.value()),
                trigger_slope=str(self.trigger_slope.currentData()),
                trigger_sweep=sweep or str(self.trigger_sweep.currentData()),
            )

        def _apply_settings(self, sweep: str | None = None) -> bool:
            was_running = self.running
            self.running = False
            self.capture_timer.stop()
            QApplication.processEvents()
            try:
                new_config = self._settings_from_controls(sweep=sweep)
                self.scope.config = new_config
                self.scope.configure()
                self.config = new_config
                self._fill_rate_combo(new_config.depth, new_config.sample_rate)
                self._configure_axes()
                self.statusBar().showMessage("Settings applied", 2500)
            except Exception as exc:
                QMessageBox.critical(self, "Could not apply settings", str(exc))
                self.scope.config = self.config
                self.running = was_running
                self._update_run_button()
                return False
            self.running = was_running
            self._update_run_button()
            if self.running:
                self.capture_timer.start(0)
            return True

        def _configure_axes(self) -> None:
            self.axes.clear()
            self.axes.set_facecolor("#090b0d")
            self.axes.set_ylim(-4.1, 4.1)
            duration = (self.config.depth - 1) / self.config.sample_rate
            factor, unit = (1e6, "µs") if duration < 0.01 else (1e3, "ms") if duration < 10 else (1, "s")
            self.axes.set_xlim(-duration * factor / 2, duration * factor / 2)
            self.axes.set_xlabel(f"Time ({unit})")
            self.axes.set_ylabel("Vertical divisions")
            self.axes.set_title("Hantek 6254BC", color="#eeeeee")
            self.axes.tick_params(colors="#bbbbbb")
            for spine in self.axes.spines.values():
                spine.set_color("#777777")
            self.axes.grid(True, which="major", color="#68717a", alpha=0.45, linewidth=0.8)
            self.axes.set_xticks(np.linspace(-duration * factor / 2, duration * factor / 2, 11))
            self.axes.set_yticks(np.arange(-4, 5, 1))
            self.lines = []
            for channel, color in enumerate(CHANNEL_COLORS):
                (line,) = self.axes.plot([], [], color=color, linewidth=1.1, label=f"CH{channel + 1}")
                line.set_visible(self.channel_checks[channel].isChecked())
                self.lines.append(line)
            self.trigger_line = self.axes.axhline(0, color="#eeeeee", linestyle="--", linewidth=0.8, alpha=0.7)
            self.axes.legend(loc="upper right", ncols=4, facecolor="#15191e", labelcolor="white")
            self.figure.tight_layout()
            self.canvas.draw_idle()

        def _display_capture(self, capture: Capture) -> None:
            self.last_capture = capture
            duration = capture.time_s[-1] if len(capture.time_s) else 0.0
            factor = 1e6 if duration < 0.01 else 1e3 if duration < 10 else 1
            x = (capture.time_s - duration / 2.0) * factor
            measurements = []
            for channel, line in enumerate(self.lines):
                vdiv = self.config.volts_per_div[channel]
                y = capture.volts[channel] / vdiv + self.position_spins[channel].value()
                line.set_data(x, y)
                if self.channel_checks[channel].isChecked():
                    vpp, frequency = waveform_measurements(capture.time_s, capture.volts[channel], vdiv)
                    text = f"CH{channel + 1}: {vpp:.3g} Vpp"
                    if frequency is not None:
                        text += f", {frequency:.4g} Hz"
                    measurements.append(text)
            source = self.config.trigger_source
            trigger_divisions = (
                self.config.trigger_level / self.config.volts_per_div[source]
                + self.position_spins[source].value()
            )
            self.trigger_line.set_ydata((trigger_divisions, trigger_divisions))
            self.trigger_line.set_color(CHANNEL_COLORS[source])
            self.trigger_line.set_visible(-4.1 <= trigger_divisions <= 4.1)
            self.statusBar().showMessage("   |   ".join(measurements) or "No channels displayed")
            self.canvas.draw_idle()

        def _tick(self) -> None:
            if self.closed or not self.running:
                return
            try:
                capture = self.scope.capture()
                self._display_capture(capture)
            except Exception as exc:
                self.running = False
                self._update_run_button()
                self.statusBar().showMessage(f"Stopped: {exc}")
                QMessageBox.critical(self, "Acquisition stopped", str(exc))
                return
            self.capture_timer.start(100)

        def _toggle_run(self) -> None:
            self.running = not self.running
            self._update_run_button()
            if self.running:
                self.statusBar().showMessage("Running")
                self.capture_timer.start(0)
            else:
                self.capture_timer.stop()
                self.statusBar().showMessage("Stopped")

        def _update_run_button(self) -> None:
            self.run_button.setText("Stop" if self.running else "Run")

        def _single(self) -> None:
            self.running = False
            self.capture_timer.stop()
            self._update_run_button()
            if not self._apply_settings(sweep="single"):
                return
            self.running = False
            self._update_run_button()
            QApplication.processEvents()
            try:
                self._display_capture(self.scope.capture())
                self.statusBar().showMessage("Single acquisition complete")
            except Exception as exc:
                QMessageBox.critical(self, "Single acquisition failed", str(exc))

        def _update_visibility(self) -> None:
            for checkbox, line in zip(self.channel_checks, self.lines):
                line.set_visible(checkbox.isChecked())
            self.canvas.draw_idle()
            if self.last_capture is not None:
                self._display_capture(self.last_capture)

        def _redraw_last_capture(self) -> None:
            if self.last_capture is not None:
                self._display_capture(self.last_capture)

        def _save_csv(self) -> None:
            if self.last_capture is None:
                QMessageBox.information(self, "Nothing to save", "Acquire a waveform first.")
                return
            filename, _ = QFileDialog.getSaveFileName(self, "Save waveform", "capture.csv", "CSV files (*.csv)")
            if not filename:
                return
            path = Path(filename)
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("time_s", "ch1_v", "ch2_v", "ch3_v", "ch4_v"))
                writer.writerows(zip(self.last_capture.time_s, *self.last_capture.volts))
            self.statusBar().showMessage(f"Saved {path}", 4000)

        def closeEvent(self, event: QCloseEvent) -> None:
            self.closed = True
            self.running = False
            self.capture_timer.stop()
            self.scope.close()
            event.accept()

    app = QApplication.instance() or QApplication([])
    scope = Hantek6254BC.open(config=config, bus=bus, address=address)
    try:
        window = ScopeWindow(scope)
    except Exception:
        scope.close()
        raise
    window.show()
    return int(app.exec())
