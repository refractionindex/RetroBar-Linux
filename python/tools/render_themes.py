"""Render a sample taskbar for each bundled theme into PNG files (offscreen; no compositor needed)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QIcon, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from retrobar.settings import Edge, Settings  # noqa: E402
from retrobar.theme import Theme, install_font_substitutes, install_icon_theme  # noqa: E402
from retrobar.ui import elements  # noqa: E402
from retrobar.ui.view import TaskbarView, TaskData, TrayData  # noqa: E402


def sample_tasks() -> list[TaskData]:
    names = [("Konsole", "utilities-terminal", "Active"), ("Dolphin - Home", "system-file-manager", "Normal"),
             ("Kate", "accessories-text-editor", "Flashing"), ("Firefox", "firefox", "Normal")]
    return [TaskData(key=i, Title=t, Icon=QIcon.fromTheme(icon), State=state) for i, (t, icon, state) in enumerate(names)]


def sample_tray() -> list[TrayData]:
    return [TrayData(key=i, Title=n, Icon=QIcon.fromTheme(n)) for i, n in enumerate(["audio-volume-high", "network-wireless", "battery-good"])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/retrobar-themes"))
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--edge", choices=[e.name.lower() for e in Edge], default="bottom")
    parser.add_argument("themes", nargs="*")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    install_icon_theme()
    install_font_substitutes()
    args.out.mkdir(parents=True, exist_ok=True)
    settings = Settings(args.out / "settings.json")
    settings.Edge = int(Edge[args.edge.upper()])
    settings.LockTaskbar = True
    theme = Theme()
    for name in args.themes or theme.themes():
        theme.load(name, "English")
        elements.set_theme(theme)
        view = TaskbarView(settings)
        view.set_tasks(sample_tasks())
        view.set_tray(sample_tray())
        thickness = view.thickness()
        w, h = (args.width, thickness) if view.horizontal else (thickness, 600)
        start = time.perf_counter()
        view.layout(w, h)
        image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        p = QPainter(image)
        view.render(p)
        p.end()
        elapsed = (time.perf_counter() - start) * 1000
        path = args.out / f"{name}.png"
        image.save(str(path))
        print(f"{name}: {w}x{h} in {elapsed:.0f} ms -> {path}")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
