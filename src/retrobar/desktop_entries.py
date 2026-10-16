"""Freedesktop .desktop lookups (app id → icon) and detached process launching."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from functools import cache
from pathlib import Path

from PyQt6.QtGui import QIcon

from .applications import application_dirs as _application_dirs
from .applications import desktop_id as _desktop_id
from .applications import read_entry as _read_entry

log = logging.getLogger(__name__)
# Field codes in Exec lines that expand to nothing when no files are passed.
_FIELD_CODE = re.compile(r"^%[fFuUdDnNvmick]$")


@cache
def _wm_class_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for directory in reversed(_application_dirs()):
        for path in directory.rglob("*.desktop"):
            wm_class = _read_entry(path).get("StartupWMClass")
            if wm_class:
                index[wm_class.lower()] = path
    return index


@cache
def find_desktop_file(app_id: str) -> Path | None:
    if not app_id:
        return None
    candidates = [app_id, app_id.lower()]
    if "." in app_id:
        candidates.append(app_id.rsplit(".", 1)[-1].lower())
    for directory in _application_dirs():
        for name in candidates:
            path = directory / f"{name}.desktop"
            if path.is_file():
                return path
    return _wm_class_index().get(app_id.lower())


def icon_for_app(app_id: str) -> QIcon | None:
    path = find_desktop_file(app_id)
    names = []
    if path is not None:
        names.append(_read_entry(path).get("Icon", ""))
    names += [app_id, app_id.lower()]
    for name in names:
        if not name:
            continue
        if os.path.isabs(name) and os.path.isfile(name):
            return QIcon(name)
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return None


def launch(command: str | list[str], activation_token: str = "") -> bool:
    """Start a program detached from RetroBar."""
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv:
        return False
    if shutil.which(argv[0]) is None:
        log.warning("Cannot launch %s: not found", argv[0])
        return False
    env = dict(os.environ)
    if activation_token:
        env["XDG_ACTIVATION_TOKEN"] = activation_token
    try:
        subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except OSError as exc:
        log.warning("Cannot launch %s: %s", argv, exc)
        return False
    return True


def _resolve(app: str | Path) -> tuple[Path, str] | None:
    """A .desktop path and its desktop file ID, from an app id, desktop file ID or path."""
    candidate = Path(app)
    if candidate.is_absolute() and candidate.is_file():
        for directory in _application_dirs():
            if candidate.is_relative_to(directory):
                return candidate, _desktop_id(candidate, directory)
        return candidate, ""
    name = str(app).removesuffix(".desktop")
    for directory in _application_dirs():
        path = directory / f"{name}.desktop"
        if path.is_file():
            return path, name
    path = find_desktop_file(name)
    if path is None:
        return None
    for directory in _application_dirs():
        if path.is_relative_to(directory):
            return path, _desktop_id(path, directory)
    return path, path.stem


def exec_argv(path: Path) -> list[str]:
    """Exec= with field codes removed, wrapped in a terminal when Terminal=true."""
    entry = _read_entry(path)
    try:
        argv = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return []
    out = []
    for arg in argv:
        if _FIELD_CODE.match(arg):
            if arg == "%i" and entry.get("Icon"):
                out += ["--icon", entry["Icon"]]
            elif arg == "%c" and entry.get("Name"):
                out.append(entry["Name"])
            elif arg == "%k":
                out.append(str(path))
            continue
        out.append(arg.replace("%%", "%"))
    if out and entry.get("Terminal", "").lower() == "true":
        terminal = shutil.which("konsole") or shutil.which("x-terminal-emulator") or shutil.which("xterm")
        if terminal:
            out = [terminal, "-e", *out]
    return out


def launch_desktop_file(app: str | Path, activation_token: str = "") -> bool:
    """Start an application from an app id, a desktop file ID, or a .desktop path."""
    resolved = _resolve(app)
    if resolved is None:
        return False
    path, did = resolved
    service = did.removesuffix(".desktop")
    if service and shutil.which("kstart"):
        return launch(["kstart", "--application", service], activation_token)
    if service and shutil.which("gtk-launch"):
        return launch(["gtk-launch", service], activation_token)
    return launch(exec_argv(path), activation_token)


def open_path(path: str | Path, activation_token: str = "") -> bool:
    """Open a file or folder with its default application."""
    opener = "kde-open" if shutil.which("kde-open") else "xdg-open"
    return launch([opener, str(path)], activation_token)
