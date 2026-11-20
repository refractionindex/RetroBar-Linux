"""Build a labelled contact sheet from theme preview PNGs."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402


def main() -> int:
    default_folder = Path(tempfile.gettempdir()) / "retrobar-themes"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", type=Path, default=default_folder,
                        help="directory of theme PNGs (default: %(default)s)")
    parser.add_argument("-o", "--out", type=Path, default=None,
                        help="output path (default: <folder>/_sheet.png)")
    parser.add_argument("--zoom", type=float, default=1.0, help="scale factor for each preview")
    args = parser.parse_args()
    out = args.out or (args.folder / "_sheet.png")

    _app = QGuiApplication(sys.argv)
    images = [
        (p.stem, QImage(str(p)))
        for p in sorted(args.folder.glob("*.png"))
        if not p.stem.startswith("_") and not p.stem.startswith("zoom")
    ]
    if not images:
        print(f"no PNGs found in {args.folder}", file=sys.stderr)
        return 1
    label_h = 14
    width = int(max(i.width() for _, i in images) * args.zoom)
    height = sum(int(i.height() * args.zoom) + label_h for _, i in images)
    sheet = QImage(width, height, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#3a6ea5"))
    p = QPainter(sheet)
    y = 0
    for name, image in images:
        p.setPen(QColor("white"))
        p.drawText(2, y + 11, name)
        y += label_h
        scaled = image.scaled(
            int(image.width() * args.zoom),
            int(image.height() * args.zoom),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        p.drawImage(0, y, scaled)
        y += scaled.height()
    p.end()
    sheet.save(str(out))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
