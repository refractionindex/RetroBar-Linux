"""Toplevel window list from KWin's org_kde_plasma_window_management (the Linux side of TasksService)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QByteArray, QDataStream, QIODevice, QObject, QRectF, QSocketNotifier, pyqtSignal
from PyQt6.QtGui import QIcon

from ..desktop_entries import icon_for_app
from ..protocol.org_kde_plasma_virtual_desktop import OrgKdePlasmaVirtualDesktopManagement
from ..protocol.plasma_window_management import OrgKdePlasmaWindowManagement
from .client import ShmSurface, WaylandClient

log = logging.getLogger(__name__)

ACTIVE = 0x1
MINIMIZED = 0x2
MAXIMIZED = 0x4
FULLSCREEN = 0x8
ON_ALL_DESKTOPS = 0x40
DEMANDS_ATTENTION = 0x80
CLOSEABLE = 0x100
MINIMIZABLE = 0x200
MAXIMIZABLE = 0x400
SKIP_TASKBAR = 0x1000
MOVABLE = 0x8000
RESIZABLE = 0x10000


class Window:
    def __init__(self, manager: WindowManager, uuid: str, proxy: Any) -> None:
        self.manager = manager
        self.uuid = uuid
        self.proxy = proxy
        self.title = ""
        self.app_id = ""
        self.resource_name = ""
        self.pid = 0
        self.state = 0
        self.themed_icon = ""
        self.icon: QIcon | None = None
        self.desktops: set[str] = set()
        self.parent: Window | None = None
        self.geometry = (0, 0, 0, 0)
        self.ready = False
        self.unmapped = False
        self._icon_reader: QSocketNotifier | None = None
        self._icon_data = bytearray()
        d = proxy.dispatcher
        d["title_changed"] = self._title
        d["app_id_changed"] = self._app_id
        d["state_changed"] = self._state
        d["themed_icon_name_changed"] = self._themed_icon
        d["unmapped"] = self._unmapped
        d["initial_state"] = self._initial_state
        d["parent_window"] = self._parent
        d["geometry"] = self._geometry
        d["icon_changed"] = self._icon_changed
        d["pid_changed"] = self._pid
        d["virtual_desktop_entered"] = self._desktop_entered
        d["virtual_desktop_left"] = self._desktop_left
        d["resource_name_changed"] = self._resource_name

    def __repr__(self) -> str:
        return f"<Window {self.title!r} {self.app_id} state={self.state:#x}>"

    # --- events -----------------------------------------------------------------------------------
    def _changed(self) -> None:
        if self.ready:
            self.manager.window_changed(self)

    def _title(self, _p: Any, title: str) -> None:
        self.title = title
        self._changed()

    def _app_id(self, _p: Any, app_id: str) -> None:
        self.app_id = app_id
        if not self.themed_icon:
            self._fallback_icon()
        self._changed()

    def _resource_name(self, _p: Any, name: str) -> None:
        self.resource_name = name

    def _pid(self, _p: Any, pid: int) -> None:
        self.pid = pid

    def _state(self, _p: Any, flags: int) -> None:
        self.state = flags
        self._changed()

    def _themed_icon(self, _p: Any, name: str) -> None:
        self.themed_icon = name
        if name:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                self.icon = icon
                self._changed()

    def _icon_changed(self, _p: Any) -> None:
        if not self.themed_icon:
            self._fetch_icon()

    def _unmapped(self, _p: Any) -> None:
        self.unmapped = True
        self.manager.window_removed(self)

    def _initial_state(self, _p: Any) -> None:
        self.ready = True
        if self.icon is None:
            self._fetch_icon()
        self.manager.window_added(self)

    def _parent(self, _p: Any, parent: Any) -> None:
        self.parent = self.manager.by_proxy(parent) if parent is not None else None

    def _geometry(self, _p: Any, x: int, y: int, w: int, h: int) -> None:
        self.geometry = (x, y, w, h)

    def _desktop_entered(self, _p: Any, desktop: str) -> None:
        self.desktops.add(desktop)
        self._changed()

    def _desktop_left(self, _p: Any, desktop: str) -> None:
        self.desktops.discard(desktop)
        self._changed()

    # --- icons -------------------------------------------------------------------------------------
    def _fallback_icon(self) -> None:
        icon = icon_for_app(self.app_id) or (icon_for_app(self.resource_name) if self.resource_name else None)
        if icon is not None:
            self.icon = icon

    def _fetch_icon(self) -> None:
        if self._icon_reader is not None:
            return
        try:
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            return
        self.proxy.get_icon(write_fd)
        self.manager.client.flush()
        os.close(write_fd)
        self._icon_data = bytearray()
        self._icon_reader = QSocketNotifier(read_fd, QSocketNotifier.Type.Read)
        self._icon_reader.activated.connect(lambda *_: self._read_icon(read_fd))

    def _read_icon(self, fd: int) -> None:
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                self._icon_data += chunk
        except BlockingIOError:
            return
        except OSError:
            pass
        if self._icon_reader is not None:
            self._icon_reader.setEnabled(False)
            self._icon_reader.deleteLater()
            self._icon_reader = None
        os.close(fd)
        if self._icon_data:
            # QDataStream keeps a pointer to the array, so it must outlive the stream.
            buffer = QByteArray(bytes(self._icon_data))
            stream = QDataStream(buffer, QIODevice.OpenModeFlag.ReadOnly)
            icon = QIcon()
            stream >> icon
            if not icon.isNull():
                self.icon = icon
                self._changed()
                return
        if self.icon is None:
            self._fallback_icon()
            self._changed()

    # --- properties & requests -----------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return bool(self.state & ACTIVE)

    @property
    def minimized(self) -> bool:
        return bool(self.state & MINIMIZED)

    @property
    def maximized(self) -> bool:
        return bool(self.state & MAXIMIZED)

    @property
    def demands_attention(self) -> bool:
        return bool(self.state & DEMANDS_ATTENTION)

    def has(self, flag: int) -> bool:
        return bool(self.state & flag)

    def _set_state(self, flags: int, state: int) -> None:
        self.proxy.set_state(flags, state)
        self.manager.client.flush()

    def activate(self) -> None:
        self._set_state(ACTIVE | MINIMIZED, ACTIVE)

    def minimize(self) -> None:
        self._set_state(MINIMIZED, MINIMIZED)

    def maximize(self) -> None:
        self._set_state(MAXIMIZED | MINIMIZED | ACTIVE, MAXIMIZED | ACTIVE)

    def restore(self) -> None:
        if self.minimized:
            self.activate()
        elif self.maximized:
            self._set_state(MAXIMIZED, 0)

    def close(self) -> None:
        self.proxy.close()
        self.manager.client.flush()

    def request_move(self) -> None:
        self.activate()
        self.proxy.request_move()
        self.manager.client.flush()

    def request_resize(self) -> None:
        self.activate()
        self.proxy.request_resize()
        self.manager.client.flush()

    def set_minimized_geometry(self, panel: ShmSurface, rect: QRectF) -> None:
        self.proxy.set_minimized_geometry(panel.surface, int(rect.x()), int(rect.y()), max(1, int(rect.width())), max(1, int(rect.height())))


class WindowManager(QObject):
    """Tracks toplevels and the current virtual desktop; emits ``changed`` when the task list should update."""

    changed = pyqtSignal()
    show_desktop_changed = pyqtSignal(bool)

    def __init__(self, client: WaylandClient) -> None:
        super().__init__()
        self.client = client
        self.windows: dict[str, Window] = {}
        self.order: list[str] = []
        self.showing_desktop = False
        self.current_desktop: str | None = None
        self._desktops: dict[str, Any] = {}
        self.on_window_added: Callable[[Window], None] = lambda w: None
        self.wm = client.bind("org_kde_plasma_window_management", OrgKdePlasmaWindowManagement, 16)
        if self.wm is None:
            log.error(
                "org_kde_plasma_window_management is not available. KWin only offers it to trusted clients: "
                "install retrobar.desktop with X-KDE-Wayland-Interfaces (see README)."
            )
        else:
            self.wm.dispatcher["window_with_uuid"] = self._window_with_uuid
            self.wm.dispatcher["show_desktop_changed"] = self._show_desktop
        self.vdm = client.bind("org_kde_plasma_virtual_desktop_management", OrgKdePlasmaVirtualDesktopManagement, 2)
        if self.vdm is not None:
            self.vdm.dispatcher["desktop_created"] = self._desktop_created
            self.vdm.dispatcher["desktop_removed"] = self._desktop_removed
        client.roundtrip()

    @property
    def available(self) -> bool:
        return self.wm is not None

    # --- virtual desktops -----------------------------------------------------------------------------
    def _desktop_created(self, vdm: Any, desktop_id: str, _position: int) -> None:
        proxy = vdm.get_virtual_desktop(desktop_id)
        self._desktops[desktop_id] = proxy
        proxy.dispatcher["activated"] = lambda _p: self._desktop_activated(desktop_id)
        proxy.dispatcher["removed"] = lambda _p: self._desktops.pop(desktop_id, None)

    def _desktop_removed(self, _vdm: Any, desktop_id: str) -> None:
        self._desktops.pop(desktop_id, None)

    def _desktop_activated(self, desktop_id: str) -> None:
        self.current_desktop = desktop_id
        self.changed.emit()

    # --- windows ----------------------------------------------------------------------------------------
    def _window_with_uuid(self, wm: Any, _id: int, uuid: str) -> None:
        if uuid in self.windows:
            return
        proxy = wm.get_window_by_uuid(uuid)
        self.windows[uuid] = Window(self, uuid, proxy)

    def _show_desktop(self, _wm: Any, state: int) -> None:
        self.showing_desktop = bool(state)
        self.show_desktop_changed.emit(self.showing_desktop)

    def by_proxy(self, proxy: Any) -> Window | None:
        for window in self.windows.values():
            if window.proxy is proxy:
                return window
        return None

    def window_added(self, window: Window) -> None:
        if window.uuid not in self.order:
            self.order.append(window.uuid)
        self.on_window_added(window)
        self.changed.emit()

    def window_changed(self, _window: Window) -> None:
        self.changed.emit()

    def window_removed(self, window: Window) -> None:
        self.windows.pop(window.uuid, None)
        if window.uuid in self.order:
            self.order.remove(window.uuid)
        try:
            window.proxy.destroy()
        except Exception:  # noqa: BLE001 - already gone on the server
            pass
        self.changed.emit()

    def taskbar_windows(self) -> list[Window]:
        """Windows that belong on the taskbar, in the order they appeared."""
        result = []
        for uuid in self.order:
            window = self.windows.get(uuid)
            if window is None or not window.ready or window.unmapped or window.has(SKIP_TASKBAR):
                continue
            if self.current_desktop and window.desktops and not window.has(ON_ALL_DESKTOPS) and self.current_desktop not in window.desktops:
                continue
            result.append(window)
        return result

    def active_window(self) -> Window | None:
        for window in self.windows.values():
            if window.active and window.ready:
                return window
        return None

    def set_show_desktop(self, show: bool) -> None:
        if self.wm is not None:
            self.wm.show_desktop(1 if show else 0)
            self.client.flush()

    def move(self, uuid: str, new_index: int) -> None:
        """Reorder a task (drag and drop in the task list)."""
        if uuid in self.order:
            self.order.remove(uuid)
            self.order.insert(max(0, min(new_index, len(self.order))), uuid)
            self.changed.emit()
