"""StatusNotifierItem host: the Linux counterpart of the Windows notification area."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import dbus
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap

log = logging.getLogger(__name__)

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_IFACE = "org.kde.StatusNotifierItem"
PROPS = "org.freedesktop.DBus.Properties"
MENU_IFACE = "com.canonical.dbusmenu"

_main_loop_installed = False


def install_main_loop() -> None:
    """Route dbus-python through GLib, which Qt's event dispatcher also runs on Linux."""
    global _main_loop_installed
    if not _main_loop_installed:
        from dbus.mainloop.glib import DBusGMainLoop

        DBusGMainLoop(set_as_default=True)
        _main_loop_installed = True


def _ignore(*_args: Any) -> None:
    pass


def _log_error(what: str) -> Callable[[Exception], None]:
    return lambda exc: log.debug("%s failed: %s", what, exc)


def _pixmap_icon(pixmaps: Any) -> QIcon | None:
    """IconPixmap is a(iiay) of ARGB32 in network byte order."""
    icon = QIcon()
    for entry in pixmaps or []:
        try:
            width, height, data = int(entry[0]), int(entry[1]), bytes(bytearray(entry[2]))
        except (TypeError, ValueError, IndexError):
            continue
        if width <= 0 or height <= 0 or len(data) < width * height * 4:
            continue
        data = data[: width * height * 4]
        bgra = bytearray(len(data))
        bgra[0::4], bgra[1::4], bgra[2::4], bgra[3::4] = data[3::4], data[2::4], data[1::4], data[0::4]
        image = QImage(bytes(bgra), width, height, width * 4, QImage.Format.Format_ARGB32).copy()
        icon.addPixmap(QPixmap.fromImage(image))
    return None if icon.isNull() else icon


def _named_icon(name: str, theme_path: str) -> QIcon | None:
    if not name:
        return None
    if os.path.isabs(name):
        return QIcon(name) if os.path.exists(name) else None
    if theme_path:
        base = Path(theme_path)
        for ext in ("png", "svg", "svgz", "xpm"):
            direct = base / f"{name}.{ext}"
            if direct.is_file():
                return QIcon(str(direct))
        for found in sorted(base.rglob(f"{name}.*"), key=lambda p: -len(p.parts)):
            if found.suffix in (".png", ".svg", ".svgz", ".xpm"):
                return QIcon(str(found))
    icon = QIcon.fromTheme(name)
    return None if icon.isNull() else icon


class TrayItem:
    def __init__(self, host: TrayHost, key: str, service: str, path: str) -> None:
        self.host = host
        self.key = key
        self.service = service
        self.path = path
        self.props: dict[str, Any] = {}
        self.icon: QIcon | None = None
        self.title = ""
        self.status = "Active"
        self.menu_path = ""
        self.item_is_menu = False
        self._receivers: list[Any] = []
        self._refresh_pending = False
        self.proxy = host.bus.get_object(service, path, introspect=False)
        self._receivers.append(
            host.bus.add_signal_receiver(self._signal, dbus_interface=ITEM_IFACE, bus_name=service, path=path, member_keyword="member")
        )
        self.refresh()

    def _signal(self, *_args: Any, member: str = "") -> None:
        if member.startswith("New"):
            self.refresh()

    def refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self.proxy.GetAll(ITEM_IFACE, dbus_interface=PROPS, reply_handler=self._got_props, error_handler=self._props_failed)

    def _props_failed(self, exc: Exception) -> None:
        self._refresh_pending = False
        log.debug("Tray item %s: %s", self.key, exc)

    def _got_props(self, props: Any) -> None:
        self._refresh_pending = False
        self.props = dict(props)
        self.status = str(self.props.get("Status", "Active"))
        self.menu_path = str(self.props.get("Menu", "") or "")
        self.item_is_menu = bool(self.props.get("ItemIsMenu", False))
        tooltip = self.props.get("ToolTip")
        tip_title = ""
        if tooltip is not None and len(tooltip) >= 3:
            tip_title = str(tooltip[2])
        self.title = tip_title or str(self.props.get("Title", "") or self.props.get("Id", ""))
        theme_path = str(self.props.get("IconThemePath", "") or "")
        attention = self.status == "NeedsAttention"
        icon = None
        if attention:
            icon = _named_icon(str(self.props.get("AttentionIconName", "") or ""), theme_path) or _pixmap_icon(self.props.get("AttentionIconPixmap"))
        icon = icon or _named_icon(str(self.props.get("IconName", "") or ""), theme_path) or _pixmap_icon(self.props.get("IconPixmap"))
        self.icon = icon or QIcon.fromTheme("application-x-executable")
        self.host.item_changed()

    @property
    def visible(self) -> bool:
        return self.status != "Passive"

    # --- actions ------------------------------------------------------------------------------------
    def _call(self, method: str, *args: Any) -> None:
        getattr(self.proxy, method)(*args, dbus_interface=ITEM_IFACE, reply_handler=_ignore, error_handler=_log_error(f"{self.key}.{method}"))

    def provide_activation_token(self, token: str) -> None:
        self._call("ProvideXdgActivationToken", token)

    def activate(self, x: int = 0, y: int = 0) -> None:
        self._call("Activate", dbus.Int32(x), dbus.Int32(y))

    def secondary_activate(self, x: int = 0, y: int = 0) -> None:
        self._call("SecondaryActivate", dbus.Int32(x), dbus.Int32(y))

    def context_menu(self, x: int = 0, y: int = 0) -> None:
        self._call("ContextMenu", dbus.Int32(x), dbus.Int32(y))

    def scroll(self, delta: int, orientation: str = "vertical") -> None:
        self._call("Scroll", dbus.Int32(delta), orientation)

    # --- dbusmenu ----------------------------------------------------------------------------------------
    def fetch_menu(self, callback: Callable[[list[dict[str, Any]] | None], None]) -> None:
        if not self.menu_path:
            callback(None)
            return
        menu = self.host.bus.get_object(self.service, self.menu_path, introspect=False)

        def got_layout(_revision: Any, layout: Any) -> None:
            callback(_parse_layout(layout)["children"])

        def about_to_show_done(*_args: Any) -> None:
            menu.GetLayout(
                dbus.Int32(0),
                dbus.Int32(-1),
                dbus.Array([], signature="s"),
                dbus_interface=MENU_IFACE,
                reply_handler=got_layout,
                error_handler=lambda exc: (log.debug("GetLayout failed: %s", exc), callback(None)),
            )

        menu.AboutToShow(dbus.Int32(0), dbus_interface=MENU_IFACE, reply_handler=about_to_show_done, error_handler=lambda _e: about_to_show_done())

    def menu_event(self, item_id: int, event: str = "clicked") -> None:
        menu = self.host.bus.get_object(self.service, self.menu_path, introspect=False)
        menu.Event(
            dbus.Int32(item_id),
            event,
            dbus.Int32(0, variant_level=1),
            dbus.UInt32(0),
            dbus_interface=MENU_IFACE,
            reply_handler=_ignore,
            error_handler=_log_error("dbusmenu Event"),
        )

    def menu_about_to_show(self, item_id: int) -> None:
        menu = self.host.bus.get_object(self.service, self.menu_path, introspect=False)
        menu.AboutToShow(dbus.Int32(item_id), dbus_interface=MENU_IFACE, reply_handler=_ignore, error_handler=_ignore)

    def close(self) -> None:
        for receiver in self._receivers:
            receiver.remove()
        self._receivers.clear()


def _parse_layout(node: Any) -> dict[str, Any]:
    item_id, props, children = node[0], dict(node[1]), node[2]
    return {
        "id": int(item_id),
        "label": str(props.get("label", "")),
        "enabled": bool(props.get("enabled", True)),
        "visible": bool(props.get("visible", True)),
        "type": str(props.get("type", "standard")),
        "toggle_type": str(props.get("toggle-type", "")),
        "toggle_state": int(props.get("toggle-state", -1)),
        "icon_name": str(props.get("icon-name", "")),
        "submenu": str(props.get("children-display", "")) == "submenu" or bool(children),
        "children": [_parse_layout(child) for child in children],
    }


class TrayHost(QObject):
    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        install_main_loop()
        self.bus = dbus.SessionBus()
        self.items: dict[str, TrayItem] = {}
        self.order: list[str] = []
        self.host_name = f"org.kde.StatusNotifierHost-{os.getpid()}-retrobar"
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.setInterval(30)
        self._emit_timer.timeout.connect(self.changed.emit)
        try:
            self.bus.request_name(self.host_name, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
        except dbus.DBusException as exc:
            log.warning("Could not own %s: %s", self.host_name, exc)
        self.bus.add_signal_receiver(self._registered, "StatusNotifierItemRegistered", WATCHER, path=WATCHER_PATH)
        self.bus.add_signal_receiver(self._unregistered, "StatusNotifierItemUnregistered", WATCHER, path=WATCHER_PATH)
        self.bus.watch_name_owner(WATCHER, self._watcher_owner)

    def _watcher_owner(self, owner: str) -> None:
        if not owner:
            log.info("No StatusNotifierWatcher on the session bus; tray is empty")
            return
        watcher = self.bus.get_object(WATCHER, WATCHER_PATH, introspect=False)
        watcher.RegisterStatusNotifierHost(self.host_name, dbus_interface=WATCHER, reply_handler=_ignore, error_handler=_log_error("RegisterStatusNotifierHost"))
        watcher.Get(WATCHER, "RegisteredStatusNotifierItems", dbus_interface=PROPS, reply_handler=self._initial_items, error_handler=_log_error("RegisteredStatusNotifierItems"))

    def _initial_items(self, items: Any) -> None:
        for key in items:
            self._registered(str(key))

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        if "/" in key:
            index = key.index("/")
            return key[:index], key[index:]
        return key, "/StatusNotifierItem"

    def _registered(self, key: str) -> None:
        key = str(key)
        if key in self.items:
            return
        service, path = self._split(key)
        try:
            self.items[key] = TrayItem(self, key, service, path)
        except dbus.DBusException as exc:
            log.debug("Ignoring tray item %s: %s", key, exc)
            return
        self.order.append(key)

    def _unregistered(self, key: str) -> None:
        item = self.items.pop(str(key), None)
        if item is not None:
            item.close()
            self.order.remove(str(key))
            self.item_changed()

    def item_changed(self) -> None:
        self._emit_timer.start()

    def visible_items(self) -> list[TrayItem]:
        return [self.items[key] for key in self.order if key in self.items and self.items[key].icon is not None and self.items[key].visible]
