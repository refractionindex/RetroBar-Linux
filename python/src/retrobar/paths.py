"""Filesystem locations: bundled data from the Windows project, and per-user XDG directories."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def bundled_root() -> Path:
    """Directory that holds Themes/, Languages/ and Resources/ (the Windows project's RetroBar folder)."""
    override = os.environ.get("RETROBAR_DATA_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "RetroBar"
        if (candidate / "Themes" / "System.xaml").is_file():
            return candidate
    installed = Path(__file__).resolve().parent / "data"
    if (installed / "Themes" / "System.xaml").is_file():
        return installed
    raise FileNotFoundError("Cannot find RetroBar theme data; set RETROBAR_DATA_DIR")


def xdg_data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def user_data_dir() -> Path:
    """Equivalent of %localappdata%\\RetroBar for installed themes and languages."""
    return xdg_data_home() / "RetroBar"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "retrobar"


def settings_path() -> Path:
    return config_dir() / "settings.json"
