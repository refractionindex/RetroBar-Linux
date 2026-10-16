"""Entry point: ``python -m retrobar`` or the ``retrobar`` script."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retrobar", description="Classic Windows taskbar for KDE Plasma on Wayland")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--replace", action="store_true", help="run even if another RetroBar instance owns the session bus name")
    parser.add_argument("--start-menu", action="store_true", help="toggle the Start menu of the running RetroBar (bind it to a shortcut)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.start_menu:
        return toggle_start_menu()
    if not os.environ.get("WAYLAND_DISPLAY"):
        logging.error("RetroBar needs a Wayland session (WAYLAND_DISPLAY is not set)")
        return 1
    # PyQt aborts on exceptions escaping slots unless a custom excepthook is installed.
    sys.excepthook = lambda kind, value, tb: logging.getLogger("retrobar").error("Unhandled exception", exc_info=(kind, value, tb))

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    from . import paths
    from .app import RetroBar, claim_single_instance

    app = QApplication(sys.argv[:1])
    app.setApplicationName("RetroBar")
    app.setDesktopFileName("retrobar")
    app.setWindowIcon(QIcon(str(paths.bundled_root() / "Resources" / "retrobar.ico")))
    app.setQuitOnLastWindowClosed(False)
    if not claim_single_instance() and not args.replace:
        logging.error("RetroBar is already running")
        return 1
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    # Let Python run signal handlers while Qt's loop is idle.
    from PyQt6.QtCore import QTimer

    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)
    try:
        retrobar = RetroBar(app)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    code = app.exec()
    retrobar.shutdown()
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    # pywayland frees proxies from garbage collection; at interpreter teardown that can run after the
    # display is gone and crash, so skip teardown once our surfaces are destroyed and flushed.
    os._exit(code)


def toggle_start_menu() -> int:
    import dbus

    from .app import BUS_NAME, BUS_PATH

    try:
        bar = dbus.SessionBus().get_object(BUS_NAME, BUS_PATH, introspect=False)
        bar.ToggleStartMenu(dbus_interface=BUS_NAME)
    except dbus.DBusException as exc:
        logging.error("RetroBar is not running (%s)", exc.get_dbus_message() or exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
