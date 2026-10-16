from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QLocale  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from retrobar import paths  # noqa: E402
from retrobar.theme import install_font_substitutes, install_icon_theme  # noqa: E402

BUNDLED = paths.bundled_root()
THEME_NAMES = sorted(p.stem for p in (BUNDLED / "Themes").glob("*.xaml"))
LANGUAGE_NAMES = sorted(p.stem for p in (BUNDLED / "Languages").glob("*.xaml"))


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
    app = QApplication.instance() or QApplication([])
    install_icon_theme()
    install_font_substitutes()
    return app


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep tests away from the user's settings and installed themes."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path
