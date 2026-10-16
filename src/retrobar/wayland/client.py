"""A pywayland connection driven by the Qt event loop, plus shm-backed surfaces painted with QPainter."""

from __future__ import annotations

import ctypes
import dataclasses
import logging
import math
import mmap
import os
import time
from collections.abc import Callable
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QAbstractEventDispatcher, QObject, QRectF, QSocketNotifier, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPainter
from pywayland.client import Display
from pywayland.protocol.cursor_shape_v1 import WpCursorShapeManagerV1
from pywayland.protocol.fractional_scale_v1 import WpFractionalScaleManagerV1
from pywayland.protocol.viewporter import WpViewporter
from pywayland.protocol.wayland import WlCompositor, WlKeyboard, WlOutput, WlPointer, WlSeat, WlShm
from pywayland.protocol.xdg_activation_v1 import XdgActivationV1
from pywayland.protocol.xdg_shell import XdgWmBase

from ..protocol.wlr_layer_shell_unstable_v1 import ZwlrLayerShellV1
from .keyboard import KeyEvent, Xkb

log = logging.getLogger(__name__)

# enter/leave may name a surface we destroyed a moment ago; libwayland passes NULL for it, which
# pywayland rejects unless the argument is declared nullable.
for _interface in (WlKeyboard, WlPointer):
    for _event in _interface.events:
        if _event.name in ("enter", "leave"):
            _args = list(_event.arguments)
            _args[1] = dataclasses.replace(_args[1], nullable=True)
            _event.arguments = tuple(_args)

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BUTTONS = {BTN_LEFT: 1, BTN_MIDDLE: 2, BTN_RIGHT: 3}

# zwlr_layer_surface_v1.anchor
ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT = 1, 2, 4, 8
LAYER_TOP = 2
# zwlr_layer_surface_v1.keyboard_interactivity
KEYBOARD_NONE, KEYBOARD_EXCLUSIVE, KEYBOARD_ON_DEMAND = 0, 1, 2

# xdg_positioner
POS_ANCHOR = {"none": 0, "top": 1, "bottom": 2, "left": 3, "right": 4, "top_left": 5, "bottom_left": 6, "top_right": 7, "bottom_right": 8}
SLIDE_X, SLIDE_Y, FLIP_X, FLIP_Y, RESIZE_X, RESIZE_Y = 1, 2, 4, 8, 16, 32


class Output:
    def __init__(self, global_name: int, proxy: Any) -> None:
        self.global_name = global_name
        self.proxy = proxy
        self.name = ""
        self.description = ""
        self.scale = 1
        self.x = self.y = 0
        self.width = self.height = 0
        self.done = False
        proxy.dispatcher["geometry"] = self._geometry
        proxy.dispatcher["mode"] = self._mode
        proxy.dispatcher["scale"] = self._scale
        proxy.dispatcher["name"] = self._name
        proxy.dispatcher["description"] = self._description
        proxy.dispatcher["done"] = self._done
        self.on_done: Callable[[Output], None] = lambda output: None

    def _geometry(self, _p: Any, x: int, y: int, *_rest: Any) -> None:
        self.x, self.y = x, y

    def _mode(self, _p: Any, flags: int, width: int, height: int, _refresh: int) -> None:
        if flags & 1:  # current
            self.width, self.height = width, height

    def _scale(self, _p: Any, factor: int) -> None:
        self.scale = factor

    def _name(self, _p: Any, name: str) -> None:
        self.name = name

    def _description(self, _p: Any, description: str) -> None:
        self.description = description

    def _done(self, _p: Any) -> None:
        self.done = True
        self.on_done(self)

    def __repr__(self) -> str:
        return f"<Output {self.name or self.global_name} {self.width}x{self.height}@{self.scale}>"


class WaylandClient(QObject):
    outputs_changed = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.display = Display()
        self.display.connect()
        self.compositor: Any = None
        self.shm: Any = None
        self.seat: Any = None
        self.pointer: Any = None
        self.wm_base: Any = None
        self.wm_base_version = 0
        self.layer_shell: Any = None
        self.viewporter: Any = None
        self.fractional: Any = None
        self.cursor_shape: Any = None
        self.cursor_device: Any = None
        self.activation: Any = None
        self._tokens: set[Any] = set()
        self.outputs: dict[int, Output] = {}
        self.available: dict[str, tuple[int, int]] = {}
        self._surfaces: dict[Any, ShmSurface] = {}
        self._pointer_focus: ShmSurface | None = None
        self._pointer_pos = (0.0, 0.0)
        self.keyboard: Any = None
        self.xkb: Xkb | None = None
        self._keyboard_focus: ShmSurface | None = None
        self._repeat_rate, self._repeat_delay = 25, 600
        self._repeat_key: int | None = None
        self._repeat_timer = QTimer(self)
        self._repeat_timer.timeout.connect(self._repeat)
        self.last_serial = 0
        self.last_press_serial = 0
        self.registry = self.display.get_registry()
        self.registry.dispatcher["global"] = self._global
        self.registry.dispatcher["global_remove"] = self._global_remove
        self.display.roundtrip()
        self.display.roundtrip()
        missing = [name for name, value in (("wl_compositor", self.compositor), ("wl_shm", self.shm), ("zwlr_layer_shell_v1", self.layer_shell), ("xdg_wm_base", self.wm_base)) if value is None]
        if missing:
            raise RuntimeError(f"Compositor lacks required Wayland globals: {', '.join(missing)}")
        self._notifier = QSocketNotifier(self.display.get_fd(), QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._readable)
        dispatcher = QAbstractEventDispatcher.instance()
        if dispatcher is not None:
            dispatcher.aboutToBlock.connect(self.flush)

    # --- registry -------------------------------------------------------------------------------
    def _global(self, registry: Any, name: int, interface: str, version: int) -> None:
        self.available[interface] = (name, version)
        match interface:
            case "wl_compositor":
                self.compositor = registry.bind(name, WlCompositor, min(version, 6))
            case "wl_shm":
                self.shm = registry.bind(name, WlShm, 1)
            case "wl_seat" if self.seat is None:
                self.seat = registry.bind(name, WlSeat, min(version, 7))
                self.seat.dispatcher["capabilities"] = self._seat_capabilities
            case "wl_output":
                output = Output(name, registry.bind(name, WlOutput, min(version, 4)))
                output.on_done = lambda _o: self.outputs_changed.emit()
                self.outputs[name] = output
            case "xdg_wm_base":
                self.wm_base_version = min(version, 6)
                self.wm_base = registry.bind(name, XdgWmBase, self.wm_base_version)
                self.wm_base.dispatcher["ping"] = lambda wm, serial: wm.pong(serial)
            case "zwlr_layer_shell_v1":
                self.layer_shell = registry.bind(name, ZwlrLayerShellV1, min(version, 4))
            case "wp_viewporter":
                self.viewporter = registry.bind(name, WpViewporter, 1)
            case "wp_fractional_scale_manager_v1":
                self.fractional = registry.bind(name, WpFractionalScaleManagerV1, 1)
            case "wp_cursor_shape_manager_v1":
                self.cursor_shape = registry.bind(name, WpCursorShapeManagerV1, 1)
            case "xdg_activation_v1":
                self.activation = registry.bind(name, XdgActivationV1, 1)

    def _global_remove(self, _registry: Any, name: int) -> None:
        output = self.outputs.pop(name, None)
        if output is not None:
            self.outputs_changed.emit()

    def request_activation_token(self, surface: ShmSurface | None, callback: Callable[[str], None]) -> None:
        """Get an xdg-activation token so the program we hand it to may take focus."""
        if self.activation is None:
            callback("")
            return
        token = self.activation.get_activation_token()
        self._tokens.add(token)
        if self.seat is not None and self.last_serial:
            token.set_serial(self.last_serial, self.seat)
        if surface is not None:
            token.set_surface(surface.surface)

        def done(proxy: Any, value: str) -> None:
            self._tokens.discard(proxy)
            proxy.destroy()
            callback(value)

        token.dispatcher["done"] = done
        token.commit()
        self.flush()

    def bind(self, interface_name: str, interface: Any, max_version: int) -> Any:
        found = self.available.get(interface_name)
        if found is None:
            return None
        name, version = found
        return self.registry.bind(name, interface, min(version, max_version))

    # --- event loop -------------------------------------------------------------------------------
    def _readable(self) -> None:
        try:
            self.display.read()
            self.display.dispatch(block=False)
        except RuntimeError as exc:
            log.error("Wayland connection lost: %s", exc)
            self._notifier.setEnabled(False)
            self.disconnected.emit()
            return
        self.flush()

    def flush(self) -> None:
        if not self._notifier.isEnabled():
            return
        try:
            self.display.dispatch(block=False)
        except RuntimeError as exc:
            log.error("Wayland dispatch failed: %s", exc)
            self._notifier.setEnabled(False)
            self.disconnected.emit()
            return
        self.display.flush()

    def roundtrip(self) -> None:
        self.display.roundtrip()

    # --- input ------------------------------------------------------------------------------------
    def _seat_capabilities(self, seat: Any, caps: int) -> None:
        if caps & WlSeat.capability.pointer and self.pointer is None:
            self.pointer = seat.get_pointer()
            self.pointer.dispatcher["enter"] = self._pointer_enter
            self.pointer.dispatcher["leave"] = self._pointer_leave
            self.pointer.dispatcher["motion"] = self._pointer_motion
            self.pointer.dispatcher["button"] = self._pointer_button
            self.pointer.dispatcher["axis"] = self._pointer_axis
            if self.cursor_shape is not None:
                self.cursor_device = self.cursor_shape.get_pointer(self.pointer)
        if caps & WlSeat.capability.keyboard and self.keyboard is None:
            try:
                self.xkb = Xkb()
            except RuntimeError as exc:
                log.warning("Keyboard input disabled: %s", exc)
                return
            self.keyboard = seat.get_keyboard()
            self.keyboard.dispatcher["keymap"] = self._keymap
            self.keyboard.dispatcher["enter"] = self._keyboard_enter
            self.keyboard.dispatcher["leave"] = self._keyboard_leave
            self.keyboard.dispatcher["key"] = self._key
            self.keyboard.dispatcher["modifiers"] = self._modifiers
            self.keyboard.dispatcher["repeat_info"] = self._repeat_info

    def register_surface(self, surface: Any, owner: ShmSurface) -> None:
        self._surfaces[surface] = owner

    def unregister_surface(self, surface: Any) -> None:
        owner = self._surfaces.pop(surface, None)
        if owner is not None and owner is self._pointer_focus:
            self._pointer_focus = None
        if owner is not None and owner is self._keyboard_focus:
            self._keyboard_focus = None
            self._stop_repeat()

    # --- keyboard -----------------------------------------------------------------------------------
    def _keymap(self, _kb: Any, fmt: int, fd: int, size: int) -> None:
        try:
            if fmt != 1 or self.xkb is None:  # 1 = xkb_v1
                return
            with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as mm:
                text = mm[:size].rstrip(b"\0")
            if not self.xkb.set_keymap(text):
                log.warning("Could not compile the compositor's keymap")
        finally:
            os.close(fd)

    def _keyboard_enter(self, _kb: Any, serial: int, surface: Any, _keys: Any) -> None:
        self.last_serial = serial
        self._keyboard_focus = self._surfaces.get(surface)
        if self._keyboard_focus is not None:
            self._keyboard_focus.keyboard_enter()

    def _keyboard_leave(self, _kb: Any, serial: int, _surface: Any) -> None:
        self.last_serial = serial
        self._stop_repeat()
        focus, self._keyboard_focus = self._keyboard_focus, None
        if focus is not None:
            focus.keyboard_leave()

    def _key(self, _kb: Any, serial: int, _time: int, key: int, state: int) -> None:
        self.last_serial = serial
        if self.xkb is None or not self.xkb.ready:
            return
        if not state:
            if key == self._repeat_key:
                self._stop_repeat()
            return
        self.last_press_serial = serial
        self._stop_repeat()
        if self._repeat_rate > 0 and self.xkb.repeats(key):
            self._repeat_key = key
            self._repeat_timer.start(self._repeat_delay)
        self._deliver(self.xkb.translate(key, serial))

    def _deliver(self, event: KeyEvent) -> None:
        focus = self._keyboard_focus
        if focus is not None:
            focus.key_press(event)
            self.flush()

    def _repeat(self) -> None:
        if self._repeat_key is None or self.xkb is None:
            self._stop_repeat()
            return
        self._repeat_timer.setInterval(max(1, 1000 // self._repeat_rate))
        event = self.xkb.translate(self._repeat_key, self.last_press_serial)
        event.repeat = True
        self._deliver(event)

    def _stop_repeat(self) -> None:
        self._repeat_key = None
        self._repeat_timer.stop()

    def _modifiers(self, _kb: Any, serial: int, depressed: int, latched: int, locked: int, group: int) -> None:
        self.last_serial = serial
        if self.xkb is not None:
            self.xkb.update_modifiers(depressed, latched, locked, group)

    def _repeat_info(self, _kb: Any, rate: int, delay: int) -> None:
        self._repeat_rate, self._repeat_delay = max(0, rate), max(1, delay)

    def _pointer_enter(self, _p: Any, serial: int, surface: Any, x: float, y: float) -> None:
        self.last_serial = serial
        if self.cursor_device is not None:
            self.cursor_device.set_shape(serial, 1)  # default arrow
        self._pointer_focus = self._surfaces.get(surface)
        self._pointer_pos = (x, y)
        if self._pointer_focus is not None:
            self._pointer_focus.pointer_enter(x, y)

    def _pointer_leave(self, _p: Any, serial: int, _surface: Any) -> None:
        self.last_serial = serial
        focus, self._pointer_focus = self._pointer_focus, None
        if focus is not None:
            focus.pointer_leave()

    def _pointer_motion(self, _p: Any, _time: int, x: float, y: float) -> None:
        self._pointer_pos = (x, y)
        if self._pointer_focus is not None:
            self._pointer_focus.pointer_motion(x, y)

    def _pointer_button(self, _p: Any, serial: int, _time: int, button: int, state: int) -> None:
        self.last_serial = serial
        if state:
            self.last_press_serial = serial
        if self._pointer_focus is not None and button in BUTTONS:
            x, y = self._pointer_pos
            self._pointer_focus.pointer_button(BUTTONS[button], bool(state), x, y)

    def _pointer_axis(self, _p: Any, _time: int, axis: int, value: float) -> None:
        if self._pointer_focus is not None and axis == 0:
            x, y = self._pointer_pos
            self._pointer_focus.pointer_axis(value, x, y)


class ShmBuffer:
    def __init__(self, client: WaylandClient, width: int, height: int) -> None:
        self.width, self.height = width, height
        stride = width * 4
        size = stride * height
        fd = os.memfd_create("retrobar-buffer", os.MFD_CLOEXEC)
        os.ftruncate(fd, size)
        self._mm = mmap.mmap(fd, size)
        pool = client.shm.create_pool(fd, size)
        self.wl = pool.create_buffer(0, width, height, stride, WlShm.format.argb8888)
        pool.destroy()
        os.close(fd)
        self._anchor = ctypes.c_char.from_buffer(self._mm)
        self.image = QImage(sip.voidptr(ctypes.addressof(self._anchor)), width, height, stride, QImage.Format.Format_ARGB32_Premultiplied)
        self.busy = False
        self.wl.dispatcher["release"] = self._release

    def _release(self, _buffer: Any) -> None:
        self.busy = False

    def destroy(self) -> None:
        self.wl.destroy()
        self.image = QImage()
        del self._anchor
        self._mm.close()


class ShmSurface:
    """A wl_surface whose content is painted with QPainter into shm buffers."""

    MIN_FRAME_INTERVAL = 0.012

    def __init__(self, client: WaylandClient) -> None:
        self.client = client
        self.surface = client.compositor.create_surface()
        client.register_surface(self.surface, self)
        self.width = self.height = 0
        self.configured = False
        self.int_scale = 1
        self.scale120 = 0
        self._buffers: list[ShmBuffer] = []
        self._dirty = False
        self._scheduled = False
        self._last_draw = 0.0
        self._destroyed = False
        self.viewport = client.viewporter.get_viewport(self.surface) if client.viewporter else None
        self.fractional = None
        if client.fractional is not None and self.viewport is not None:
            self.fractional = client.fractional.get_fractional_scale(self.surface)
            self.fractional.dispatcher["preferred_scale"] = self._preferred_scale
        self.surface.dispatcher["enter"] = self._enter_output
        self.surface.dispatcher["preferred_buffer_scale"] = self._preferred_buffer_scale

    # --- scale ------------------------------------------------------------------------------------
    @property
    def scale(self) -> float:
        if self.scale120:
            return self.scale120 / 120
        return float(self.int_scale)

    def _preferred_scale(self, _p: Any, scale: int) -> None:
        if scale != self.scale120:
            self.scale120 = scale
            self.scale_changed()
            self.request_redraw()

    def _preferred_buffer_scale(self, _p: Any, factor: int) -> None:
        if factor != self.int_scale:
            self.int_scale = factor
            if not self.scale120:
                self.scale_changed()
                self.request_redraw()

    def _enter_output(self, _p: Any, output: Any) -> None:
        for out in self.client.outputs.values():
            if out.proxy is output and out.scale != self.int_scale:
                self._preferred_buffer_scale(None, out.scale)

    def scale_changed(self) -> None:
        """Hook for subclasses."""

    # --- drawing ----------------------------------------------------------------------------------
    def request_redraw(self) -> None:
        self._dirty = True
        if self._scheduled or self._destroyed:
            return
        self._scheduled = True
        delay = max(0.0, self.MIN_FRAME_INTERVAL - (time.monotonic() - self._last_draw))
        QTimer.singleShot(int(delay * 1000), self._draw)

    def _buffer(self, width: int, height: int) -> ShmBuffer | None:
        for buf in list(self._buffers):
            if (buf.width, buf.height) != (width, height) and not buf.busy:
                buf.destroy()
                self._buffers.remove(buf)
        for buf in self._buffers:
            if not buf.busy:
                return buf
        if len(self._buffers) >= 3:
            return None
        buf = ShmBuffer(self.client, width, height)
        self._buffers.append(buf)
        return buf

    def _draw(self) -> None:
        self._scheduled = False
        if self._destroyed or not self._dirty or not self.configured or self.width <= 0 or self.height <= 0:
            return
        scale = self.scale
        bw, bh = max(1, math.ceil(self.width * scale - 0.01)), max(1, math.ceil(self.height * scale - 0.01))
        buf = self._buffer(bw, bh)
        if buf is None:
            QTimer.singleShot(8, self.request_redraw)
            return
        self._dirty = False
        self._last_draw = time.monotonic()
        image = buf.image
        image.setDevicePixelRatio(scale)
        image.fill(0)
        p = QPainter(image)
        try:
            self.paint(p, QRectF(0, 0, self.width, self.height))
        except Exception:
            log.exception("Painting failed")
        finally:
            p.end()
        buf.busy = True
        self.surface.attach(buf.wl, 0, 0)
        if self.viewport is not None:
            self.surface.set_buffer_scale(1)
            self.viewport.set_destination(self.width, self.height)
        else:
            self.surface.set_buffer_scale(int(scale))
        self.surface.damage_buffer(0, 0, bw, bh)
        self.surface.commit()
        self.client.flush()

    def paint(self, p: QPainter, rect: QRectF) -> None:
        """Paint the surface content in logical coordinates."""

    # --- input hooks --------------------------------------------------------------------------------
    def pointer_enter(self, x: float, y: float) -> None: ...
    def pointer_leave(self) -> None: ...
    def pointer_motion(self, x: float, y: float) -> None: ...
    def pointer_button(self, button: int, pressed: bool, x: float, y: float) -> None: ...
    def pointer_axis(self, value: float, x: float, y: float) -> None: ...
    def keyboard_enter(self) -> None: ...
    def keyboard_leave(self) -> None: ...
    def key_press(self, event: KeyEvent) -> None: ...

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self.client.unregister_surface(self.surface)
        if self.fractional is not None:
            self.fractional.destroy()
        if self.viewport is not None:
            self.viewport.destroy()
        self.surface.destroy()
        for buf in self._buffers:
            buf.destroy()
        self._buffers.clear()
        self.client.flush()


class LayerSurface(ShmSurface):
    def __init__(self, client: WaylandClient, output: Output | None, namespace: str = "retrobar", layer: int = LAYER_TOP) -> None:
        super().__init__(client)
        self.output = output
        self.layer_surface = client.layer_shell.get_layer_surface(self.surface, output.proxy if output else None, layer, namespace)
        self.layer_surface.dispatcher["configure"] = self._configure
        self.layer_surface.dispatcher["closed"] = lambda _ls: self.closed()
        self._initial_output_scale = output.scale if output else 1
        self.int_scale = self._initial_output_scale
        self.keyboard_mode = KEYBOARD_NONE

    def setup(self, anchor: int, width: int, height: int, exclusive: int, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> None:
        ls = self.layer_surface
        ls.set_anchor(anchor)
        ls.set_size(width, height)
        ls.set_exclusive_zone(exclusive)
        ls.set_margin(*margins)
        ls.set_keyboard_interactivity(self.keyboard_mode)
        self.surface.commit()
        self.client.flush()

    def set_keyboard_focus(self, mode: int) -> None:
        """KEYBOARD_ON_DEMAND while a popup that takes typing is open, KEYBOARD_NONE otherwise."""
        if mode == self.keyboard_mode or self._destroyed:
            return
        self.keyboard_mode = mode
        self.layer_surface.set_keyboard_interactivity(mode)
        self.surface.commit()
        self.client.flush()

    def _configure(self, ls: Any, serial: int, width: int, height: int) -> None:
        ls.ack_configure(serial)
        changed = (width, height) != (self.width, self.height)
        self.width, self.height = width, height
        self.configured = True
        if changed:
            self.resized()
        self.request_redraw()

    def resized(self) -> None:
        """Hook for subclasses."""

    def closed(self) -> None:
        """The compositor removed this surface (e.g. its output went away)."""

    def destroy(self) -> None:
        if not self._destroyed:
            self.layer_surface.destroy()
        super().destroy()


class PopupSurface(ShmSurface):
    """An xdg_popup attached to a layer surface or another popup (menus, tooltips, calendars)."""

    def __init__(
        self,
        client: WaylandClient,
        parent: LayerSurface | PopupSurface,
        anchor_rect: QRectF,
        width: int,
        height: int,
        anchor: str,
        gravity: str,
        constraints: int = SLIDE_X | SLIDE_Y | FLIP_X | FLIP_Y,
        grab: bool = True,
    ) -> None:
        super().__init__(client)
        self.parent = parent
        self.width, self.height = width, height
        self.on_done: Callable[[], None] = lambda: None
        self.int_scale = parent.int_scale
        self.scale120 = parent.scale120
        self.xdg_surface = client.wm_base.get_xdg_surface(self.surface)
        self._placement = (anchor, gravity, constraints)
        self._reposition_token = 0
        positioner = self._positioner(anchor_rect, width, height)
        if isinstance(parent, PopupSurface):
            self.popup = self.xdg_surface.get_popup(parent.xdg_surface, positioner)
        else:
            self.popup = self.xdg_surface.get_popup(None, positioner)
            parent.layer_surface.get_popup(self.popup)
        positioner.destroy()
        self.xdg_surface.set_window_geometry(0, 0, max(1, width), max(1, height))
        self.xdg_surface.dispatcher["configure"] = self._configure
        self.popup.dispatcher["configure"] = self._popup_configure
        self.popup.dispatcher["popup_done"] = lambda _p: self.dismissed()
        if grab and client.seat is not None:
            self.popup.grab(client.seat, client.last_press_serial or client.last_serial)
        self.surface.commit()
        self.client.flush()

    def _positioner(self, anchor_rect: QRectF, width: int, height: int) -> Any:
        anchor, gravity, constraints = self._placement
        positioner = self.client.wm_base.create_positioner()
        positioner.set_size(max(1, width), max(1, height))
        positioner.set_anchor_rect(
            int(math.floor(anchor_rect.x())), int(math.floor(anchor_rect.y())), max(1, int(round(anchor_rect.width()))), max(1, int(round(anchor_rect.height())))
        )
        positioner.set_anchor(POS_ANCHOR[anchor])
        positioner.set_gravity(POS_ANCHOR[gravity])
        positioner.set_constraint_adjustment(constraints)
        return positioner

    def reposition(self, anchor_rect: QRectF, width: int, height: int) -> bool:
        """Resize and re-place an open popup (xdg_wm_base v3+); False if the compositor cannot."""
        if self._destroyed or self.client.wm_base_version < 3:
            return False
        positioner = self._positioner(anchor_rect, width, height)
        self._reposition_token += 1
        self.popup.reposition(positioner, self._reposition_token)
        positioner.destroy()
        self.width, self.height = width, height
        self.xdg_surface.set_window_geometry(0, 0, max(1, width), max(1, height))
        self.client.flush()
        return True

    def _popup_configure(self, _p: Any, _x: int, _y: int, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self.width, self.height = width, height

    def _configure(self, xdg_surface: Any, serial: int) -> None:
        xdg_surface.ack_configure(serial)
        self.configured = True
        self.request_redraw()

    def dismissed(self) -> None:
        self.destroy()
        self.on_done()

    def destroy(self) -> None:
        if not self._destroyed:
            self.popup.destroy()
            self.xdg_surface.destroy()
        super().destroy()
