"""Conversions for XAML primitive values (Thickness, Color, Geometry, ...)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainterPath

_SEP = re.compile(r"[\s,]+")


def split_numbers(text: str) -> list[float]:
    return [float(part) for part in _SEP.split(text.strip()) if part]


@dataclass(frozen=True, slots=True)
class Thickness:
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0

    @property
    def horizontal(self) -> float:
        return self.left + self.right

    @property
    def vertical(self) -> float:
        return self.top + self.bottom

    def mirrored(self) -> Thickness:
        return Thickness(self.right, self.top, self.left, self.bottom)

    @staticmethod
    @lru_cache(maxsize=1024)
    def parse(text: str) -> Thickness:
        nums = split_numbers(text)
        if len(nums) == 1:
            return Thickness(nums[0], nums[0], nums[0], nums[0])
        if len(nums) == 2:
            return Thickness(nums[0], nums[1], nums[0], nums[1])
        if len(nums) == 4:
            return Thickness(*nums)
        raise ValueError(f"Invalid Thickness: {text!r}")


ZERO = Thickness()


@dataclass(frozen=True, slots=True)
class CornerRadius:
    top_left: float = 0.0
    top_right: float = 0.0
    bottom_right: float = 0.0
    bottom_left: float = 0.0

    def is_zero(self) -> bool:
        return not (self.top_left or self.top_right or self.bottom_right or self.bottom_left)

    def mirrored(self) -> CornerRadius:
        return CornerRadius(self.top_right, self.top_left, self.bottom_left, self.bottom_right)

    @staticmethod
    @lru_cache(maxsize=256)
    def parse(text: str) -> CornerRadius:
        nums = split_numbers(text)
        if len(nums) == 1:
            return CornerRadius(nums[0], nums[0], nums[0], nums[0])
        if len(nums) == 4:
            return CornerRadius(*nums)
        raise ValueError(f"Invalid CornerRadius: {text!r}")


def parse_point(text: str) -> QPointF:
    nums = split_numbers(text)
    return QPointF(nums[0], nums[1])


def parse_rect(text: str) -> QRectF:
    nums = split_numbers(text)
    return QRectF(nums[0], nums[1], nums[2], nums[3])


def parse_bool(text: str) -> bool:
    return str(text).strip().lower() == "true"


def parse_double(text: str) -> float:
    text = str(text).strip()
    if text.lower() in ("auto", "nan"):
        return math.nan
    if text.lower() == "infinity":
        return math.inf
    return float(text)


@lru_cache(maxsize=2048)
def parse_color(text: str) -> QColor:
    text = text.strip()
    if text.startswith("#"):
        hexpart = text[1:]
        if len(hexpart) in (3, 4):
            hexpart = "".join(c * 2 for c in hexpart)
        if len(hexpart) == 6:
            hexpart = "ff" + hexpart
        if len(hexpart) != 8:
            raise ValueError(f"Invalid color: {text!r}")
        value = int(hexpart, 16)
        return QColor((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF, (value >> 24) & 0xFF)
    if text.startswith("sc#"):
        parts = split_numbers(text[3:])
        if len(parts) == 3:
            parts = [1.0, *parts]
        a, r, g, b = parts
        return QColor.fromRgbF(r, g, b, a)
    color = QColor(text.lower())
    if not color.isValid():
        raise ValueError(f"Invalid color: {text!r}")
    return color


FONT_WEIGHTS = {
    "thin": 100,
    "extralight": 200,
    "ultralight": 200,
    "light": 300,
    "normal": 400,
    "regular": 400,
    "medium": 500,
    "demibold": 600,
    "semibold": 600,
    "bold": 700,
    "extrabold": 800,
    "ultrabold": 800,
    "black": 900,
    "heavy": 900,
}


def parse_font_weight(text: str) -> int:
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    return FONT_WEIGHTS.get(text.lower(), 400)


# --- Path markup (WPF "mini-language") ---------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


@lru_cache(maxsize=512)
def parse_geometry(text: str) -> QPainterPath:
    """Parse WPF path markup (M, L, H, V, C, Q, S, T, A, Z and F0/F1) into a QPainterPath."""
    tokens = _TOKEN.findall(text)
    path = QPainterPath()
    i = 0
    cmd = ""
    cur = QPointF(0, 0)
    start = QPointF(0, 0)
    last_ctrl: QPointF | None = None
    last_cmd = ""

    def num() -> float:
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in "Ff":
                fill = num()
                path.setFillRule(Qt.FillRule.WindingFill if fill == 1 else Qt.FillRule.OddEvenFill)
                continue
            if cmd in "Zz":
                path.closeSubpath()
                cur = QPointF(start)
                last_cmd = cmd
                continue
        elif not cmd:
            raise ValueError(f"Invalid geometry: {text!r}")

        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (cur.x(), cur.y()) if rel else (0.0, 0.0)
        if c == "M":
            cur = QPointF(ox + num(), oy + num())
            path.moveTo(cur)
            start = QPointF(cur)
            cmd = "l" if rel else "L"
            last_ctrl = None
        elif c == "L":
            cur = QPointF(ox + num(), oy + num())
            path.lineTo(cur)
            last_ctrl = None
        elif c == "H":
            cur = QPointF((cur.x() if rel else 0.0) + num(), cur.y())
            path.lineTo(cur)
            last_ctrl = None
        elif c == "V":
            cur = QPointF(cur.x(), (cur.y() if rel else 0.0) + num())
            path.lineTo(cur)
            last_ctrl = None
        elif c == "C":
            c1 = QPointF(ox + num(), oy + num())
            c2 = QPointF(ox + num(), oy + num())
            cur = QPointF(ox + num(), oy + num())
            path.cubicTo(c1, c2, cur)
            last_ctrl = c2
        elif c == "S":
            c1 = (
                QPointF(2 * cur.x() - last_ctrl.x(), 2 * cur.y() - last_ctrl.y())
                if last_ctrl is not None and last_cmd.upper() in "CS"
                else QPointF(cur)
            )
            c2 = QPointF(ox + num(), oy + num())
            cur = QPointF(ox + num(), oy + num())
            path.cubicTo(c1, c2, cur)
            last_ctrl = c2
        elif c == "Q":
            c1 = QPointF(ox + num(), oy + num())
            cur = QPointF(ox + num(), oy + num())
            path.quadTo(c1, cur)
            last_ctrl = c1
        elif c == "T":
            c1 = (
                QPointF(2 * cur.x() - last_ctrl.x(), 2 * cur.y() - last_ctrl.y())
                if last_ctrl is not None and last_cmd.upper() in "QT"
                else QPointF(cur)
            )
            cur = QPointF(ox + num(), oy + num())
            path.quadTo(c1, cur)
            last_ctrl = c1
        elif c == "A":
            rx, ry, angle = num(), num(), num()
            large, sweep = num() != 0, num() != 0
            end = QPointF(ox + num(), oy + num())
            _arc_to(path, cur, end, rx, ry, angle, large, sweep)
            cur = end
            last_ctrl = None
        else:
            raise ValueError(f"Unsupported geometry command {cmd!r} in {text!r}")
        last_cmd = cmd
    return path


def _arc_to(path: QPainterPath, p0: QPointF, p1: QPointF, rx: float, ry: float, angle: float, large: bool, sweep: bool) -> None:
    """SVG/WPF endpoint arc to cubic segments."""
    if rx == 0 or ry == 0 or (p0.x() == p1.x() and p0.y() == p1.y()):
        path.lineTo(p1)
        return
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(angle)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (p0.x() - p1.x()) / 2, (p0.y() - p1.y()) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lam = (x1p**2) / (rx**2) + (y1p**2) / (ry**2)
    if lam > 1:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale
    num = rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2
    den = rx**2 * y1p**2 + ry**2 * x1p**2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (p0.x() + p1.x()) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (p0.y() + p1.y()) / 2

    def ang(ux: float, uy: float, vx: float, vy: float) -> float:
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    segments = max(1, math.ceil(abs(dt) / (math.pi / 2)))
    delta = dt / segments
    k = 4 / 3 * math.tan(delta / 4)
    t = t1
    for _ in range(segments):
        cos1, sin1 = math.cos(t), math.sin(t)
        cos2, sin2 = math.cos(t + delta), math.sin(t + delta)
        e1 = (cos1 - k * sin1, sin1 + k * cos1)
        e2 = (cos2 + k * sin2, sin2 - k * cos2)
        pts = []
        for ex, ey in (e1, e2, (cos2, sin2)):
            x, y = ex * rx, ey * ry
            pts.append(QPointF(cx + cos_phi * x - sin_phi * y, cy + sin_phi * x + cos_phi * y))
        path.cubicTo(pts[0], pts[1], pts[2])
        t += delta
