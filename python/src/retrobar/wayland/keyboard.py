"""Keyboard translation with libxkbcommon (through ctypes): Wayland keycodes to keysyms and text."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Keysyms used by menus and the search box.
KEY_RETURN, KEY_KP_ENTER, KEY_ESCAPE, KEY_BACKSPACE, KEY_DELETE = 0xFF0D, 0xFF8D, 0xFF1B, 0xFF08, 0xFFFF
KEY_TAB, KEY_ISO_LEFT_TAB = 0xFF09, 0xFE20
KEY_HOME, KEY_LEFT, KEY_UP, KEY_RIGHT, KEY_DOWN, KEY_PAGE_UP, KEY_PAGE_DOWN, KEY_END = range(0xFF50, 0xFF58)
KEY_SPACE = 0x20

MOD_SHIFT, MOD_CTRL, MOD_ALT, MOD_SUPER = 1, 2, 4, 8
_MOD_NAMES = {MOD_SHIFT: b"Shift", MOD_CTRL: b"Control", MOD_ALT: b"Mod1", MOD_SUPER: b"Mod4"}
_STATE_MODS_EFFECTIVE = 1 << 3
_KEYMAP_FORMAT_TEXT_V1 = 1
_EVDEV_OFFSET = 8  # Wayland sends evdev codes; XKB keycodes are 8 higher.


@dataclass
class KeyEvent:
    sym: int
    text: str
    modifiers: int
    serial: int = 0
    repeat: bool = False

    @property
    def printable(self) -> bool:
        return bool(self.text) and self.text.isprintable() and not self.modifiers & (MOD_CTRL | MOD_ALT | MOD_SUPER)


class _RuleNames(ctypes.Structure):
    _fields_ = [(name, ctypes.c_char_p) for name in ("rules", "model", "layout", "variant", "options")]


def _load() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:
        log.warning("libxkbcommon unavailable, keyboard input disabled: %s", exc)
        return None
    vp, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        "xkb_context_new": (vp, [i32]),
        "xkb_context_unref": (None, [vp]),
        "xkb_keymap_new_from_string": (vp, [vp, ctypes.c_char_p, i32, i32]),
        "xkb_keymap_new_from_names": (vp, [vp, ctypes.POINTER(_RuleNames), i32]),
        "xkb_keymap_unref": (None, [vp]),
        "xkb_keymap_key_repeats": (i32, [vp, u32]),
        "xkb_state_new": (vp, [vp]),
        "xkb_state_unref": (None, [vp]),
        "xkb_state_update_mask": (i32, [vp, u32, u32, u32, u32, u32, u32]),
        "xkb_state_update_key": (i32, [vp, u32, i32]),
        "xkb_state_key_get_one_sym": (u32, [vp, u32]),
        "xkb_state_key_get_utf8": (i32, [vp, u32, ctypes.c_char_p, ctypes.c_size_t]),
        "xkb_state_mod_name_is_active": (i32, [vp, ctypes.c_char_p, i32]),
    }
    for fn, (restype, argtypes) in signatures.items():
        func = getattr(lib, fn)
        func.restype = restype
        func.argtypes = argtypes
    return lib


_lib: ctypes.CDLL | None | bool = False


def library() -> ctypes.CDLL | None:
    global _lib
    if _lib is False:
        _lib = _load()
    return _lib  # type: ignore[return-value]


class Xkb:
    """One keymap plus its modifier state."""

    def __init__(self) -> None:
        lib = library()
        if lib is None:
            raise RuntimeError("libxkbcommon is not available")
        self._lib = lib
        self._context = lib.xkb_context_new(0)
        if not self._context:
            raise RuntimeError("xkb_context_new failed")
        self._keymap: int | None = None
        self._state: int | None = None

    @property
    def ready(self) -> bool:
        return bool(self._state)

    def _replace(self, keymap: int | None) -> bool:
        if not keymap:
            return False
        state = self._lib.xkb_state_new(keymap)
        if not state:
            self._lib.xkb_keymap_unref(keymap)
            return False
        self._release()
        self._keymap, self._state = keymap, state
        return True

    def set_keymap(self, text: bytes) -> bool:
        return self._replace(self._lib.xkb_keymap_new_from_string(self._context, text, _KEYMAP_FORMAT_TEXT_V1, 0))

    def set_layout(self, layout: str = "us", variant: str = "", options: str = "") -> bool:
        names = _RuleNames(b"evdev", b"pc105", layout.encode(), variant.encode(), options.encode())
        return self._replace(self._lib.xkb_keymap_new_from_names(self._context, ctypes.byref(names), 0))

    def update_modifiers(self, depressed: int, latched: int, locked: int, group: int) -> None:
        if self._state:
            self._lib.xkb_state_update_mask(self._state, depressed, latched, locked, 0, 0, group)

    def press(self, evdev_code: int, down: bool) -> None:
        """Feed a key directly (tests; compositors send modifier masks instead)."""
        if self._state:
            self._lib.xkb_state_update_key(self._state, evdev_code + _EVDEV_OFFSET, 1 if down else 0)

    def modifiers(self) -> int:
        if not self._state:
            return 0
        mods = 0
        for bit, name in _MOD_NAMES.items():
            if self._lib.xkb_state_mod_name_is_active(self._state, name, _STATE_MODS_EFFECTIVE) > 0:
                mods |= bit
        return mods

    def translate(self, evdev_code: int, serial: int = 0) -> KeyEvent:
        if not self._state:
            return KeyEvent(0, "", 0, serial)
        code = evdev_code + _EVDEV_OFFSET
        sym = self._lib.xkb_state_key_get_one_sym(self._state, code)
        buf = ctypes.create_string_buffer(64)
        size = self._lib.xkb_state_key_get_utf8(self._state, code, buf, len(buf))
        text = buf.value[: max(0, size)].decode("utf-8", errors="replace") if size > 0 else ""
        return KeyEvent(sym, text, self.modifiers(), serial)

    def repeats(self, evdev_code: int) -> bool:
        return bool(self._keymap) and self._lib.xkb_keymap_key_repeats(self._keymap, evdev_code + _EVDEV_OFFSET) > 0

    def _release(self) -> None:
        if self._state:
            self._lib.xkb_state_unref(self._state)
        if self._keymap:
            self._lib.xkb_keymap_unref(self._keymap)
        self._state = self._keymap = None

    def close(self) -> None:
        self._release()
        if self._context:
            self._lib.xkb_context_unref(self._context)
            self._context = None
