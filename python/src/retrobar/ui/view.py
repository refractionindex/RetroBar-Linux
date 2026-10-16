"""The taskbar element tree (Taskbar.xaml and the controls it hosts) and its pointer interaction."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QIcon, QPainter

from ..settings import Edge, Settings
from ..xaml.markup import Binding, ResourceRef
from ..xaml.values import Thickness
from . import elements
from .elements import Element, ScrollViewer, make

LEFT_BUTTON, MIDDLE_BUTTON, RIGHT_BUTTON = 1, 2, 3
BUTTON_TYPES = {"Button", "ToggleButton", "RepeatButton"}


@dataclass(eq=False)
class TaskData:
    """DataContext of a TaskButton; property names match the WPF ApplicationWindow bindings."""

    key: Any
    Title: str = ""
    Icon: QIcon | None = None
    OverlayIcon: QIcon | None = None
    ProgressValue: int = 0
    ProgressState: int = 0
    State: str = "Normal"  # Normal | Active | Flashing
    window: Any = None


@dataclass(eq=False)
class TrayData:
    key: Any
    Title: str = ""
    Icon: QIcon | None = None
    item: Any = None


@dataclass
class ClockData:
    Now: datetime = field(default_factory=datetime.now)


@dataclass
class Hit:
    role: str
    element: Element
    data: Any = None


class TaskbarView:
    """Owns the element tree for one taskbar and translates pointer input into roles.

    Coordinates passed in and out are surface-logical pixels; the tree itself is laid out
    in unscaled units and drawn with TaskbarScale applied, like Taskbar.xaml's LayoutTransform.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.on_activate: Callable[[Hit, int, QRectF], None] = lambda hit, button, rect: None
        self.on_scroll: Callable[[Hit, int], None] = lambda hit, delta: None
        self.on_double_click: Callable[[Hit], None] = lambda hit: None
        self.tasks: list[TaskData] = []
        self.tray: list[TrayData] = []
        self.clock_data = ClockData()
        self.size = (0.0, 0.0)
        self.output_scale = 1
        self._task_elements: dict[Any, Element] = {}
        self._tray_elements: dict[Any, Element] = {}
        self._hovered: list[Element] = []
        self._pressed: Element | None = None
        self._pressed_hit: Hit | None = None
        self.build()

    # --- tree -----------------------------------------------------------------------------------
    @property
    def edge(self) -> Edge:
        return self.settings.edge

    @property
    def horizontal(self) -> bool:
        return self.edge in (Edge.TOP, Edge.BOTTOM)

    @property
    def scale(self) -> float:
        return max(0.5, float(self.settings.TaskbarScale or 1.0))

    def build(self) -> None:
        self._task_elements.clear()
        self._tray_elements.clear()
        self._hovered = []
        self._pressed = None

        self.window = make("Window", "TaskbarWindow")
        self.content = make("ContentControl", "Taskbar")
        self.window.add(self.content)
        dock = make("DockPanel")
        self.content.local["Content"] = dock

        self.start = make("UserControl")
        self.start_button = make("ToggleButton", "StartButton")
        self.start_button.tag = "start"
        start_grid = make("Grid")
        start_grid.col_defs = ["Auto", "*"]
        start_grid.add(make("Image", "StartIcon", Grid__Column=0))
        start_grid.add(make("TextBlock", "StartLabel", Grid__Column=1))
        self.start_button.local["Content"] = start_grid
        self.start.local["Content"] = self.start_button

        self.desktop = make("UserControl")
        self.desktop_button = make("ToggleButton", "ShowDesktopButton")
        self.desktop_button.tag = "desktop"
        self.desktop_button.local["Content"] = make("Image", "ShowDesktopIcon")
        self.desktop.local["Content"] = self.desktop_button

        self.tray_box = make("GroupBox", "Tray")
        self.tray_box.tag = "tray_area"
        self.tray_stack = make("StackPanel")
        self.tray_box.local["Content"] = self.tray_stack
        self.icon_list = make("UserControl")
        self.icon_list_stack = make("StackPanel")
        self.icon_list.local["Content"] = self.icon_list_stack
        self.tray_toggle = make("ToggleButton", "TrayToggleButton", Visibility="Collapsed")
        self.tray_toggle.tag = "tray_toggle"
        self.tray_items = make("ItemsControl", "NotifyIconItems")
        self.tray_panel = make("WrapPanel", Background="Transparent")
        self.tray_items.local["Content"] = self.tray_panel
        self.icon_list_stack.set_children([self.tray_toggle, self.tray_items])
        self.clock = make("UserControl", VerticalAlignment="Center", DataContext=self.clock_data)
        self.clock.local["Template"] = ResourceRef("ClockTemplateKey")
        self.clock.tag = "clock"
        self.tray_stack.set_children([self.icon_list, self.clock])

        self.task_list = make("UserControl", VerticalAlignment="Stretch")
        self.task_list.tag = "task_list"
        task_dock = make("DockPanel")
        self.task_list.local["Content"] = task_dock
        self.thumb = make("Thumb", "ToolbarThumb")
        self.thumb.tag = "thumb"
        self.scroller: ScrollViewer = make("ScrollViewer", "TaskListScrollViewer")  # type: ignore[assignment]
        self.task_items = make("ItemsControl", HorizontalAlignment="Stretch")
        self.task_panel = make("WrapPanel", "TaskListWrapPanel")
        self.task_items.local["Content"] = self.task_panel
        self.scroller.local["Content"] = self.task_items
        task_dock.set_children([self.thumb, self.scroller])

        dock.set_children([self.start, self.desktop, self.tray_box, self.task_list])
        self.update_host()
        self.set_tasks(self.tasks)
        self.set_tray(self.tray)

    def update_host(self) -> None:
        """Properties the WPF Taskbar window exposes to theme triggers."""
        s = self.settings
        horizontal = self.horizontal
        orientation = "Horizontal" if horizontal else "Vertical"
        theme = elements.theme()
        w = self.window
        w.local.update(
            {
                "Orientation": orientation,
                "AppBarEdge": self.edge.name.capitalize(),
                "Rows": int(s.RowCount),
                "IsLocked": bool(s.LockTaskbar),
                "AppBarMode": "AutoHide" if s.AutoHide else "Normal",
                "FlowDirection": "LeftToRight",
                "IsScaled": self.output_scale > 1 or self.scale > 1,
                "TextOptions.TextRenderingMode": "ClearType" if s.AllowFontSmoothing else "Aliased",
            }
        )
        self.language_flow = theme.flow_direction()
        leading, trailing = ("Left", "Right") if horizontal else ("Top", "Bottom")
        self.start.local["DockPanel.Dock"] = leading
        self.thumb.local["DockPanel.Dock"] = leading
        self.desktop.local["DockPanel.Dock"] = trailing
        self.tray_box.local["DockPanel.Dock"] = trailing
        self.desktop.local["Visibility"] = "Visible" if s.ShowDesktopButton else "Collapsed"
        self.clock.local["Visibility"] = "Visible" if s.ShowClock else "Collapsed"
        self.tray_stack.local["Orientation"] = orientation
        self.icon_list_stack.local["Orientation"] = orientation
        self.tray_panel.local["Orientation"] = "Vertical" if horizontal and s.RowCount > 1 else "Horizontal"
        self.task_panel.local["Orientation"] = orientation
        self.scroller.set_scrollable(not horizontal)
        elements.invalidate()

    # --- data -----------------------------------------------------------------------------------
    @staticmethod
    def _task_style(data: TaskData) -> str:
        return {"Active": "TaskButtonActive", "Flashing": "TaskButtonFlashing"}.get(data.State, "TaskButton")

    def _make_task(self, data: TaskData) -> Element:
        host = make("UserControl", DataContext=data)
        host.tag = "task"
        button = make("Button", self._task_style(data))
        grid = make("Grid")
        grid.col_defs = ["Auto", "*"]
        grid.add(make("Image", "TaskIcon", Source=Binding("Icon"), Grid__Column=0))
        grid.add(make("Image", "TaskOverlayIcon", Source=Binding("OverlayIcon"), Grid__Column=0, Visibility="Collapsed"))
        grid.add(make("TextBlock", "TaskLabel", Text=Binding("Title"), Grid__Column=1))
        button.local["Content"] = grid
        host.local["Content"] = button
        return host

    def set_tasks(self, tasks: list[TaskData]) -> None:
        self.tasks = list(tasks)
        children = []
        alive = set()
        for data in self.tasks:
            el = self._task_elements.get(data.key)
            if el is None:
                el = self._task_elements[data.key] = self._make_task(data)
            el.local["DataContext"] = data
            button = el.local["Content"]
            button.local["Style"] = ResourceRef(self._task_style(data))
            alive.add(data.key)
            children.append(el)
        for key in list(self._task_elements):
            if key not in alive:
                del self._task_elements[key]
        self.task_panel.set_children(children)
        elements.invalidate()

    def _make_tray(self, data: TrayData) -> Element:
        host = make("UserControl", DataContext=data)
        host.tag = "tray"
        stack = make("StackPanel")
        border = make("Border")
        border.add(make("Image", "NotifyIcon", Source=Binding("Icon")))
        stack.add(border)
        host.local["Content"] = stack
        return host

    def set_tray(self, items: list[TrayData]) -> None:
        self.tray = list(items)
        children = []
        alive = set()
        for data in self.tray:
            el = self._tray_elements.get(data.key)
            if el is None:
                el = self._tray_elements[data.key] = self._make_tray(data)
            el.local["DataContext"] = data
            alive.add(data.key)
            children.append(el)
        for key in list(self._tray_elements):
            if key not in alive:
                del self._tray_elements[key]
        self.tray_panel.set_children(children)
        elements.invalidate()

    def tick(self, now: datetime | None = None) -> None:
        self.clock_data.Now = now or datetime.now()
        elements.invalidate()

    # --- size & layout -------------------------------------------------------------------------
    def thickness(self) -> int:
        """Desired height (horizontal) or width (vertical) in logical pixels, as RecalculateSize does."""
        theme = elements.theme()
        s = self.settings
        row = self.scale * theme.number("TaskbarRowHeight")
        if self.horizontal:
            size = self.scale * theme.number("TaskbarHeight") + row * (max(1, int(s.RowCount)) - 1)
        else:
            size = self.scale * theme.number("TaskbarWidth") + row * (max(1, int(s.TaskbarWidth)) - 1)
        if s.AutoHide or not s.LockTaskbar:
            size += self.scale * theme.number("TaskbarUnlockedSize")
        return max(1, round(size))

    def layout(self, width: float, height: float) -> None:
        self.size = (width, height)
        lw, lh = width / self.scale, height / self.scale
        for _ in range(4):
            self.window.measure(lw, lh)
            self.window.arrange(0, 0, lw, lh)
            if not self._update_task_widths():
                break

    def _update_task_widths(self) -> bool:
        """TaskList.SetTaskButtonWidth and TaskButtonWidthConverter; returns True if layout must rerun."""
        theme = elements.theme()
        count = len(self.tasks)
        if not self.horizontal:
            base, extra, per_row, scrollable = self.task_list.rect.width(), 0, 1, True
        else:
            margin = theme.resource("TaskButtonMargin")
            margin = margin if isinstance(margin, Thickness) else Thickness.parse(str(margin or "0"))
            m = margin.left + margin.right
            rows = max(1, int(self.settings.RowCount))
            per_row = max(1, math.ceil(count / rows))
            available = self.task_items.rect.width()
            max_width = available / per_row
            default = theme.number("TaskButtonWidth", 160) + m
            minimum = theme.number("TaskButtonMinWidth", 0) + m
            if max_width > default:
                base, extra, scrollable = default, 0, False
            elif max_width < minimum:
                base, extra, scrollable = math.ceil(default / 2), 0, True
            else:
                base = math.floor(max_width)
                extra = int(math.floor(available)) - int(base) * per_row
                scrollable = False
        changed = scrollable != self.scroller.can_scroll
        self.scroller.set_scrollable(scrollable)
        for index, data in enumerate(self.tasks):
            el = self._task_elements[data.key]
            width = base
            if extra > 0:
                column = index % per_row
                width += (column + 1) * extra // per_row - column * extra // per_row
            if el.local.get("Width") != width:
                el.local["Width"] = float(width)
                changed = True
        if changed:
            elements.invalidate()
        return changed

    def render(self, p: QPainter) -> None:
        p.save()
        p.scale(self.scale, self.scale)
        self.window.render(p)
        p.restore()

    # --- hit testing & input ------------------------------------------------------------------
    def to_surface(self, rect: QRectF) -> QRectF:
        s = self.scale
        return QRectF(rect.x() * s, rect.y() * s, rect.width() * s, rect.height() * s)

    def element_at(self, x: float, y: float) -> Element | None:
        return self.window.hit(QPointF(x / self.scale, y / self.scale))

    def hit_at(self, x: float, y: float) -> Hit:
        el = self.element_at(x, y)
        node = el
        while node is not None:
            if node.type == "RepeatButton":
                command = str(node.get("Command") or "")
                if "PageUp" in command or "LineUp" in command or "PageLeft" in command:
                    return Hit("page_up", node)
                if "PageDown" in command or "LineDown" in command or "PageRight" in command:
                    return Hit("page_down", node)
            if isinstance(node.tag, str):
                return Hit(node.tag, node, node.local.get("DataContext"))
            node = node.parent
        return Hit("taskbar", self.window)

    @staticmethod
    def _button_of(el: Element | None) -> Element | None:
        node = el
        while node is not None:
            if node.type in BUTTON_TYPES:
                return node
            node = node.parent
        return None

    def _set_hover(self, el: Element | None) -> bool:
        chain = [] if el is None else [el, *el.ancestors()]
        if chain == self._hovered:
            return False
        new = {id(node) for node in chain}
        for node in self._hovered:
            if id(node) not in new:
                node.local["IsMouseOver"] = False
        for node in chain:
            node.local["IsMouseOver"] = True
        self._hovered = chain
        if self._pressed is not None:
            self._pressed.local["IsPressed"] = any(node is self._pressed for node in chain)
        elements.invalidate()
        return True

    def motion(self, x: float, y: float) -> bool:
        return self._set_hover(self.element_at(x, y))

    def leave(self) -> bool:
        return self._set_hover(None)

    def press(self, button: int, x: float, y: float) -> None:
        if button != LEFT_BUTTON:
            return
        el = self.element_at(x, y)
        self._pressed_hit = self.hit_at(x, y)
        self._pressed = self._button_of(el)
        if self._pressed is not None:
            self._pressed.local["IsPressed"] = True
            elements.invalidate()

    def release(self, button: int, x: float, y: float) -> None:
        hit = self.hit_at(x, y)
        if button == LEFT_BUTTON:
            pressed, pressed_hit = self._pressed, self._pressed_hit
            self._pressed = None
            self._pressed_hit = None
            if pressed is not None:
                pressed.local["IsPressed"] = False
                elements.invalidate()
            if pressed_hit is None or pressed_hit.element is not hit.element:
                return
            if hit.role == "page_up" or hit.role == "page_down":
                self.scroller.page(-1 if hit.role == "page_up" else 1)
                return
        self.on_activate(hit, button, self.to_surface(hit.element.rect))

    def double_click(self, x: float, y: float) -> None:
        self.on_double_click(self.hit_at(x, y))

    def scroll(self, x: float, y: float, delta: int) -> None:
        hit = self.hit_at(x, y)
        if hit.role in ("task", "task_list", "page_up", "page_down") and self.scroller.can_scroll:
            self.scroller.page(1 if delta > 0 else -1)
            return
        self.on_scroll(hit, delta)

    def tooltip_at(self, x: float, y: float) -> tuple[str, QRectF] | None:
        theme = elements.theme()
        hit = self.hit_at(x, y)
        rect = self.to_surface(hit.element.rect)
        match hit.role:
            case "start":
                return theme.tooltip_text("StartButtonTip", "start_button_tip"), rect
            case "desktop":
                return theme.tooltip_text("ShowDesktopButtonTip", "show_desktop_tip"), rect
            case "task" | "tray":
                return (hit.data.Title, rect) if hit.data is not None and hit.data.Title else None
            case "clock":
                return elements.format_datetime(self.clock_data.Now, "D"), rect
        return None

    def task_rect(self, key: Any) -> QRectF | None:
        el = self._task_elements.get(key)
        if el is None or el.rect.isEmpty():
            return None
        return self.to_surface(el.rect)
