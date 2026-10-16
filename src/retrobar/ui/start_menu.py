"""The classic Start menu: banner, type-to-search box, Programs folders and system commands."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QMimeDatabase, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QLinearGradient, QPainter

from ..applications import Application, Catalog, Document, recent_documents
from ..wayland.client import LayerSurface, WaylandClient
from ..wayland.keyboard import KEY_BACKSPACE, KEY_ESCAPE, MOD_CTRL, KeyEvent
from . import elements
from .menu import MenuItem, MenuPopup, MenuView, _color, _menu_font, bevel

# Kickoff's category icons, by catalog folder.
FOLDER_ICONS = {
    "Development": "applications-development",
    "Education": "applications-education",
    "Games": "applications-games",
    "Graphics": "applications-graphics",
    "Internet": "applications-internet",
    "Multimedia": "applications-multimedia",
    "Office": "applications-office",
    "Science & Math": "applications-science",
    "Settings": "preferences-system",
    "System": "applications-system",
    "Utilities": "applications-utilities",
}

# Defaults for strings a language file may override.
STRINGS = {
    "start_menu_programs": "_Programs",
    "start_menu_documents": "_Documents",
    "start_menu_settings": "_Settings",
    "start_menu_run": "_Run...",
    "start_menu_log_off": "_Log Off {0}...",
    "start_menu_shut_down": "Sh_ut Down...",
    "start_menu_control_panel": "_Control Panel",
    "start_menu_printers": "_Printers",
    "start_menu_taskbar": "_Taskbar & Start Menu...",
    "start_menu_search": "Search programs",
    "start_menu_no_matches": "(No matches)",
    "start_menu_empty": "(Empty)",
}


def text(key: str) -> str:
    return elements.theme().string(key, STRINGS.get(key, key))


def folder_label(folder: str) -> str:
    key = "start_menu_folder_" + "".join(ch if ch.isalnum() else "_" for ch in folder.casefold()).strip("_")
    return elements.theme().string(key, folder)


def banner_text(os_release: Path = Path("/etc/os-release")) -> tuple[str, str]:
    """('Debian', '13') from os-release, styled like 'Windows98' on the classic banner."""
    values: dict[str, str] = {}
    for candidate in (os_release, Path("/usr/lib/os-release")):
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip("\"'")
            break
        except OSError:
            continue
    name = (values.get("NAME") or "Linux").split()[0]
    return name, values.get("VERSION_ID", "")


class StartMenuView(MenuView):
    """Root Start menu layout: a vertical banner on the left and a search field above 32 px rows."""

    BANNER = 22
    SEARCH_H = 30

    def __init__(self, items: list[MenuItem], aliased: bool, banner: tuple[str, str], max_height: float = 0) -> None:
        super().__init__(items, aliased, icon_size=32, max_height=max_height, min_width=190)
        self.left = self.BANNER
        self.top = self.SEARCH_H
        self.banner = banner
        self.query = ""
        self.placeholder = text("start_menu_search")
        self.caret_visible = True

    def search_row(self) -> QRectF:
        """The search box row, also the anchor the results menu cascades from."""
        width, _ = self.size
        return QRectF(self.PAD + self.left, self.PAD, width - self.left - 2 * self.PAD, self.SEARCH_H)

    def paint(self, p: QPainter, rect: QRectF) -> None:
        super().paint(p, rect)
        self._paint_banner(p, QRectF(self.PAD, self.PAD, self.BANNER, rect.height() - 2 * self.PAD))
        self._paint_search(p, self.search_row())

    def _paint_banner(self, p: QPainter, r: QRectF) -> None:
        caption = _color("ActiveCaption")
        top = QColor(caption.red() // 5, caption.green() // 5, caption.blue() // 5)
        gradient = QLinearGradient(r.topLeft(), r.bottomLeft())
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, caption)
        p.fillRect(r, gradient)
        name, version = self.banner
        theme = elements.theme()
        family = theme.resource("GlobalFontFamily")
        family = str(family.text if hasattr(family, "text") else family) if family else None
        size = r.width() - 4
        bold = theme.font(family, size, 900, False, self.aliased)
        light = theme.font(family, size, 400, False, self.aliased)
        p.save()
        p.setClipRect(r)
        p.translate(r.left(), r.bottom() - 6)
        p.rotate(-90)
        x = 0.0
        p.setFont(bold)
        p.setPen(QColor("white"))
        line = QRectF(0, 0, r.height(), r.width())
        p.drawText(line, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
        x += QFontMetricsF(bold).horizontalAdvance(name)
        if version:
            p.setFont(light)
            p.setPen(_color("GradientActiveCaption").lighter(170))
            p.drawText(line.adjusted(x, 0, 0, 0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), version)
        p.restore()

    def _paint_search(self, p: QPainter, row: QRectF) -> None:
        field = row.adjusted(4, 4, -4, -4)
        bevel(p, field, _color("ControlDark"), _color("ControlLightLight"))
        inner = field.adjusted(1, 1, -1, -1)
        bevel(p, inner, _color("ControlDarkDark"), _color("ControlLight"))
        box = inner.adjusted(1, 1, -1, -1)
        p.fillRect(box, _color("Window"))
        icon = QIcon.fromTheme("system-search")
        icon_rect = QRectF(box.left() + 3, box.center().y() - 8, 16, 16)
        if not icon.isNull():
            icon.paint(p, icon_rect.toRect())
        text_box = box.adjusted(icon_rect.width() + 7, 0, -3, 0)
        font = _menu_font(False, self.aliased)
        fm = QFontMetricsF(font)
        p.save()
        p.setClipRect(text_box)
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if self.query:
            shown = fm.elidedText(self.query, Qt.TextElideMode.ElideLeft, text_box.width() - 2)
            p.setFont(font)
            p.setPen(_color("WindowText"))
            p.drawText(text_box, flags, shown)
            caret_x = text_box.left() + fm.horizontalAdvance(shown)
        else:
            italic = QFont(font)
            italic.setItalic(True)
            p.setFont(italic)
            p.setPen(_color("GrayText"))
            p.drawText(text_box, flags, self.placeholder)
            caret_x = text_box.left()
        if self.caret_visible:
            height = math.ceil(fm.height())
            top = text_box.center().y() - height / 2
            p.fillRect(QRectF(math.floor(caret_x), top, 1, height), _color("WindowText"))
        p.restore()

    def row_at(self, x: float, y: float) -> int:
        if self.search_row().contains(QPointF(x, y)):
            return -1
        return super().row_at(x, y)


class StartMenuPopup(MenuPopup):
    """Root Start menu. Typing always edits the search box; matches cascade beside it."""

    CARET_MS = 530

    def __init__(
        self,
        client: WaylandClient,
        parent: LayerSurface,
        items: list[MenuItem],
        anchor_rect: QRectF,
        anchor: str,
        gravity: str,
        aliased: bool,
        search: Callable[[str], list[MenuItem]],
        banner: tuple[str, str],
        max_height: float = 0,
    ) -> None:
        self.start_view = StartMenuView(items, aliased, banner, max_height)
        self.search = search
        self.results: MenuPopup | None = None
        super().__init__(client, parent, items, anchor_rect, anchor, gravity, aliased, view=self.start_view, max_height=max_height)
        self._caret = QTimer()
        self._caret.setInterval(self.CARET_MS)
        self._caret.timeout.connect(self._blink)
        self._caret.start()

    @property
    def query(self) -> str:
        return self.start_view.query

    def _blink(self) -> None:
        self.start_view.caret_visible = not self.start_view.caret_visible
        self.request_redraw()

    def set_query(self, query: str) -> None:
        self.start_view.query = query
        self.start_view.caret_visible = True
        self._caret.start()
        self.request_redraw()
        self._update_results()

    def _update_results(self) -> None:
        query = self.query.strip()
        if self.results is not None and (self.results._destroyed or self.child is not self.results):
            self.results = None
        if not query:
            if self.results is not None:
                self.results.close_tree()
                self.child = self.results = None
            return
        items = self.search(query) or [MenuItem(text("start_menu_no_matches"), enabled=False)]
        anchor = self.start_view.search_row()
        if self.results is not None and self.results.set_items(items, anchor):
            self.results.hover = self.results.view.step(1, -1)
            return
        if self.child is not None:
            self.child.close_tree()
            self.child = None
        self._submenu_timer.stop()
        results = MenuPopup(self.client, self, items, anchor, "top_right", "bottom_right", self.aliased, self, max_height=self.view.max_height)
        results.hover = results.view.step(1, -1)
        self.child = self.results = results

    def _open_hover_submenu(self) -> None:
        # The pointer crossing the search box on its way to the results must not close them.
        if self.hover < 0 and self.results is not None and self.child is self.results:
            return
        super()._open_hover_submenu()

    def handle_key(self, event: KeyEvent) -> bool:
        if event.printable and event.text:
            self.set_query(self.query + event.text)
            return True
        if event.sym == KEY_BACKSPACE:
            if self.query:
                if event.modifiers & MOD_CTRL:
                    trimmed = self.query.rstrip()
                    cut = trimmed.rfind(" ")
                    self.set_query(trimmed[: cut + 1] if cut >= 0 else "")
                else:
                    self.set_query(self.query[:-1])
            return True
        if event.sym == KEY_ESCAPE and self.query:
            self.set_query("")
            return True
        return super().handle_key(event)

    def destroy(self) -> None:
        self._caret.stop()
        super().destroy()


@dataclass
class StartActions:
    """What the Start menu's commands do; supplied by the application."""

    launch: Callable[[Application], None]
    open_document: Callable[[Path], None]
    control_panel: Callable[[], None]
    printers: Callable[[], None]
    taskbar_properties: Callable[[], None]
    run: Callable[[], None]
    log_off: Callable[[], None]
    shut_down: Callable[[], None]


def _app_item(app: Application, actions: StartActions) -> MenuItem:
    return MenuItem(app.name.replace("_", "__"), lambda: actions.launch(app), icon=app.icon)


def search_items(catalog: Catalog, query: str, actions: StartActions, limit: int = 15) -> list[MenuItem]:
    return [_app_item(app, actions) for app in catalog.search(query, limit)]


def program_items(catalog: Catalog, actions: StartActions) -> list[MenuItem]:
    items = []
    for folder, apps in catalog.folders():
        icon = QIcon.fromTheme(FOLDER_ICONS.get(folder, "applications-other"))
        items.append(MenuItem(folder_label(folder), icon=icon, submenu=lambda apps=apps: [_app_item(a, actions) for a in apps]))
    return items or [MenuItem(text("start_menu_empty"), enabled=False)]


_mime = QMimeDatabase()


def document_items(actions: StartActions, docs: list[Document] | None = None) -> list[MenuItem]:
    docs = recent_documents() if docs is None else docs
    items = []
    for doc in docs:
        icon = QIcon.fromTheme(_mime.mimeTypeForFile(str(doc.path)).iconName(), QIcon.fromTheme("text-x-generic"))
        items.append(MenuItem(doc.name.replace("_", "__"), lambda path=doc.path: actions.open_document(path), icon=icon))
    return items or [MenuItem(text("start_menu_empty"), enabled=False)]


def start_items(catalog: Catalog, actions: StartActions) -> list[MenuItem]:
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    settings = [
        MenuItem(text("start_menu_control_panel"), actions.control_panel, icon=QIcon.fromTheme("preferences-system")),
        MenuItem(text("start_menu_printers"), actions.printers, icon=QIcon.fromTheme("printer")),
        MenuItem(text("start_menu_taskbar"), actions.taskbar_properties, icon=QIcon.fromTheme("configure")),
    ]
    return [
        MenuItem(text("start_menu_programs"), icon=QIcon.fromTheme("applications-all", QIcon.fromTheme("start-here-kde")), submenu=lambda: program_items(catalog, actions)),
        MenuItem(text("start_menu_documents"), icon=QIcon.fromTheme("folder-documents"), submenu=lambda: document_items(actions)),
        MenuItem(text("start_menu_settings"), icon=QIcon.fromTheme("preferences-system"), submenu=settings),
        MenuItem(text("start_menu_run"), actions.run, icon=QIcon.fromTheme("system-run")),
        MenuItem.sep(),
        MenuItem(text("start_menu_log_off").replace("{0}", user.replace("_", "__")), actions.log_off, icon=QIcon.fromTheme("system-log-out")),
        MenuItem(text("start_menu_shut_down"), actions.shut_down, icon=QIcon.fromTheme("system-shutdown")),
    ]
