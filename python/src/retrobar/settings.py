"""Persistent settings stored as JSON with the same property names as the Windows settings.json."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)


class Edge(IntEnum):
    """Matches ManagedShell's AppBarEdge (and Win32 ABE_*) so settings files are interchangeable."""

    LEFT = 0
    TOP = 1
    RIGHT = 2
    BOTTOM = 3


class TaskMiddleClick(IntEnum):
    DO_NOTHING = 0
    OPEN_NEW_INSTANCE = 1
    CLOSE_TASK = 2


class ClockClick(IntEnum):
    DO_NOTHING = 0
    OPEN_AERO_CALENDAR = 1
    OPEN_MODERN_CALENDAR = 2
    OPEN_NOTIFICATION_CENTER = 3


DEFAULTS: dict[str, Any] = {
    "Language": "System",
    "Theme": "Windows 95-98",
    "ShowInputLanguage": False,
    "ShowClock": True,
    "ShowClockSeconds": False,
    "OverrideClockFormat": False,
    "ClockFormat": "ddd MMM d  h:mm:ss tt",
    "ShowDesktopButton": False,
    "ShowMultiMon": False,
    "CollapseNotifyIcons": False,
    "AllowFontSmoothing": False,
    "AllowFontSmoothingMenu": True,
    "Edge": int(Edge.BOTTOM),
    "RowCount": 1,
    "TaskbarWidth": 1,
    "TaskbarScale": 1.0,
    "AutoHide": False,
    "LockTaskbar": False,
    "ShowTaskBadges": True,
    "TaskMiddleClickAction": int(TaskMiddleClick.OPEN_NEW_INSTANCE),
    "ClockClickAction": int(ClockClick.OPEN_NOTIFICATION_CENTER),
    "ShowExitMenuItem": True,
    "DebugLogging": False,
    # Linux-only: command for the Start button; when set it wins over everything else.
    "StartMenuCommand": "",
    # Linux-only: RetroBar's own classic Start menu; off means Plasma's application launcher.
    "ClassicStartMenu": True,
}


class Settings:
    """Dictionary-backed settings with change notification.

    Unknown keys from a Windows settings.json are preserved on save so the file stays
    usable by both versions.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.settings_path()
        self._values: dict[str, Any] = dict(DEFAULTS)
        self._listeners: list[Callable[[str], None]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings file %s: %s", self._path, exc)
            return
        if isinstance(data, dict):
            for key, value in data.items():
                default = DEFAULTS.get(key)
                if default is not None and not isinstance(value, type(default)):
                    if isinstance(default, float) and isinstance(value, int):
                        value = float(value)
                    else:
                        continue
                self._values[key] = value

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._values, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if self._values.get(name) == value:
            return
        self._values[name] = value
        try:
            self.save()
        except OSError as exc:
            log.error("Could not save settings: %s", exc)
        for listener in list(self._listeners):
            listener(name)

    @property
    def edge(self) -> Edge:
        try:
            return Edge(self._values["Edge"])
        except ValueError:
            return Edge.BOTTOM
