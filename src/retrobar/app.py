"""The RetroBar application: taskbars on layer-shell surfaces wired to windows, tray, clock, menus and settings."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import dbus
import dbus.service
from PyQt6.QtCore import QObject, QRectF, QTimer
from PyQt6.QtGui import QIcon, QPainter
from PyQt6.QtWidgets import QApplication

from . import desktop_entries, dotnet_format
from .applications import Application, Catalog
from .settings import ClockClick, Edge, Settings, TaskMiddleClick
from .theme import Theme, install_font_substitutes, install_icon_theme
from .tray import TrayHost, TrayItem, install_main_loop
from .ui import elements
from .ui.menu import CalendarPopup, MenuItem, MenuPopup, TooltipPopup
from .ui.start_menu import StartActions, StartMenuPopup, banner_text, search_items, start_items
from .ui.view import Hit, TaskbarView, TaskData, TrayData
from .wayland.client import (
    ANCHOR_BOTTOM,
    ANCHOR_LEFT,
    ANCHOR_RIGHT,
    ANCHOR_TOP,
    KEYBOARD_NONE,
    KEYBOARD_ON_DEMAND,
    LayerSurface,
    Output,
    WaylandClient,
)
from .wayland.windows import MAXIMIZABLE, MINIMIZABLE, MOVABLE, RESIZABLE, Window, WindowManager
from .xaml.markup import ResourceRef
from .xaml.resources import Style

log = logging.getLogger(__name__)

BUS_NAME = "io.github.dremin.RetroBar"
BUS_PATH = "/io/github/dremin/RetroBar"
AUTOHIDE_VISIBLE = 2
DOUBLE_CLICK_SECONDS = 0.4
TOOLTIP_DELAY_MS = 500
START_GRACE = 1.0  # seconds the Start button stays pressed while the launcher is still mapping
# A press on Start that the compositor already used to dismiss the menu must not reopen it.
START_DISMISS_SECONDS = 0.3

ANCHORS = {
    Edge.BOTTOM: ANCHOR_BOTTOM | ANCHOR_LEFT | ANCHOR_RIGHT,
    Edge.TOP: ANCHOR_TOP | ANCHOR_LEFT | ANCHOR_RIGHT,
    Edge.LEFT: ANCHOR_LEFT | ANCHOR_TOP | ANCHOR_BOTTOM,
    Edge.RIGHT: ANCHOR_RIGHT | ANCHOR_TOP | ANCHOR_BOTTOM,
}
# Where popups open relative to the anchor so they move away from the screen edge.
POPUP_GRAVITY = {Edge.BOTTOM: "top_right", Edge.TOP: "bottom_right", Edge.LEFT: "bottom_right", Edge.RIGHT: "bottom_left"}
TOOLTIP_SIDE = {Edge.BOTTOM: "top", Edge.TOP: "bottom", Edge.LEFT: "right", Edge.RIGHT: "left"}
# The corner of the Start button the Start menu grows from.
START_ANCHOR = {Edge.BOTTOM: "top_left", Edge.TOP: "bottom_left", Edge.LEFT: "top_right", Edge.RIGHT: "top_left"}


class StartMenuService(dbus.service.Object):
    """``retrobar --start-menu`` (or a global shortcut) toggles the Start menu of the running instance."""

    def __init__(self, bus: dbus.SessionBus, app: RetroBar) -> None:
        super().__init__(bus, BUS_PATH)
        self.app = app

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="")
    def ToggleStartMenu(self) -> None:  # noqa: N802 - D-Bus method name
        QTimer.singleShot(0, self.app.toggle_start_menu)


class Taskbar(LayerSurface):
    def __init__(self, app: RetroBar, output: Output | None) -> None:
        # KWin maps the layer namespace to a window type; "dock" keeps the bar up during show desktop.
        super().__init__(app.client, output, "dock")
        self.app = app
        self.view = TaskbarView(app.settings)
        self.view.on_activate = lambda hit, button, rect: app.activate(self, hit, button, rect)
        self.view.on_scroll = lambda hit, delta: app.scroll(self, hit, delta)
        self.view.on_double_click = lambda hit: app.double_click(self, hit)
        self.pointer_pos = (0.0, 0.0)
        self.hovering = False
        self.hidden = bool(app.settings.AutoHide)
        self.tooltip: TooltipPopup | None = None
        self._tip_hit: tuple[str, int] | None = None
        self._tip_timer = QTimer()
        self._tip_timer.setSingleShot(True)
        self._tip_timer.setInterval(TOOLTIP_DELAY_MS)
        self._tip_timer.timeout.connect(self._show_tooltip)
        self._hide_timer = QTimer()
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(700)
        self._hide_timer.timeout.connect(self.autohide)
        self._last_click: tuple[float, int] = (0.0, 0)
        self.press_closed_start = False
        self._minimized_rects: dict[Any, tuple[int, int, int, int]] = {}
        self.apply_geometry()

    # --- geometry -----------------------------------------------------------------------------------
    def apply_geometry(self) -> None:
        settings = self.app.settings
        edge = settings.edge
        thickness = self.view.thickness()
        horizontal = edge in (Edge.TOP, Edge.BOTTOM)
        width, height = (0, thickness) if horizontal else (thickness, 0)
        exclusive = 0 if settings.AutoHide else thickness
        offset = -(thickness - AUTOHIDE_VISIBLE) if settings.AutoHide and self.hidden else 0
        margins = {
            Edge.TOP: (offset, 0, 0, 0),
            Edge.RIGHT: (0, offset, 0, 0),
            Edge.BOTTOM: (0, 0, offset, 0),
            Edge.LEFT: (0, 0, 0, offset),
        }[edge]
        self.setup(ANCHORS[edge], width, height, exclusive, margins)
        self.request_redraw()

    def show_bar(self) -> None:
        self._hide_timer.stop()
        if self.hidden:
            self.hidden = False
            self.apply_geometry()

    def autohide(self) -> None:
        if not self.app.settings.AutoHide or self.hovering or self.app.popup_open(self):
            return
        if not self.hidden:
            self.hidden = True
            self.apply_geometry()

    def closed(self) -> None:
        self.app.taskbar_closed(self)

    # --- painting -------------------------------------------------------------------------------------
    def paint(self, p: QPainter, rect: QRectF) -> None:
        self.view.layout(rect.width(), rect.height())
        self.view.render(p)
        QTimer.singleShot(0, self._update_minimized_geometry)

    def _update_minimized_geometry(self) -> None:
        if self._destroyed:
            return
        for task in self.view.tasks:
            rect = self.view.task_rect(task.key)
            window = task.window
            if rect is None or window is None:
                continue
            key = (int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height()))
            if self._minimized_rects.get(task.key) != key:
                self._minimized_rects[task.key] = key
                window.set_minimized_geometry(self, rect)

    # --- input ------------------------------------------------------------------------------------------
    def pointer_enter(self, x: float, y: float) -> None:
        self.hovering = True
        self.show_bar()
        self.pointer_motion(x, y)

    def pointer_motion(self, x: float, y: float) -> None:
        self.pointer_pos = (x, y)
        if self.view.motion(x, y):
            self.request_redraw()
        hit = self.view.hit_at(x, y)
        key = (hit.role, id(hit.element))
        if key != self._tip_hit:
            self._tip_hit = key
            self.close_tooltip()
            self._tip_timer.start()

    def pointer_leave(self) -> None:
        self.hovering = False
        self._tip_hit = None
        self._tip_timer.stop()
        self.close_tooltip()
        if self.view.leave():
            self.request_redraw()
        if self.app.settings.AutoHide:
            self._hide_timer.start()

    def pointer_button(self, button: int, pressed: bool, x: float, y: float) -> None:
        self._tip_timer.stop()
        self.close_tooltip()
        self.pointer_pos = (x, y)
        if pressed:
            self.press_closed_start = self.app.start_menu_open() or time.monotonic() - self.app.start_closed_at < START_DISMISS_SECONDS
            self.view.press(button, x, y)
            self.request_redraw()
            return
        hit = self.view.hit_at(x, y)
        now = time.monotonic()
        double = button == 1 and now - self._last_click[0] < DOUBLE_CLICK_SECONDS and self._last_click[1] == id(hit.element)
        self._last_click = (0.0, 0) if double else (now, id(hit.element))
        self.view.release(button, x, y)
        if double:
            self.view.double_click(x, y)
        self.request_redraw()

    def pointer_axis(self, value: float, x: float, y: float) -> None:
        self.view.scroll(x, y, 1 if value > 0 else -1)
        self.request_redraw()

    # --- tooltips ----------------------------------------------------------------------------------------
    def _show_tooltip(self) -> None:
        if not self.hovering or self.app.popup_open(self):
            return
        x, y = self.pointer_pos
        found = self.view.tooltip_at(x, y)
        if not found:
            return
        text, rect = found
        self.close_tooltip()
        self.tooltip = TooltipPopup(self.client, self, text, rect, TOOLTIP_SIDE[self.app.settings.edge], not self.app.settings.AllowFontSmoothingMenu)

    def close_tooltip(self) -> None:
        if self.tooltip is not None:
            self.tooltip.destroy()
            self.tooltip = None

    def destroy(self) -> None:
        self._tip_timer.stop()
        self._hide_timer.stop()
        self.close_tooltip()
        super().destroy()


class RetroBar(QObject):
    def __init__(self, qapp: QApplication) -> None:
        super().__init__()
        self.qapp = qapp
        self.settings = Settings()
        if self.settings.DebugLogging:
            logging.getLogger().setLevel(logging.DEBUG)
        install_icon_theme()
        install_font_substitutes()
        install_main_loop()
        self.bus = dbus.SessionBus()
        self.theme = Theme()
        self._load_theme()
        self._apply_clock_format()
        self.client = WaylandClient()
        self.client.disconnected.connect(qapp.quit)
        self.windows = WindowManager(self.client)
        self.windows.changed.connect(self._schedule_tasks)
        self.windows.show_desktop_changed.connect(self._show_desktop_changed)
        try:
            self.tray: TrayHost | None = TrayHost()
            self.tray.changed.connect(self._update_tray)
        except dbus.DBusException as exc:
            log.warning("Notification area unavailable: %s", exc)
            self.tray = None
        self.taskbars: dict[Any, Taskbar] = {}
        self.menu: MenuPopup | None = None
        self.calendar: CalendarPopup | None = None
        self.start_menu: StartMenuPopup | None = None
        self.start_closed_at = 0.0
        self.catalog = Catalog(self)
        self.properties: Any = None
        self._task_data: dict[str, TaskData] = {}
        self._tray_data: dict[str, TrayData] = {}
        self._tasks_timer = QTimer(self)
        self._tasks_timer.setSingleShot(True)
        self._tasks_timer.setInterval(15)
        self._tasks_timer.timeout.connect(self._update_tasks)
        self._outputs_timer = QTimer(self)
        self._outputs_timer.setSingleShot(True)
        self._outputs_timer.setInterval(200)
        self._outputs_timer.timeout.connect(self._sync_taskbars)
        self.client.outputs_changed.connect(self._outputs_timer.start)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick)
        self._clock_timer.start(250)
        self._last_clock = ""
        self._start_opened_at = 0.0
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.setInterval(int(START_GRACE * 1000) + 50)
        self._start_timer.timeout.connect(self._update_start)
        self.settings.subscribe(self._setting_changed)
        self._sync_taskbars()
        self._update_tasks()
        self._update_tray()
        try:
            self._service: StartMenuService | None = StartMenuService(self.bus, self)
        except (dbus.DBusException, KeyError) as exc:
            log.warning("Start menu D-Bus interface unavailable: %s", exc)
            self._service = None

    # --- theme --------------------------------------------------------------------------------------
    def _load_theme(self) -> None:
        self.theme.load(self.settings.Theme, self.settings.Language)
        elements.set_theme(self.theme)

    def _apply_clock_format(self) -> None:
        s = self.settings
        if s.OverrideClockFormat and s.ClockFormat:
            dotnet_format.set_short_time_pattern(s.ClockFormat.replace("\\n", "\n"))
        elif s.ShowClockSeconds:
            dotnet_format.set_short_time_pattern("T")
        else:
            dotnet_format.set_short_time_pattern(None)
        self._last_clock = ""

    # --- taskbars -------------------------------------------------------------------------------------
    def _sync_taskbars(self) -> None:
        if self.settings.ShowMultiMon:
            wanted: dict[Any, Output | None] = {o.global_name: o for o in self.client.outputs.values() if o.done}
        else:
            wanted = {None: None}
        for key in list(self.taskbars):
            if key not in wanted:
                self.taskbars.pop(key).destroy()
        for key, output in wanted.items():
            if key not in self.taskbars:
                bar = Taskbar(self, output)
                self.taskbars[key] = bar
                bar.view.set_tasks(self._current_tasks())
                bar.view.set_tray(self._current_tray())
                bar.view.update_host()
        self.client.flush()

    def taskbar_closed(self, bar: Taskbar) -> None:
        for key, value in list(self.taskbars.items()):
            if value is bar:
                del self.taskbars[key]
        bar.destroy()
        self._outputs_timer.start()

    def _redraw(self) -> None:
        for bar in self.taskbars.values():
            bar.request_redraw()

    def _rebuild(self) -> None:
        tasks, tray = self._current_tasks(), self._current_tray()
        for bar in self.taskbars.values():
            bar.view.build()
            bar.view.set_tasks(tasks)
            bar.view.set_tray(tray)
            bar.apply_geometry()

    # --- tasks & tray ---------------------------------------------------------------------------------------
    def _schedule_tasks(self) -> None:
        self._tasks_timer.start()

    def _current_tasks(self) -> list[TaskData]:
        result = []
        alive = set()
        for window in self.windows.taskbar_windows():
            data = self._task_data.get(window.uuid)
            if data is None:
                data = self._task_data[window.uuid] = TaskData(key=window.uuid, window=window)
            data.Title = window.title
            data.Icon = window.icon or QIcon.fromTheme("application-x-executable")
            if window.active and not window.minimized:
                data.State = "Active"
            elif window.demands_attention:
                data.State = "Flashing"
            else:
                data.State = "Normal"
            alive.add(window.uuid)
            result.append(data)
        for key in list(self._task_data):
            if key not in alive:
                del self._task_data[key]
        return result

    def _update_tasks(self) -> None:
        tasks = self._current_tasks()
        for bar in self.taskbars.values():
            bar.view.set_tasks(tasks)
            bar.request_redraw()
        if self._start_opened_at:
            self._update_start()

    def _current_tray(self) -> list[TrayData]:
        if self.tray is None:
            return []
        result = []
        alive = set()
        for item in self.tray.visible_items():
            data = self._tray_data.get(item.key)
            if data is None:
                data = self._tray_data[item.key] = TrayData(key=item.key, item=item)
            data.Title = item.title
            data.Icon = item.icon
            alive.add(item.key)
            result.append(data)
        for key in list(self._tray_data):
            if key not in alive:
                del self._tray_data[key]
        return result

    def _update_tray(self) -> None:
        tray = self._current_tray()
        for bar in self.taskbars.values():
            bar.view.set_tray(tray)
            bar.request_redraw()

    def _show_desktop_changed(self, showing: bool) -> None:
        for bar in self.taskbars.values():
            bar.view.desktop_button.local["IsChecked"] = showing
        elements.invalidate()
        self._redraw()

    def _tick(self) -> None:
        now = datetime.now()
        text = dotnet_format.format_datetime(now, "t")
        if text == self._last_clock:
            return
        self._last_clock = text
        for bar in self.taskbars.values():
            bar.view.tick(now)
            bar.request_redraw()

    # --- settings -------------------------------------------------------------------------------------------
    def _setting_changed(self, name: str) -> None:
        match name:
            case "Theme" | "Language":
                self._load_theme()
                self._last_clock = ""
                self._rebuild()
            case "Edge" | "RowCount" | "TaskbarWidth" | "TaskbarScale" | "LockTaskbar" | "AutoHide":
                for bar in self.taskbars.values():
                    bar.hidden = bool(self.settings.AutoHide)
                    bar.view.update_host()
                    bar.apply_geometry()
            case "ShowMultiMon":
                self._sync_taskbars()
            case "ShowClockSeconds" | "OverrideClockFormat" | "ClockFormat":
                self._apply_clock_format()
                self._tick()
                for bar in self.taskbars.values():
                    bar.apply_geometry()
            case "DebugLogging":
                logging.getLogger().setLevel(logging.DEBUG if self.settings.DebugLogging else logging.INFO)
            case _:
                for bar in self.taskbars.values():
                    bar.view.update_host()
                self._redraw()

    # --- popups ---------------------------------------------------------------------------------------------
    def _popups(self) -> tuple[Any, ...]:
        return (self.menu, self.calendar, self.start_menu)

    def popup_open(self, bar: Taskbar) -> bool:
        for popup in self._popups():
            if popup is not None and not popup._destroyed and popup.parent is bar:
                return True
        return False

    def start_menu_open(self) -> bool:
        return self.start_menu is not None and not self.start_menu._destroyed

    def close_popups(self) -> None:
        if self.menu is not None:
            self.menu.close_tree()
            self.menu = None
        if self.calendar is not None:
            self.calendar.destroy()
            self.calendar = None
            self._update_keyboard_focus()
        if self.start_menu is not None:
            self.start_menu.close_tree()
            self.start_menu = None

    def _update_keyboard_focus(self) -> None:
        """A bar accepts keyboard focus only while one of its popups is open, so menus can be driven by keys."""
        for bar in self.taskbars.values():
            bar.set_keyboard_focus(KEYBOARD_ON_DEMAND if self.popup_open(bar) else KEYBOARD_NONE)

    def _popup_closed(self, bar: Taskbar) -> None:
        self.menu = None
        self._update_keyboard_focus()
        if self.settings.AutoHide and not bar.hovering:
            bar._hide_timer.start()

    def show_menu(self, bar: Taskbar, items: list[MenuItem], at: tuple[float, float] | None = None) -> None:
        self.close_popups()
        bar.close_tooltip()
        x, y = at if at is not None else bar.pointer_pos
        menu = MenuPopup(self.client, bar, items, QRectF(x, y, 1, 1), "top_left", POPUP_GRAVITY[self.settings.edge], not self.settings.AllowFontSmoothingMenu)
        menu.on_closed = lambda: self._popup_closed(bar)
        self.menu = menu
        self._update_keyboard_focus()

    def show_calendar(self, bar: Taskbar, rect: QRectF) -> None:
        if self.calendar is not None and not self.calendar._destroyed:
            self.close_popups()
            return
        self.close_popups()
        bar.close_tooltip()
        side = TOOLTIP_SIDE[self.settings.edge]
        self.calendar = CalendarPopup(self.client, bar, rect, side, not self.settings.AllowFontSmoothingMenu)

        def done() -> None:
            self.calendar = None
            self._update_keyboard_focus()

        self.calendar.on_done = done

    # --- Start menu -------------------------------------------------------------------------------------------
    def _start_actions(self, bar: Taskbar) -> StartActions:
        def with_token(go: Any) -> None:
            self.client.request_activation_token(bar, go)

        def launch(app: Application) -> None:
            with_token(lambda token: desktop_entries.launch_desktop_file(app.path, token))

        def open_document(path: Path) -> None:
            with_token(lambda token: desktop_entries.open_path(path, token))

        def printers() -> None:
            if shutil.which("kcmshell6"):
                self._run(["kcmshell6", "kcm_printer_manager"], bar)
            else:
                self._run(["systemsettings", "kcm_printer_manager"], bar)

        return StartActions(
            launch=launch,
            open_document=open_document,
            control_panel=lambda: self._run(["systemsettings"], bar),
            printers=printers,
            taskbar_properties=self.open_properties,
            run=lambda: self.open_run(bar),
            log_off=lambda: self._logout_prompt("promptLogout"),
            shut_down=lambda: self._logout_prompt("promptShutDown"),
        )

    def _screen_height(self, bar: Taskbar) -> int:
        output = bar.output or next((o for o in self.client.outputs.values() if o.done), None)
        screens = self.qapp.screens()
        for screen in screens:
            if output is not None and screen.name() == output.name:
                return screen.geometry().height()
        if output is not None and output.height:
            return output.height // max(1, output.scale)
        return screens[0].geometry().height() if screens else 768

    def show_start_menu(self, bar: Taskbar) -> None:
        self.close_popups()
        bar.close_tooltip()
        view = bar.view
        button = view.to_surface(view.start_button.rect)
        edge = self.settings.edge
        # Grow from the bar's outer edge so the menu sits flush on it, like Windows.
        if edge in (Edge.TOP, Edge.BOTTOM):
            anchor_rect = QRectF(button.x(), 0, max(1.0, button.width()), max(1, bar.height))
            limit = self._screen_height(bar) - bar.height
        else:
            anchor_rect = QRectF(0, button.y(), max(1, bar.width), max(1.0, button.height()))
            limit = self._screen_height(bar)
        actions = self._start_actions(bar)
        menu = StartMenuPopup(
            self.client,
            bar,
            start_items(self.catalog, actions),
            anchor_rect,
            START_ANCHOR[edge],
            POPUP_GRAVITY[edge],
            not self.settings.AllowFontSmoothingMenu,
            search=lambda query: search_items(self.catalog, query, actions),
            banner=banner_text(),
            max_height=max(200, limit - 8),
        )
        menu.on_closed = lambda: self._start_menu_closed(bar, menu)
        self.start_menu = menu
        self._update_keyboard_focus()
        self._update_start()

    def _start_menu_closed(self, bar: Taskbar, menu: StartMenuPopup) -> None:
        if self.start_menu is menu:
            self.start_menu = None
        self.start_closed_at = time.monotonic()
        self._update_keyboard_focus()
        self._update_start()
        if self.settings.AutoHide and not bar.hovering:
            bar._hide_timer.start()

    def toggle_start_menu(self) -> None:
        """Toggle from outside (D-Bus): on the bar under the pointer, else the first one."""
        if self.start_menu_open():
            self.close_popups()
            return
        bar = next((b for b in self.taskbars.values() if b.hovering), None) or next(iter(self.taskbars.values()), None)
        if bar is not None:
            bar.show_bar()
            self.open_start(bar)

    def open_run(self, bar: Taskbar) -> None:
        def failed(exc: Exception) -> None:
            log.debug("KRunner D-Bus unavailable (%s); starting it", exc)
            self._run(["krunner"], bar)

        try:
            krunner = self.bus.get_object("org.kde.krunner", "/App", introspect=False)
            krunner.display(dbus_interface="org.kde.krunner.App", reply_handler=lambda *a: None, error_handler=failed)
        except dbus.DBusException as exc:
            failed(exc)

    def _logout_prompt(self, method: str) -> None:
        def failed(exc: Exception) -> None:
            log.warning("Plasma logout prompt unavailable: %s", exc)

        try:
            prompt = self.bus.get_object("org.kde.LogoutPrompt", "/LogoutPrompt", introspect=False)
            getattr(prompt, method)(dbus_interface="org.kde.LogoutPrompt", reply_handler=lambda *a: None, error_handler=failed)
        except dbus.DBusException as exc:
            failed(exc)

    # --- menus ----------------------------------------------------------------------------------------------
    def _label(self, key: str) -> str:
        return self.theme.string(key)

    def _task_manager_label(self) -> str:
        style = self.theme.resource("TaskManMenuItem")
        if isinstance(style, Style):
            setter = style.all_setters().get("Header")
            if setter is not None:
                value = setter.value
                if isinstance(value, ResourceRef):
                    return self.theme.string(str(value.key))
                if isinstance(value, str):
                    return value
        return self._label("show_taskman")

    def _common_items(self) -> list[MenuItem]:
        s = self.settings
        return [
            MenuItem(self._task_manager_label(), self.open_task_manager),
            MenuItem.sep(),
            MenuItem(self._label("lock_taskbar_menu"), lambda: setattr(s, "LockTaskbar", not s.LockTaskbar), checked=bool(s.LockTaskbar)),
            MenuItem(self._label("tray_properties"), self.open_properties),
            MenuItem(self._label("exit_retrobar"), self.qapp.quit, visible=bool(s.ShowExitMenuItem)),
        ]

    def taskbar_menu(self) -> list[MenuItem]:
        return self._common_items()

    def tray_area_menu(self) -> list[MenuItem]:
        weight = self.theme.resource("SetTimeMenuItemWeight")
        bold = isinstance(weight, int) and weight >= 600
        return [
            MenuItem(self._label("set_time"), self.open_date_time, bold=bold),
            MenuItem(self._label("customize_notifications_menu"), lambda: self._run(["systemsettings", "kcm_notifications"])),
            MenuItem.sep(),
            *self._common_items(),
        ]

    def task_menu(self, window: Window) -> list[MenuItem]:
        minimized, maximized = window.minimized, window.maximized
        return [
            MenuItem(self._label("restore"), window.restore, enabled=minimized or maximized, glyph="2"),
            MenuItem(self._label("move"), window.request_move, enabled=not maximized and window.has(MOVABLE)),
            MenuItem(self._label("size"), window.request_resize, enabled=not minimized and not maximized and window.has(RESIZABLE)),
            MenuItem(self._label("minimize"), window.minimize, enabled=not minimized and window.has(MINIMIZABLE), glyph="0"),
            MenuItem(self._label("maximize"), window.maximize, enabled=not maximized and window.has(MAXIMIZABLE), glyph="1"),
            MenuItem.sep(),
            MenuItem(self._label("close"), window.close, bold=True, glyph="r"),
        ]

    def _dbusmenu_items(self, item: TrayItem, nodes: list[dict[str, Any]]) -> list[MenuItem]:
        result = []
        for node in nodes:
            if not node["visible"]:
                continue
            if node["type"] == "separator":
                result.append(MenuItem.sep())
                continue
            checked = None
            if node["toggle_type"] in ("checkmark", "radio"):
                checked = node["toggle_state"] == 1
            icon = QIcon.fromTheme(node["icon_name"]) if node["icon_name"] else None
            item_id = node["id"]
            entry = MenuItem(
                node["label"],
                (lambda i=item_id: item.menu_event(i)) if not node["children"] else None,
                enabled=node["enabled"],
                checked=checked,
                radio=node["toggle_type"] == "radio",
                icon=icon,
            )
            if node["children"]:
                children = node["children"]
                entry.submenu = lambda c=children, i=item_id: (item.menu_about_to_show(i), self._dbusmenu_items(item, c))[1]
            result.append(entry)
        return result

    def show_tray_menu(self, bar: Taskbar, item: TrayItem, at: tuple[float, float]) -> None:
        def got(nodes: list[dict[str, Any]] | None) -> None:
            if nodes:
                self.show_menu(bar, self._dbusmenu_items(item, nodes), at)
            else:
                item.context_menu()

        item.fetch_menu(got)

    # --- actions ------------------------------------------------------------------------------------------------
    def _run(self, argv: list[str], bar: Taskbar | None = None) -> None:
        def go(token: str) -> None:
            desktop_entries.launch(argv, token)

        self.client.request_activation_token(bar or next(iter(self.taskbars.values()), None), go)

    def open_task_manager(self) -> None:
        for candidate in (["plasma-systemmonitor"], ["ksysguard"], ["gnome-system-monitor"], ["xfce4-taskmanager"]):
            if shutil.which(candidate[0]):
                self._run(candidate)
                return
        log.warning("No task manager found")

    def open_date_time(self) -> None:
        if shutil.which("kcmshell6"):
            self._run(["kcmshell6", "kcm_clock"])
        elif shutil.which("systemsettings"):
            self._run(["systemsettings", "kcm_clock"])

    def open_properties(self) -> None:
        from .properties import PropertiesDialog

        if self.properties is None:
            self.properties = PropertiesDialog(self.settings, self.theme)
            self.properties.finished.connect(lambda _r: setattr(self, "properties", None))
        self.properties.show()
        self.properties.raise_()
        self.properties.activateWindow()

    def open_start(self, bar: Taskbar, clicked: bool = False) -> None:
        if clicked and bar.press_closed_start:
            bar.press_closed_start = False
            self.close_popups()
            return
        self.close_popups()
        command = (self.settings.StartMenuCommand or "").strip()
        if command:
            self._run(["sh", "-c", command], bar)
            return
        if self.settings.ClassicStartMenu:
            self.show_start_menu(bar)
            return

        def failed(exc: Exception) -> None:
            log.warning("Plasma launcher unavailable (%s); falling back to KRunner", exc)
            self._run(["krunner"], bar)

        try:
            shell = self.bus.get_object("org.kde.plasmashell", "/PlasmaShell", introspect=False)
            shell.activateLauncherMenu(dbus_interface="org.kde.PlasmaShell", reply_handler=lambda *a: None, error_handler=failed)
        except dbus.DBusException as exc:
            failed(exc)
            return
        self._start_opened_at = time.monotonic()
        self._start_timer.start()
        self._update_start()

    def _update_start(self) -> None:
        """Keep the Start button pressed while our Start menu, or the Plasma launcher we opened, is up."""
        active = self.windows.active_window()
        launcher_active = active is not None and active.app_id == "org.kde.plasmashell"
        pending = time.monotonic() - self._start_opened_at < START_GRACE
        launcher = self._start_opened_at > 0 and (launcher_active or pending)
        if not launcher:
            self._start_opened_at = 0.0
        checked = launcher or self.start_menu_open()
        for bar in self.taskbars.values():
            if bool(bar.view.start_button.local.get("IsChecked")) != checked:
                bar.view.start_button.local["IsChecked"] = checked
                elements.invalidate()
                bar.request_redraw()

    def activate(self, bar: Taskbar, hit: Hit, button: int, rect: QRectF) -> None:
        role = hit.role
        at = bar.pointer_pos
        if button == 1:
            match role:
                case "start":
                    self.open_start(bar, clicked=True)
                case "task":
                    window = hit.data.window
                    if window.active and not window.minimized:
                        window.minimize()
                    else:
                        window.activate()
                case "tray":
                    item: TrayItem = hit.data.item
                    if item.item_is_menu and item.menu_path:
                        self.show_tray_menu(bar, item, at)
                    else:
                        self._activate_tray_item(bar, item)
                case "clock":
                    action = int(self.settings.ClockClickAction)
                    if action != ClockClick.DO_NOTHING:
                        self.show_calendar(bar, rect)
                case "desktop":
                    self.windows.set_show_desktop(not self.windows.showing_desktop)
        elif button == 2:
            match role:
                case "task":
                    window = hit.data.window
                    action = int(self.settings.TaskMiddleClickAction)
                    if action == TaskMiddleClick.OPEN_NEW_INSTANCE:
                        self.client.request_activation_token(bar, lambda token: desktop_entries.launch_desktop_file(window.app_id, token))
                    elif action == TaskMiddleClick.CLOSE_TASK:
                        window.close()
                case "tray":
                    hit.data.item.secondary_activate()
        elif button == 3:
            match role:
                case "task":
                    self.show_menu(bar, self.task_menu(hit.data.window), at)
                case "tray":
                    self.show_tray_menu(bar, hit.data.item, at)
                case "clock" | "tray_area":
                    self.show_menu(bar, self.tray_area_menu(), at)
                case "desktop":
                    self.show_menu(
                        bar,
                        [
                            MenuItem(self._label("show_desktop"), lambda: self.windows.set_show_desktop(not self.windows.showing_desktop), bold=True),
                            MenuItem(self._label("tray_properties"), self.open_properties),
                        ],
                        at,
                    )
                case _:
                    self.show_menu(bar, self.taskbar_menu(), at)

    def _activate_tray_item(self, bar: Taskbar, item: TrayItem) -> None:
        def go(token: str) -> None:
            if token:
                item.provide_activation_token(token)
            item.activate()

        self.client.request_activation_token(bar, go)

    def double_click(self, bar: Taskbar, hit: Hit) -> None:
        if hit.role == "clock":
            self.close_popups()
            self.open_date_time()

    def scroll(self, bar: Taskbar, hit: Hit, delta: int) -> None:
        if hit.role == "tray":
            hit.data.item.scroll(delta * 120)

    def shutdown(self) -> None:
        self.close_popups()
        for bar in self.taskbars.values():
            bar.destroy()
        self.taskbars.clear()
        self.client.flush()


def claim_single_instance() -> bool:
    install_main_loop()
    bus = dbus.SessionBus()
    result = bus.request_name(BUS_NAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    return result in (dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER, dbus.bus.REQUEST_NAME_REPLY_ALREADY_OWNER)
