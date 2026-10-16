"""Stack rendered theme PNGs (from render_themes.py) into one labelled image for review."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402


def main() -> int:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/retrobar-themes")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else folder / "_sheet.png")
    zoom = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    _app = QGuiApplication(sys.argv)
    images = [(p.stem, QImage(str(p))) for p in sorted(folder.glob("*.png")) if not p.stem.startswith("_") and p.stem != "zoom" and not p.stem.startswith("zoom")]
    label_h = 14
    width = int(max(i.width() for _, i in images) * zoom)
    height = sum(int(i.height() * zoom) + label_h for _, i in images)
    sheet = QImage(width, height, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#3a6ea5"))
    p = QPainter(sheet)
    y = 0
    for name, image in images:
        p.setPen(QColor("white"))
        p.drawText(2, y + 11, name)
        y += label_h
        scaled = image.scaled(int(image.width() * zoom), int(image.height() * zoom), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        p.drawImage(0, y, scaled)
        y += scaled.height()
    p.end()
    sheet.save(str(out))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
