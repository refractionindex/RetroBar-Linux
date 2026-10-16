"""Classic popup menus and tooltips drawn on xdg_popup surfaces."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import QDate, QLocale, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPainter, QPainterPath, QPen

from ..wayland.client import FLIP_X, FLIP_Y, SLIDE_X, SLIDE_Y, LayerSurface, PopupSurface, WaylandClient
from ..wayland.keyboard import (
    KEY_DOWN,
    KEY_END,
    KEY_ESCAPE,
    KEY_HOME,
    KEY_ISO_LEFT_TAB,
    KEY_KP_ENTER,
    KEY_LEFT,
    KEY_RETURN,
    KEY_RIGHT,
    KEY_SPACE,
    KEY_TAB,
    KEY_UP,
    KeyEvent,
)
from ..xaml.markup import StaticMember
from . import elements

MARLETT = {"0": "\u2013", "1": "\u25a1", "2": "\u2750", "r": "\u2715"}


@dataclass
class MenuItem:
    label: str = ""
    action: Callable[[], None] | None = None
    enabled: bool = True
    checked: bool | None = None
    bold: bool = False
    separator: bool = False
    glyph: str = ""
    icon: QIcon | None = None
    submenu: list[MenuItem] | Callable[[], list[MenuItem]] | None = None
    visible: bool = True
    radio: bool = False

    @staticmethod
    def sep() -> MenuItem:
        return MenuItem(separator=True)

    @property
    def access_key(self) -> str:
        """The character after a single '_' (WPF access key), lower-cased."""
        label = self.label.replace("__", "")
        index = label.find("_")
        return label[index + 1].casefold() if 0 <= index < len(label) - 1 else ""


@dataclass
class _Row:
    item: MenuItem
    rect: QRectF = field(default_factory=QRectF)


def _color(name: str) -> QColor:
    return elements.theme().static_member(StaticMember("SystemColors", name + "Color"))


def _menu_font(bold: bool = False, aliased: bool = False) -> QFont:
    theme = elements.theme()
    family = theme.resource("GlobalFontFamily")
    size = theme.number("GlobalFontSize", 12.0)
    family = family.text if hasattr(family, "text") else family
    return theme.font(str(family) if family else None, size, 700 if bold else 400, False, aliased)


def _mnemonic(text: str) -> str:
    """WPF access keys use '_'; Qt's mnemonic marker is '&'."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "&":
            out.append("&&")
        elif ch == "_":
            if i + 1 < len(text) and text[i + 1] == "_":
                out.append("_")
                i += 1
            else:
                out.append("&")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def bevel(p: QPainter, rect: QRectF, top_left: QColor, bottom_right: QColor) -> None:
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    p.fillRect(QRectF(x, y, w - 1, 1), top_left)
    p.fillRect(QRectF(x, y, 1, h - 1), top_left)
    p.fillRect(QRectF(x, y + h - 1, w, 1), bottom_right)
    p.fillRect(QRectF(x + w - 1, y, 1, h), bottom_right)


class MenuView:
    """Layout, painting and hit testing of a classic menu, independent of any surface."""

    PAD = 3
    ARROW = 17
    SEPARATOR = 9.0

    def __init__(self, items: list[MenuItem], aliased: bool = False, icon_size: int = 16, max_height: float = 0, min_width: int = 100) -> None:
        self.items = [i for i in items if i.visible]
        self.aliased = aliased
        self.icon_size = icon_size
        self.max_height = max_height
        self.min_width = min_width
        self.left = 0.0  # reserved at the left edge (Start menu banner)
        self.top = 0.0  # reserved at the top edge (Start menu search box)
        self.rows: list[_Row] = []
        self.hover = -1
        self.size = (0, 0)

    @property
    def gutter(self) -> int:
        return 20 if self.icon_size <= 16 else self.icon_size + 12

    def measure(self) -> tuple[int, int]:
        normal = QFontMetricsF(_menu_font(False, self.aliased))
        bold = QFontMetricsF(_menu_font(True, self.aliased))
        row_h = max(18.0, math.ceil(normal.height()) + 6)
        if self.icon_size > 16:
            row_h = max(row_h, self.icon_size + 6.0)
        text_w = 0.0
        for item in self.items:
            if not item.separator:
                fm = bold if item.bold else normal
                text_w = max(text_w, fm.horizontalAdvance(item.label.replace("_", "")))
        col_w = math.ceil(text_w) + self.gutter + self.ARROW + 8
        x0, y0 = self.PAD + self.left, self.PAD + self.top
        limit = self.max_height - self.PAD if self.max_height > 0 else math.inf
        self.rows = []
        x, y, bottom = x0, y0, y0
        for item in self.items:
            h = self.SEPARATOR if item.separator else row_h
            if y + h > limit and y > y0:
                # Too tall for the screen: continue in another column, as classic menus do.
                x, y = x + col_w, y0
                if item.separator:
                    continue
            self.rows.append(_Row(item, QRectF(x, y, col_w, h)))
            y += h
            bottom = max(bottom, y)
        columns = 1 + (round((x - x0) / col_w) if col_w else 0)
        width = max(self.min_width, int(self.left + 2 * self.PAD + columns * col_w))
        if columns == 1:
            for row in self.rows:
                row.rect.setWidth(width - self.left - 2 * self.PAD)
        self.size = (width, int(math.ceil(bottom + self.PAD)))
        return self.size

    # --- painting -------------------------------------------------------------------------------------
    def paint(self, p: QPainter, rect: QRectF) -> None:
        face, light, lighter = _color("Menu"), _color("ControlLight"), _color("ControlLightLight")
        dark, darker = _color("ControlDark"), _color("ControlDarkDark")
        p.fillRect(rect, face)
        bevel(p, rect, light, darker)
        bevel(p, rect.adjusted(1, 1, -1, -1), lighter, dark)
        for index, row in enumerate(self.rows):
            self._paint_row(p, index, row)

    def _paint_row(self, p: QPainter, index: int, row: _Row) -> None:
        item, r = row.item, row.rect
        lighter, dark = _color("ControlLightLight"), _color("ControlDark")
        if item.separator:
            y = r.y() + r.height() / 2 - 1
            p.fillRect(QRectF(r.x() + 1, y, r.width() - 2, 1), dark)
            p.fillRect(QRectF(r.x() + 1, y + 1, r.width() - 2, 1), lighter)
            return
        highlighted = index == self.hover and item.enabled
        if highlighted:
            p.fillRect(r, _color("Highlight"))
        fg = _color("HighlightText") if highlighted else _color("MenuText")
        font = _menu_font(item.bold, self.aliased)
        gutter_w = self.gutter
        text_rect = QRectF(r.x() + gutter_w, r.y(), r.width() - gutter_w - self.ARROW, r.height())
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextShowMnemonic)
        p.setFont(font)
        if not item.enabled:
            p.setPen(lighter)
            p.drawText(text_rect.translated(1, 1), flags, _mnemonic(item.label))
            p.setPen(_color("GrayText") if highlighted else dark)
        else:
            p.setPen(fg)
        p.drawText(text_rect, flags, _mnemonic(item.label))
        gutter = QRectF(r.x(), r.y(), gutter_w, r.height())
        pen_color = p.pen().color()
        if item.checked:
            self._check(p, gutter, pen_color, item.radio)
        elif item.glyph:
            glyph_font = QFont(font)
            glyph_font.setPixelSize(max(8, font.pixelSize()))
            p.setFont(glyph_font)
            p.drawText(gutter, int(Qt.AlignmentFlag.AlignCenter), MARLETT.get(item.glyph, item.glyph))
        elif item.icon is not None and not item.icon.isNull():
            mode = QIcon.Mode.Normal if item.enabled else QIcon.Mode.Disabled
            s = self.icon_size
            item.icon.paint(p, QRectF(gutter.center().x() - s / 2, gutter.center().y() - s / 2, s, s).toRect(), Qt.AlignmentFlag.AlignCenter, mode)
        if item.submenu is not None:
            cx, cy = r.right() - self.ARROW / 2, r.center().y()
            arrow = QPainterPath()
            arrow.moveTo(cx - 2, cy - 4)
            arrow.lineTo(cx + 2, cy)
            arrow.lineTo(cx - 2, cy + 4)
            arrow.closeSubpath()
            p.fillPath(arrow, pen_color)

    @staticmethod
    def _check(p: QPainter, gutter: QRectF, color: QColor, radio: bool) -> None:
        c = gutter.center()
        p.save()
        if radio:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(c, 3, 3)
        else:
            p.setPen(QPen(color, 1))
            for dx in range(3):
                x0 = c.x() - 3 + dx
                p.drawLine(QPointF(x0, c.y() - 1 + dx), QPointF(x0, c.y() + 1 + dx))
            for dx in range(4):
                x0 = c.x() + dx
                p.drawLine(QPointF(x0, c.y() + 1 - dx), QPointF(x0, c.y() + 3 - dx))
        p.restore()

    # --- hit testing & navigation ---------------------------------------------------------------------
    def row_at(self, x: float, y: float) -> int:
        for index, row in enumerate(self.rows):
            if row.rect.contains(QPointF(x, y)) and not row.item.separator:
                return index
        return -1

    def selectable(self, index: int) -> bool:
        return 0 <= index < len(self.rows) and not self.rows[index].item.separator and self.rows[index].item.enabled

    def step(self, delta: int, start: int | None = None) -> int:
        """The next selectable row from start (default: hover) in direction delta, wrapping; -1 if none."""
        count = len(self.rows)
        if count == 0:
            return -1
        index = self.hover if start is None else start
        if index < 0:
            index = -1 if delta > 0 else count
        for _ in range(count):
            index = (index + delta) % count
            if self.selectable(index):
                return index
        return -1


class MenuPopup(PopupSurface):
    """A menu (or submenu) on its own xdg_popup; submenus cascade as child popups."""

    SUBMENU_DELAY_MS = 350

    def __init__(
        self,
        client: WaylandClient,
        parent: LayerSurface | PopupSurface,
        items: list[MenuItem],
        anchor_rect: QRectF,
        anchor: str,
        gravity: str,
        aliased: bool = False,
        root: MenuPopup | None = None,
        *,
        view: MenuView | None = None,
        max_height: float = 0,
    ) -> None:
        self.view = view or MenuView(items, aliased, max_height=max_height)
        self.aliased = aliased
        self.root = root or self
        self.child: MenuPopup | None = None
        self.on_closed: Callable[[], None] = lambda: None
        self._submenu_timer = QTimer()
        self._submenu_timer.setSingleShot(True)
        self._submenu_timer.setInterval(self.SUBMENU_DELAY_MS)
        self._submenu_timer.timeout.connect(self._open_hover_submenu)
        self.from_row = -1
        width, height = self.view.measure()
        super().__init__(client, parent, anchor_rect, width, height, anchor, gravity, SLIDE_X | SLIDE_Y | FLIP_X | FLIP_Y, grab=True)
        self.on_done = self._closed_by_compositor

    @property
    def items(self) -> list[MenuItem]:
        return self.view.items

    @property
    def rows(self) -> list[_Row]:
        return self.view.rows

    @property
    def hover(self) -> int:
        return self.view.hover

    @hover.setter
    def hover(self, index: int) -> None:
        if index != self.view.hover:
            self.view.hover = index
            self.request_redraw()

    def set_items(self, items: list[MenuItem], anchor_rect: QRectF) -> bool:
        """Replace the contents in place; False if the popup cannot be resized and must be recreated."""
        view = MenuView(items, self.aliased, self.view.icon_size, self.view.max_height, self.view.min_width)
        width, height = view.measure()
        if (width, height) != (self.width, self.height) and not self.reposition(anchor_rect, width, height):
            return False
        if self.child is not None:
            self.child.close_tree()
            self.child = None
        self.view = view
        self.request_redraw()
        return True

    def paint(self, p: QPainter, rect: QRectF) -> None:
        self.view.paint(p, rect)

    # --- pointer ----------------------------------------------------------------------------------------
    def pointer_motion(self, x: float, y: float) -> None:
        index = self.view.row_at(x, y)
        if index != self.hover:
            self.hover = index
            if index >= 0 and self.rows[index].item.submenu is not None and self.rows[index].item.enabled:
                self._submenu_timer.start()
            elif self.child is not None:
                self._submenu_timer.start()

    def pointer_enter(self, x: float, y: float) -> None:
        self.pointer_motion(x, y)

    def pointer_leave(self) -> None:
        if self.child is None and self.hover != -1:
            self.hover = -1

    def pointer_button(self, button: int, pressed: bool, x: float, y: float) -> None:
        if pressed:
            return
        index = self.view.row_at(x, y)
        if index >= 0:
            self._activate(index, keyboard=False)

    # --- submenus & activation --------------------------------------------------------------------------
    def _open_hover_submenu(self) -> None:
        self._open_submenu(self.hover, keyboard=False)

    def _open_submenu(self, index: int, keyboard: bool) -> None:
        if self.child is not None:
            if index >= 0 and self.child.from_row == index and not self.child._destroyed:
                if keyboard:
                    self.child.hover = self.child.view.step(1, -1)
                return
            self.child.close_tree()
            self.child = None
        if not self.view.selectable(index):
            return
        item = self.rows[index].item
        if item.submenu is None:
            return
        sub = item.submenu() if callable(item.submenu) else item.submenu
        if not sub:
            return
        anchor = self.rows[index].rect.adjusted(0, -MenuView.PAD, 0, 0)
        child = MenuPopup(self.client, self, sub, anchor, "top_right", "bottom_right", self.aliased, self.root, max_height=self.view.max_height)
        child.from_row = index
        if keyboard:
            child.hover = child.view.step(1, -1)
        self.child = child

    def _activate(self, index: int, keyboard: bool) -> None:
        if not self.view.selectable(index):
            return
        item = self.rows[index].item
        if item.submenu is not None:
            self._submenu_timer.stop()
            self._open_submenu(index, keyboard)
            return
        self.root.close_tree()
        if item.action is not None:
            QTimer.singleShot(0, item.action)

    # --- keyboard ---------------------------------------------------------------------------------------
    def key_press(self, event: KeyEvent) -> None:
        self.root.handle_key(event)

    def active_menu(self) -> MenuPopup:
        """The deepest open menu the user has moved into (a hovered row), where arrow keys apply."""
        menu = self
        while menu.child is not None and not menu.child._destroyed and menu.child.hover >= 0:
            menu = menu.child
        return menu

    def handle_key(self, event: KeyEvent) -> bool:
        return self.active_menu().navigate(event)

    def navigate(self, event: KeyEvent) -> bool:
        sym = event.sym
        if sym in (KEY_DOWN, KEY_TAB):
            self._select(self.view.step(1))
        elif sym in (KEY_UP, KEY_ISO_LEFT_TAB):
            self._select(self.view.step(-1))
        elif sym == KEY_HOME:
            self._select(self.view.step(1, -1))
        elif sym == KEY_END:
            self._select(self.view.step(-1, len(self.rows)))
        elif sym == KEY_RIGHT:
            if self.view.selectable(self.hover) and self.rows[self.hover].item.submenu is not None:
                self._open_submenu(self.hover, keyboard=True)
        elif sym in (KEY_RETURN, KEY_KP_ENTER, KEY_SPACE):
            self._activate(self.hover, keyboard=True)
        elif sym in (KEY_LEFT, KEY_ESCAPE):
            if self is not self.root:
                parent = self.parent
                self.close_tree()
                if isinstance(parent, MenuPopup):
                    parent.child = None
            elif sym == KEY_ESCAPE:
                self.close_tree()
        elif event.printable:
            self._access(event.text.casefold())
        else:
            return False
        return True

    def _select(self, index: int) -> None:
        if index < 0:
            return
        self._submenu_timer.stop()
        if self.child is not None:
            self.child.close_tree()
            self.child = None
        self.hover = index

    def _access(self, key: str) -> None:
        """Access keys: activate the only match, or cycle through several."""
        matches = [i for i, row in enumerate(self.rows) if self.view.selectable(i) and row.item.access_key == key]
        if not matches:
            matches = [i for i, row in enumerate(self.rows) if self.view.selectable(i) and row.item.label.replace("_", "")[:1].casefold() == key]
        if len(matches) == 1:
            self._select(matches[0])
            self._activate(matches[0], keyboard=True)
        elif matches:
            later = [i for i in matches if i > self.hover]
            self._select(later[0] if later else matches[0])

    # --- closing ----------------------------------------------------------------------------------------
    def close_tree(self) -> None:
        if self.child is not None:
            self.child.close_tree()
            self.child = None
        self._submenu_timer.stop()
        if not self._destroyed:
            self.destroy()
            if self is self.root:
                self.on_closed()

    def _closed_by_compositor(self) -> None:
        if self.child is not None:
            self.child.close_tree()
            self.child = None
        self._submenu_timer.stop()
        if self is self.root:
            self.on_closed()
        elif isinstance(self.parent, MenuPopup) and self.parent.child is self:
            self.parent.child = None


class CalendarPopup(PopupSurface):
    """A small month calendar opened from the clock."""

    CELL_W, CELL_H, HEADER = 26, 18, 24

    def __init__(self, client: WaylandClient, parent: LayerSurface, anchor_rect: QRectF, anchor: str, aliased: bool) -> None:
        today = QDate.currentDate()
        self.today = today
        self.month = QDate(today.year(), today.month(), 1)
        self.aliased = aliased
        width = 7 * self.CELL_W + 12
        height = self.HEADER + 7 * self.CELL_H + 12
        super().__init__(client, parent, anchor_rect, width, height, anchor, anchor, SLIDE_X | SLIDE_Y | FLIP_X | FLIP_Y, grab=True)

    def _arrows(self, rect: QRectF) -> tuple[QRectF, QRectF]:
        return QRectF(6, 4, 20, self.HEADER - 6), QRectF(rect.width() - 26, 4, 20, self.HEADER - 6)

    def paint(self, p: QPainter, rect: QRectF) -> None:
        face, light, lighter = _color("Control"), _color("ControlLight"), _color("ControlLightLight")
        dark, darker, text = _color("ControlDark"), _color("ControlDarkDark"), _color("ControlText")
        p.fillRect(rect, face)
        bevel(p, rect, light, darker)
        bevel(p, rect.adjusted(1, 1, -1, -1), lighter, dark)
        locale = QLocale()
        font = _menu_font(False, self.aliased)
        bold = _menu_font(True, self.aliased)
        w = rect.width()
        p.setPen(text)
        p.setFont(bold)
        title = f"{locale.standaloneMonthName(self.month.month())} {self.month.year()}"
        p.drawText(QRectF(0, 4, w, self.HEADER - 6), int(Qt.AlignmentFlag.AlignCenter), title)
        left, right = self._arrows(rect)
        for r, glyph in ((left, "\u25c0"), (right, "\u25b6")):
            p.setFont(font)
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), glyph)
        first_dow = locale.firstDayOfWeek().value
        y0 = self.HEADER + 4
        for col in range(7):
            day = (first_dow - 1 + col) % 7 + 1
            cell = QRectF(6 + col * self.CELL_W, y0, self.CELL_W, self.CELL_H)
            p.drawText(cell, int(Qt.AlignmentFlag.AlignCenter), locale.dayName(day, QLocale.FormatType.NarrowFormat))
        p.fillRect(QRectF(6, y0 + self.CELL_H - 1, 7 * self.CELL_W, 1), dark)
        offset = (self.month.dayOfWeek() - first_dow) % 7
        for day in range(1, self.month.daysInMonth() + 1):
            index = offset + day - 1
            cell = QRectF(6 + (index % 7) * self.CELL_W, y0 + (1 + index // 7) * self.CELL_H, self.CELL_W, self.CELL_H)
            date = QDate(self.month.year(), self.month.month(), day)
            if date == self.today:
                p.fillRect(cell.adjusted(2, 1, -2, -1), _color("Highlight"))
                p.setPen(_color("HighlightText"))
            else:
                p.setPen(text)
            p.drawText(cell, int(Qt.AlignmentFlag.AlignCenter), str(day))

    def pointer_button(self, button: int, pressed: bool, x: float, y: float) -> None:
        if pressed or button != 1:
            return
        left, right = self._arrows(QRectF(0, 0, self.width, self.height))
        if left.contains(QPointF(x, y)):
            self.month = self.month.addMonths(-1)
            self.request_redraw()
        elif right.contains(QPointF(x, y)):
            self.month = self.month.addMonths(1)
            self.request_redraw()

    def key_press(self, event: KeyEvent) -> None:
        if event.sym == KEY_ESCAPE:
            self.dismissed()
        elif event.sym == KEY_LEFT:
            self.month = self.month.addMonths(-1)
            self.request_redraw()
        elif event.sym == KEY_RIGHT:
            self.month = self.month.addMonths(1)
            self.request_redraw()


class TooltipPopup(PopupSurface):
    """A theme-styled ToolTip (Style {x:Type ToolTip}: Background, Foreground, BorderBrush, Padding, fonts)."""

    def __init__(self, client: WaylandClient, parent: LayerSurface, text: str, anchor_rect: QRectF, anchor: str, aliased: bool) -> None:
        self.text = text
        self.tip = elements.Control("ToolTip")
        self.aliased = aliased
        font = self._font()
        fm = QFontMetricsF(font)
        pad = self.tip.get("Padding")
        lines = text.splitlines() or [""]
        width = math.ceil(max(fm.horizontalAdvance(line) for line in lines) + pad.horizontal + 2)
        height = math.ceil(fm.height() * len(lines) + pad.vertical + 2)
        super().__init__(client, parent, QRectF(anchor_rect), width, height, anchor, anchor, SLIDE_X | SLIDE_Y | FLIP_X | FLIP_Y, grab=False)

    def _font(self) -> QFont:
        family = self.tip.get("FontFamily")
        size = self.tip.get("FontSize") or 12.0
        return elements.theme().font(family, size, self.tip.get("FontWeight") or 400, False, self.aliased)

    def paint(self, p: QPainter, rect: QRectF) -> None:
        bg = self.tip.brush("Background", rect)
        p.fillRect(rect, bg if bg is not None else _color("Info"))
        border = self.tip.brush("BorderBrush", rect)
        p.setPen(QPen(border if border is not None else _color("InfoText"), 1))
        p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        fg = self.tip.brush("Foreground", rect)
        p.setPen(QPen(fg if fg is not None else _color("InfoText"), 1))
        p.setFont(self._font())
        pad = self.tip.get("Padding")
        p.drawText(rect.adjusted(1 + pad.left, 1 + pad.top, -1 - pad.right, -1 - pad.bottom), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), self.text)

    def pointer_enter(self, x: float, y: float) -> None:
        self.destroy()
