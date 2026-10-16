"""RetroBar Properties: the settings dialog (PropertiesWindow.xaml), as a Qt Widgets window."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .settings import Settings
from .theme import Theme

SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]
# Linux-only options have no entries in the language dictionaries.
LINUX_STRINGS = {"classic_start_menu": "Use the _classic Start menu (off: Plasma application launcher)"}


def _mnemonic(text: str) -> str:
    return text.replace("&", "&&").replace("_", "&")


class PropertiesDialog(QDialog):
    def __init__(self, settings: Settings, theme: Theme) -> None:
        super().__init__()
        self.settings = settings
        self.theme = theme
        self.setWindowTitle(self._s("retrobar_properties"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        tabs = QTabWidget()
        tabs.addTab(self._taskbar_tab(), _mnemonic(self._s("taskbar_tab")))
        tabs.addTab(self._advanced_tab(), _mnemonic(self._s("advanced_tab")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self._s("ok_dialog"))
        buttons.accepted.connect(self.accept)
        version = QLabel(self._s("version").replace("{0}", __version__))
        version.setEnabled(False)
        bottom = QHBoxLayout()
        bottom.addWidget(version)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addLayout(bottom)

    def _s(self, key: str) -> str:
        return self.theme.string(key, LINUX_STRINGS.get(key))

    # --- widgets bound to settings ---------------------------------------------------------------------------
    def _check(self, key: str, setting: str, on_change: Callable[[bool], None] | None = None) -> QCheckBox:
        box = QCheckBox(_mnemonic(self._s(key)))
        box.setChecked(bool(getattr(self.settings, setting)))

        def changed(state: int) -> None:
            value = state == Qt.CheckState.Checked.value
            setattr(self.settings, setting, value)
            if on_change:
                on_change(value)

        box.stateChanged.connect(changed)
        return box

    def _combo(self, options: list[str], current: int, apply: Callable[[int], None]) -> QComboBox:
        combo = QComboBox()
        combo.addItems(options)
        combo.setCurrentIndex(max(0, min(current, len(options) - 1)))
        combo.currentIndexChanged.connect(apply)
        return combo

    def _labelled(self, form: QFormLayout, key: str, widget: QWidget, tip_key: str | None = None) -> None:
        label = QLabel(_mnemonic(self._s(key)))
        label.setBuddy(widget)
        if tip_key:
            widget.setToolTip(self._s(tip_key))
        form.addRow(label, widget)

    # --- tabs ---------------------------------------------------------------------------------------------------
    def _taskbar_tab(self) -> QWidget:
        s = self.settings
        page = QWidget()
        layout = QVBoxLayout(page)

        appearance = QGroupBox(self._s("taskbar_appearance"))
        form = QFormLayout(appearance)
        themes = self.theme.themes()
        self.theme_combo = self._combo(themes, themes.index(s.Theme) if s.Theme in themes else 0, lambda i: setattr(s, "Theme", self.theme_combo.itemText(i)))
        theme_row = QHBoxLayout()
        theme_row.addWidget(self.theme_combo, 1)
        install = QPushButton("…")
        install.setToolTip("Install a theme (.xaml)")
        install.clicked.connect(self._install_theme)
        theme_row.addWidget(install)
        theme_widget = QWidget()
        theme_widget.setLayout(theme_row)
        theme_row.setContentsMargins(0, 0, 0, 0)
        self._labelled(form, "theme_text", theme_widget, "theme_tip")
        languages = self.theme.languages()
        language_combo = self._combo(languages, languages.index(s.Language) if s.Language in languages else 0, lambda i: setattr(s, "Language", languages[i]))
        self._labelled(form, "language_text", language_combo, "language_tip")
        locations = self.theme.string_list("location_values") or ["Left", "Top", "Right", "Bottom"]
        location = self._combo(locations, int(s.Edge), lambda i: setattr(s, "Edge", i))
        self._labelled(form, "location_text", location, "location_tip")
        rows = QSpinBox()
        rows.setRange(1, 6)
        rows.setValue(int(s.RowCount))
        rows.valueChanged.connect(lambda v: setattr(s, "RowCount", int(v)))
        self._labelled(form, "rowcount_text", rows, "rowcount_tip")
        scale_labels = [f"{int(x * 100)}%" for x in SCALES]
        current = min(range(len(SCALES)), key=lambda i: abs(SCALES[i] - float(s.TaskbarScale)))
        scale = self._combo(scale_labels, current, lambda i: setattr(s, "TaskbarScale", SCALES[i]))
        form.addRow(QLabel(self._s("taskbar_scale")), scale)
        layout.addWidget(appearance)

        general = QGroupBox(self._s("general_heading"))
        g = QVBoxLayout(general)
        g.addWidget(self._check("lock_taskbar", "LockTaskbar"))
        g.addWidget(self._check("enable_auto_hide", "AutoHide"))
        g.addWidget(self._check("show_multi_mon", "ShowMultiMon"))
        g.addWidget(self._check("add_show_desktop_button", "ShowDesktopButton"))
        g.addWidget(self._check("allow_font_smoothing", "AllowFontSmoothing"))
        g.addWidget(self._check("allow_font_smoothing_menu", "AllowFontSmoothingMenu"))
        layout.addWidget(general)

        clock = QGroupBox(self._s("clock_heading"))
        c = QVBoxLayout(clock)
        c.addWidget(self._check("show_clock", "ShowClock"))
        c.addWidget(self._check("show_clock_seconds", "ShowClockSeconds"))
        layout.addWidget(clock)
        layout.addStretch(1)
        return page

    def _advanced_tab(self) -> QWidget:
        s = self.settings
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        middle = self.theme.string_list("middle_mouse_task_action_values") or ["Do nothing", "Open a new instance", "Close the task"]
        self._labelled(form, "middle_mouse_task_action", self._combo(middle, int(s.TaskMiddleClickAction), lambda i: setattr(s, "TaskMiddleClickAction", i)))
        clock_actions = self.theme.string_list("clock_click_action_values") or ["Do nothing", "Open calendar"]
        self._labelled(form, "clock_click_action", self._combo(clock_actions, int(s.ClockClickAction), lambda i: setattr(s, "ClockClickAction", i)))
        layout.addLayout(form)

        fmt_row = QHBoxLayout()
        override = self._check("override_clock_format", "OverrideClockFormat")
        fmt = QLineEdit(s.ClockFormat)
        fmt.setEnabled(bool(s.OverrideClockFormat))
        override.toggled.connect(fmt.setEnabled)
        fmt.textChanged.connect(lambda text: setattr(s, "ClockFormat", text))
        fmt_row.addWidget(override)
        fmt_row.addWidget(fmt, 1)
        layout.addLayout(fmt_row)
        warning = QLabel(self._s("clock_format_warning"))
        warning.setWordWrap(True)
        warning.setEnabled(False)
        layout.addWidget(warning)

        layout.addWidget(self._check("classic_start_menu", "ClassicStartMenu"))
        start_row = QFormLayout()
        start = QLineEdit(s.StartMenuCommand)
        start.setPlaceholderText("Optional; overrides the Start menu")
        start.textChanged.connect(lambda text: setattr(s, "StartMenuCommand", text))
        start_row.addRow(QLabel("Start button command:"), start)
        layout.addLayout(start_row)

        layout.addWidget(self._check("show_exit_menu_item", "ShowExitMenuItem"))
        layout.addWidget(self._check("debug_logging", "DebugLogging"))
        open_folder = QPushButton(self._s("open_custom_themes_folder"))
        open_folder.clicked.connect(self._open_themes_folder)
        layout.addWidget(open_folder, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    # --- themes folder ---------------------------------------------------------------------------------------------
    def _install_theme(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self._s("theme_text").replace("_", "").rstrip(":"), str(Path.home()), "RetroBar themes (*.xaml)")
        if not path:
            return
        target_dir = self.theme.theme_install_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(path).name
        try:
            shutil.copyfile(path, target)
        except OSError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        self.theme_combo.addItems(self.theme.themes())
        self.theme_combo.blockSignals(False)
        self.theme_combo.setCurrentText(target.stem)

    def _open_themes_folder(self) -> None:
        folder = self.theme.theme_install_dir()
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)], start_new_session=True)
