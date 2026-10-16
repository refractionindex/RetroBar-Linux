"""Classic Start menu: application catalog, search, recent documents, keyboard translation and menu layout."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

from retrobar import applications, desktop_entries
from retrobar.applications import LOST_AND_FOUND, Application, folder_for, recent_documents, scan, search
from retrobar.theme import Theme
from retrobar.ui import elements
from retrobar.ui.menu import MenuItem, MenuView
from retrobar.ui.start_menu import StartActions, StartMenuView, banner_text, folder_label, program_items, start_items
from retrobar.wayland import keyboard


def write_entry(directory: Path, name: str, **keys: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body = {"Type": "Application", "Exec": "true", **keys}
    path.write_text("[Desktop Entry]\n" + "".join(f"{k}={v}\n" for k, v in body.items()) + "[Desktop Action new]\nName=Ignored\n")
    return path


@pytest.fixture
def app_dirs(tmp_path: Path) -> tuple[Path, Path]:
    user = tmp_path / "user" / "applications"
    system = tmp_path / "system" / "applications"
    write_entry(system, "editor.desktop", Name="Editor", Categories="Utility;TextEditor;")
    write_entry(user, "editor.desktop", Name="My Editor", Categories="Development;")
    write_entry(system, "hidden.desktop", Name="Hidden", NoDisplay="true")
    write_entry(system, "masked.desktop", Name="Masked")
    write_entry(user, "masked.desktop", Name="Masked", Hidden="true")
    write_entry(system, "gnome-only.desktop", Name="GNOME Only", OnlyShowIn="GNOME;")
    write_entry(system, "kde-only.desktop", Name="KDE Only", OnlyShowIn="KDE;", Categories="System;")
    write_entry(system, "not-kde.desktop", Name="Not KDE", NotShowIn="KDE;")
    write_entry(system, "missing.desktop", Name="Missing", TryExec="/nonexistent/binary")
    write_entry(system, "link.desktop", Name="Link", Type="Link")
    write_entry(system / "kde", "game.desktop", Name="Chess", **{"Name[de]": "Schach", "Categories": "Game;BoardGame;"})
    return user, system


def test_scan_filters_and_ids(app_dirs, monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    apps = {a.desktop_id: a for a in scan(list(app_dirs), ["KDE"])}
    assert set(apps) == {"editor.desktop", "kde-only.desktop", "kde-game.desktop"}
    assert apps["editor.desktop"].name == "My Editor"  # the user directory wins
    assert apps["kde-game.desktop"].folder == "Games"


def test_scan_localized_names(app_dirs, monkeypatch):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    names = {a.desktop_id: a.name for a in scan(list(app_dirs), ["KDE"])}
    assert names["kde-game.desktop"] == "Schach"


def test_folder_mapping():
    assert folder_for(["Network", "WebBrowser"]) == "Internet"
    assert folder_for(["Game", "Utility"]) == "Games"
    assert folder_for(["AudioVideo", "Player"]) == "Multimedia"
    assert folder_for(["Qt", "KDE"]) == LOST_AND_FOUND


def test_folders_put_lost_and_found_last():
    apps = [Application("a.desktop", Path("a"), "A", categories=["Qt"]), Application("b.desktop", Path("b"), "B", categories=["Utility"])]
    assert [name for name, _ in applications.folders(apps)] == ["Utilities", LOST_AND_FOUND]


def test_search_ranking():
    def app(name: str, **kw) -> Application:
        return Application(name.lower() + ".desktop", Path(name), name, **kw)

    apps = [
        app("Okular", generic_name="Document Viewer"),
        app("Dolphin", generic_name="File Manager", keywords=["files", "explorer"]),
        app("KWrite", generic_name="Text Editor", exec="kwrite %U"),
        app("Konsole", generic_name="Terminal"),
        app("System Monitor", exec="plasma-systemmonitor"),
        app("Kate", comment="Advanced text editor"),
    ]
    names = lambda q: [a.name for a in search(apps, q)]  # noqa: E731
    assert names("k") == ["Kate", "Konsole", "KWrite", "Okular"]  # prefixes before substrings
    assert names("mon") == ["System Monitor"]  # word prefix
    assert names("edit") == ["KWrite", "Kate"]  # generic name before comment
    assert names("file") == ["Dolphin"]
    assert names("plasma") == ["System Monitor"]  # executable
    assert names("   ") == []
    assert len(search(apps, "o", limit=2)) == 2


def test_recent_documents(tmp_path):
    older = tmp_path / "older.txt"
    newer = tmp_path / "new file.odt"
    older.write_text("x")
    newer.write_text("x")
    xbel = tmp_path / "recently-used.xbel"
    xbel.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<xbel version="1.0">
  <bookmark href="file://{older}" added="2026-01-01T00:00:00Z" modified="2026-01-01T00:00:00Z" visited="2026-01-02T00:00:00Z"/>
  <bookmark href="file://{str(newer).replace(" ", "%20")}" modified="2026-03-01T00:00:00Z" visited="2026-03-01T00:00:00Z"/>
  <bookmark href="file://{tmp_path}/deleted.txt" modified="2026-04-01T00:00:00Z"/>
  <bookmark href="https://example.com/" modified="2026-05-01T00:00:00Z"/>
</xbel>
"""
    )
    docs = recent_documents(xbel=xbel)
    assert [d.name for d in docs] == ["new file.odt", "older.txt"]
    assert recent_documents(limit=1, xbel=xbel)[0].path == newer
    assert recent_documents(xbel=tmp_path / "absent.xbel") == []


def test_exec_argv_field_codes(tmp_path):
    path = write_entry(tmp_path, "viewer.desktop", Name="Viewer", Icon="viewer", Exec="viewer --new %U %i %c 100%%")
    assert desktop_entries.exec_argv(path) == ["viewer", "--new", "--icon", "viewer", "Viewer", "100%"]


def test_xkb_us_layout():
    xkb = keyboard.Xkb()
    if not xkb.set_layout("us"):
        pytest.skip("libxkbcommon or the xkeyboard-config data is unavailable")
    try:
        a = xkb.translate(30)
        assert (a.sym, a.text, a.printable) == (ord("a"), "a", True)
        xkb.press(42, True)  # Left Shift
        shifted = xkb.translate(30)
        assert shifted.text == "A" and shifted.modifiers & keyboard.MOD_SHIFT
        xkb.press(42, False)
        assert xkb.translate(28).sym == keyboard.KEY_RETURN
        assert xkb.translate(103).sym == keyboard.KEY_UP
        xkb.press(29, True)  # Left Ctrl
        assert not xkb.translate(30).printable
    finally:
        xkb.close()


def load_theme(name: str) -> None:
    theme = Theme()
    theme.load(name, "English")
    elements.set_theme(theme)


def test_menu_view_navigation():
    load_theme("Windows 95-98")
    view = MenuView([MenuItem("_One"), MenuItem.sep(), MenuItem("_Two", enabled=False), MenuItem("T_hree"), MenuItem("Hidden", visible=False)])
    view.measure()
    assert len(view.rows) == 4
    assert view.step(1, -1) == 0
    assert view.step(1, 0) == 3  # skips the separator and the disabled row
    assert view.step(1, 3) == 0  # wraps
    assert view.step(-1, 0) == 3
    assert view.rows[3].item.access_key == "h"


def test_menu_view_wraps_into_columns():
    load_theme("Windows 95-98")
    items = [MenuItem(f"Item {i}") for i in range(40)]
    one_width, one_height = MenuView(items).measure()
    width, height = MenuView(items, max_height=200).measure()
    assert height <= 200 < one_height
    assert width >= 2 * (one_width - 2 * MenuView.PAD)


NOOP = StartActions(*([lambda *a: None] * 8))


class FakeCatalog:
    def __init__(self, apps: list[Application]) -> None:
        self.apps = apps

    def folders(self):
        return applications.folders(self.apps)

    def search(self, query, limit=15):
        return search(self.apps, query, limit)


def test_start_items(monkeypatch):
    load_theme("Windows 95-98")
    monkeypatch.setenv("USER", "tux_user")
    items = start_items(FakeCatalog([]), NOOP)
    labels = [i.label for i in items if not i.separator]
    assert labels[:4] == ["_Programs", "_Documents", "_Settings", "_Run..."]
    assert labels[-2:] == ["_Log Off tux__user...", "Sh_ut Down..."]
    assert [i.label for i in program_items(FakeCatalog([]), NOOP)] == ["(Empty)"]
    games = Application("chess.desktop", Path("chess"), "Chess_Pro", categories=["Game"])
    folder = program_items(FakeCatalog([games]), NOOP)[0]
    assert folder.label == folder_label("Games") == "Games"
    assert [i.label for i in folder.submenu()] == ["Chess__Pro"]


def test_banner_text(tmp_path):
    release = tmp_path / "os-release"
    release.write_text('NAME="Fedora Linux"\nVERSION_ID=41\n')
    assert banner_text(release) == ("Fedora", "41")
    assert banner_text(tmp_path / "absent")[0]


@pytest.mark.parametrize("theme_name", ["Windows 95-98", "Windows XP Blue", "Windows Me", "Watercolor"])
def test_start_menu_renders(theme_name):
    load_theme(theme_name)
    view = StartMenuView(start_items(FakeCatalog([]), NOOP), aliased=True, banner=("Debian", "13"), max_height=600)
    width, height = view.measure()
    assert width >= 190 and height > 6 * 32
    first = next(r for r in view.rows if not r.item.separator)
    assert first.rect.top() >= view.search_row().bottom()
    assert first.rect.left() >= StartMenuView.BANNER
    assert view.row_at(view.search_row().center().x(), view.search_row().center().y()) == -1
    image = QImage(int(width), int(height), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("magenta"))
    p = QPainter(image)
    view.query = "kon"
    view.paint(p, QRectF(0, 0, width, height))
    p.end()
    banner_bottom = QColor(image.pixel(StartMenuView.PAD + 2, int(height) - 10))
    banner_top = QColor(image.pixel(StartMenuView.PAD + 2, StartMenuView.PAD + 2))
    assert banner_bottom != QColor("magenta") and banner_top != banner_bottom  # gradient
    field = view.search_row().adjusted(8, 8, -8, -8)
    assert QColor(image.pixel(int(field.right()) - 2, int(field.center().y()))) != QColor("magenta")


@pytest.mark.skipif(not os.path.isdir("/usr/share/applications"), reason="no system applications")
def test_scan_real_system_is_sane():
    apps = scan()
    assert len({a.desktop_id for a in apps}) == len(apps)
