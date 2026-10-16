"""Turn XAML brush, drawing, geometry, transform and effect objects into Qt equivalents."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QGradient,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTransform,
)

from ..xaml import values
from ..xaml.resources import XObj

log = logging.getLogger(__name__)

# resolve(value, prop) -> resolved value, used for nested markup inside brush objects
Resolver = Callable[[Any, str], Any]

TRANSPARENT = QColor(0, 0, 0, 0)


def to_color(value: Any, resolve: Resolver) -> QColor | None:
    value = resolve(value, "Color")
    if value is None:
        return None
    if isinstance(value, QColor):
        return value
    if isinstance(value, XObj) and value.type == "Color":
        return to_color(value.text or "", resolve)
    return values.parse_color(str(value))


def _num(obj: XObj, key: str, default: float, resolve: Resolver) -> float:
    raw = resolve(obj.props.get(key), key)
    if raw is None:
        return default
    return float(raw) if isinstance(raw, (int, float)) else values.parse_double(str(raw))


def _point(obj: XObj, key: str, default: QPointF, resolve: Resolver) -> QPointF:
    raw = resolve(obj.props.get(key), key)
    if raw is None:
        return default
    return raw if isinstance(raw, QPointF) else values.parse_point(str(raw))


def _rect(obj: XObj, key: str, default: QRectF, resolve: Resolver) -> QRectF:
    raw = resolve(obj.props.get(key), key)
    if raw is None:
        return default
    return raw if isinstance(raw, QRectF) else values.parse_rect(str(raw))


def _stops(obj: XObj, opacity: float, resolve: Resolver) -> list[tuple[float, QColor]]:
    items = list(obj.children)
    extra = obj.props.get("GradientStops")
    if isinstance(extra, list):
        items += extra
    stops = []
    for stop in items:
        if isinstance(stop, XObj) and stop.type == "GradientStop":
            color = to_color(stop.props.get("Color", "Transparent"), resolve) or TRANSPARENT
            if opacity < 1:
                color = QColor(color)
                color.setAlphaF(color.alphaF() * opacity)
            stops.append((min(1.0, max(0.0, _num(stop, "Offset", 0.0, resolve))), color))
    stops.sort(key=lambda s: s[0])
    return stops


def _spread(obj: XObj) -> QGradient.Spread:
    mode = str(obj.props.get("SpreadMethod", "Pad"))
    return {"Reflect": QGradient.Spread.ReflectSpread, "Repeat": QGradient.Spread.RepeatSpread}.get(mode, QGradient.Spread.PadSpread)


def make_brush(value: Any, rect: QRectF, resolve: Resolver, load_image: Callable[[Any, Any], QImage | None]) -> QBrush | None:
    """Create a QBrush for ``value`` filling ``rect`` (element-relative bounding box)."""
    value = resolve(value, "Brush")
    if value is None:
        return None
    if isinstance(value, QBrush):
        return value
    if isinstance(value, QColor):
        return QBrush(value) if value.alpha() else None
    if isinstance(value, str):
        color = values.parse_color(value)
        return QBrush(color) if color.alpha() else None
    if not isinstance(value, XObj):
        return None
    opacity = _num(value, "Opacity", 1.0, resolve)
    kind = value.type
    if kind == "SolidColorBrush":
        color = to_color(value.props.get("Color", "Transparent"), resolve)
        if color is None:
            return None
        if opacity < 1:
            color = QColor(color)
            color.setAlphaF(color.alphaF() * opacity)
        return QBrush(color) if color.alpha() else None
    if kind == "LinearGradientBrush":
        absolute = str(value.props.get("MappingMode", "")) == "Absolute"
        start = _point(value, "StartPoint", QPointF(0, 0), resolve)
        end = _point(value, "EndPoint", QPointF(1, 1), resolve)
        if absolute:
            start, end = start + rect.topLeft(), end + rect.topLeft()
        else:
            start = QPointF(rect.x() + start.x() * rect.width(), rect.y() + start.y() * rect.height())
            end = QPointF(rect.x() + end.x() * rect.width(), rect.y() + end.y() * rect.height())
        grad = QLinearGradient(start, end)
        grad.setSpread(_spread(value))
        for offset, color in _stops(value, opacity, resolve):
            grad.setColorAt(offset, color)
        return QBrush(grad)
    if kind == "RadialGradientBrush":
        absolute = str(value.props.get("MappingMode", "")) == "Absolute"
        center = _point(value, "Center", QPointF(0.5, 0.5), resolve)
        origin = _point(value, "GradientOrigin", QPointF(center), resolve)
        rx = _num(value, "RadiusX", 0.5, resolve)
        ry = _num(value, "RadiusY", 0.5, resolve)
        if absolute:
            cx, cy = rect.x() + center.x(), rect.y() + center.y()
            sx, sy = rx, ry
        else:
            cx, cy = rect.x() + center.x() * rect.width(), rect.y() + center.y() * rect.height()
            sx, sy = rx * rect.width(), ry * rect.height()
        if sx <= 0 or sy <= 0:
            return None
        focal = QPointF((origin.x() - center.x()) / rx, (origin.y() - center.y()) / ry)
        grad = QRadialGradient(QPointF(0, 0), 1.0, focal)
        grad.setSpread(_spread(value))
        for offset, color in _stops(value, opacity, resolve):
            grad.setColorAt(offset, color)
        brush = QBrush(grad)
        brush.setTransform(QTransform().translate(cx, cy).scale(sx, sy))
        return brush
    if kind in ("ImageBrush", "DrawingBrush"):
        key = (id(value), round(rect.width(), 2), round(rect.height(), 2), _generation)
        cached = _tile_cache.get(key)
        if cached is None:
            cached = _content_brush(value, rect.size(), opacity, resolve, load_image)
            if len(_tile_cache) > 512:
                _tile_cache.clear()
            _tile_cache[key] = cached
        if cached is None:
            return None
        brush = QBrush(cached)
        brush.setTransform(cached.transform() * QTransform().translate(rect.x(), rect.y()))
        return brush
    log.debug("Unsupported brush type %s", kind)
    return None


_generation = 0
_tile_cache: dict[tuple, QBrush | None] = {}


def invalidate_cache() -> None:
    """Call when the theme changes; cached tile brushes may depend on theme resources."""
    global _generation
    _generation += 1
    _tile_cache.clear()


def _content_brush(value: XObj, size: QSizeF, opacity: float, resolve: Resolver, load_image: Callable[[Any, Any], QImage | None]) -> QBrush | None:
    rect = QRectF(QPointF(0, 0), size)
    if value.type == "ImageBrush":
        image = load_image(resolve(value.props.get("ImageSource"), "ImageSource"), value.base)
        if image is None or image.isNull():
            return None
        return _tile_brush(value, rect, opacity, resolve, image_painter(image), QRectF(0, 0, image.width(), image.height()))
    drawing = resolve(value.props.get("Drawing"), "Drawing")
    if drawing is None:
        return None
    bounds = drawing_bounds(drawing, resolve)
    if bounds.isEmpty():
        return None
    return _tile_brush(value, rect, opacity, resolve, lambda p, target, src: _paint_drawing_into(p, drawing, target, src, resolve, load_image), bounds)


def image_painter(image: QImage) -> Callable[[QPainter, QRectF, QRectF], None]:
    def paint(p: QPainter, target: QRectF, src: QRectF) -> None:
        p.drawImage(target, image, src)

    return paint


def _tile_brush(
    obj: XObj,
    rect: QRectF,
    opacity: float,
    resolve: Resolver,
    paint: Callable[[QPainter, QRectF, QRectF], None],
    content: QRectF,
) -> QBrush | None:
    """Shared ImageBrush/DrawingBrush logic: Viewbox selects content, Viewport places it."""
    if "Viewbox" in obj.props:
        viewbox = _rect(obj, "Viewbox", QRectF(0, 0, 1, 1), resolve)
        if str(obj.props.get("ViewboxUnits", "RelativeToBoundingBox")) != "Absolute":
            viewbox = QRectF(
                content.x() + viewbox.x() * content.width(),
                content.y() + viewbox.y() * content.height(),
                viewbox.width() * content.width(),
                viewbox.height() * content.height(),
            )
    else:
        viewbox = QRectF(content)
    viewport = _rect(obj, "Viewport", QRectF(0, 0, 1, 1), resolve)
    if str(obj.props.get("ViewportUnits", "RelativeToBoundingBox")) != "Absolute":
        viewport = QRectF(viewport.x() * rect.width(), viewport.y() * rect.height(), viewport.width() * rect.width(), viewport.height() * rect.height())
    if viewport.width() <= 0 or viewport.height() <= 0 or viewbox.width() <= 0 or viewbox.height() <= 0:
        return None
    stretch = str(obj.props.get("Stretch", "Fill"))
    target = _stretch_rect(QSizeF(viewbox.width(), viewbox.height()), QRectF(0, 0, viewport.width(), viewport.height()), stretch, obj)
    tile_mode = str(obj.props.get("TileMode", "None"))
    tiled = tile_mode != "None"
    width = max(1, math.ceil(viewport.width() if tiled else rect.width()))
    height = max(1, math.ceil(viewport.height() if tiled else rect.height()))
    scale = 4 if tiled and width * height <= 64 else 1
    image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, obj.type != "DrawingBrush")
    p.scale(scale, scale)
    if opacity < 1:
        p.setOpacity(opacity)
    if tiled:
        paint(p, target, viewbox)
    else:
        p.setClipRect(QRectF(0, 0, rect.width(), rect.height()))
        paint(p, target.translated(viewport.topLeft()), viewbox)
    p.end()
    brush = QBrush(image)
    transform = QTransform().translate(rect.x() + (viewport.x() if tiled else 0), rect.y() + (viewport.y() if tiled else 0))
    if scale != 1:
        transform = QTransform().scale(1 / scale, 1 / scale) * transform
    brush.setTransform(transform)
    return brush


def _stretch_rect(src: QSizeF, dest: QRectF, stretch: str, obj: XObj | None = None) -> QRectF:
    if stretch == "Fill" or src.width() <= 0 or src.height() <= 0:
        return QRectF(dest)
    if stretch == "None":
        w, h = src.width(), src.height()
    else:
        sx, sy = dest.width() / src.width(), dest.height() / src.height()
        s = min(sx, sy) if stretch == "Uniform" else max(sx, sy)
        w, h = src.width() * s, src.height() * s
    ax = str(obj.props.get("AlignmentX", "Center")) if obj is not None else "Center"
    ay = str(obj.props.get("AlignmentY", "Center")) if obj is not None else "Center"
    x = dest.x() + {"Left": 0.0, "Right": dest.width() - w}.get(ax, (dest.width() - w) / 2)
    y = dest.y() + {"Top": 0.0, "Bottom": dest.height() - h}.get(ay, (dest.height() - h) / 2)
    return QRectF(x, y, w, h)


# --- geometry & drawings ---------------------------------------------------------------------


def to_geometry(value: Any, resolve: Resolver) -> QPainterPath | None:
    value = resolve(value, "Geometry")
    if value is None:
        return None
    if isinstance(value, QPainterPath):
        return value
    if isinstance(value, str):
        return values.parse_geometry(value)
    if isinstance(value, XObj):
        if value.type == "EllipseGeometry":
            center = _point(value, "Center", QPointF(0, 0), resolve)
            rx, ry = _num(value, "RadiusX", 0, resolve), _num(value, "RadiusY", 0, resolve)
            path = QPainterPath()
            path.addEllipse(center, rx, ry)
            return path
        if value.type == "RectangleGeometry":
            r = _rect(value, "Rect", QRectF(), resolve)
            rx, ry = _num(value, "RadiusX", 0, resolve), _num(value, "RadiusY", 0, resolve)
            path = QPainterPath()
            if rx or ry:
                path.addRoundedRect(r, rx, ry)
            else:
                path.addRect(r)
            return path
        if value.type in ("PathGeometry", "StreamGeometry") and value.props.get("Figures"):
            return values.parse_geometry(str(value.props["Figures"]))
        if value.type == "GeometryGroup":
            path = QPainterPath()
            for child in value.children:
                sub = to_geometry(child, resolve)
                if sub is not None:
                    path.addPath(sub)
            return path
        if value.text:
            return values.parse_geometry(value.text)
    return None


def _drawing_items(drawing: Any, resolve: Resolver) -> list[XObj]:
    drawing = resolve(drawing, "Drawing")
    if isinstance(drawing, XObj):
        if drawing.type == "DrawingGroup":
            items: list[XObj] = []
            for child in drawing.children + (drawing.props.get("Children") or []):
                items.extend(_drawing_items(child, resolve))
            return items
        return [drawing]
    return []


def drawing_bounds(drawing: Any, resolve: Resolver) -> QRectF:
    bounds = QRectF()
    for item in _drawing_items(drawing, resolve):
        if item.type == "GeometryDrawing":
            geom = to_geometry(item.props.get("Geometry"), resolve)
            if geom is not None:
                bounds = bounds.united(geom.boundingRect())
        elif item.type == "ImageDrawing":
            bounds = bounds.united(_rect(item, "Rect", QRectF(), resolve))
    return bounds


def _paint_drawing_into(
    p: QPainter, drawing: Any, target: QRectF, viewbox: QRectF, resolve: Resolver, load_image: Callable[[Any, Any], QImage | None]
) -> None:
    p.save()
    p.translate(target.x(), target.y())
    p.scale(target.width() / viewbox.width(), target.height() / viewbox.height())
    p.translate(-viewbox.x(), -viewbox.y())
    for item in _drawing_items(drawing, resolve):
        if item.type == "GeometryDrawing":
            geom = to_geometry(item.props.get("Geometry"), resolve)
            if geom is None:
                continue
            brush = make_brush(item.props.get("Brush"), geom.boundingRect(), resolve, load_image)
            if brush is not None:
                p.fillPath(geom, brush)
            pen = resolve(item.props.get("Pen"), "Pen")
            if isinstance(pen, XObj):
                pen_brush = make_brush(pen.props.get("Brush"), geom.boundingRect(), resolve, load_image)
                if pen_brush is not None:
                    p.strokePath(geom, QPen(pen_brush, _num(pen, "Thickness", 1, resolve)))
        elif item.type == "ImageDrawing":
            image = load_image(resolve(item.props.get("ImageSource"), "ImageSource"), item.base)
            if image is not None:
                p.drawImage(_rect(item, "Rect", QRectF(), resolve), image)
    p.restore()


# --- transforms & effects --------------------------------------------------------------------


def to_transform(value: Any, size: QSizeF, origin: QPointF, resolve: Resolver) -> QTransform | None:
    value = resolve(value, "RenderTransform")
    if not isinstance(value, XObj):
        return None
    ox, oy = origin.x() * size.width(), origin.y() * size.height()

    def single(obj: XObj) -> QTransform:
        t = QTransform()
        if obj.type == "ScaleTransform":
            cx, cy = _num(obj, "CenterX", 0, resolve), _num(obj, "CenterY", 0, resolve)
            t.translate(cx, cy)
            t.scale(_num(obj, "ScaleX", 1, resolve), _num(obj, "ScaleY", 1, resolve))
            t.translate(-cx, -cy)
        elif obj.type == "RotateTransform":
            cx, cy = _num(obj, "CenterX", 0, resolve), _num(obj, "CenterY", 0, resolve)
            t.translate(cx, cy)
            t.rotate(_num(obj, "Angle", 0, resolve))
            t.translate(-cx, -cy)
        elif obj.type == "TranslateTransform":
            t.translate(_num(obj, "X", 0, resolve), _num(obj, "Y", 0, resolve))
        elif obj.type == "SkewTransform":
            t.shear(math.tan(math.radians(_num(obj, "AngleX", 0, resolve))), math.tan(math.radians(_num(obj, "AngleY", 0, resolve))))
        elif obj.type == "TransformGroup":
            for child in obj.children:
                if isinstance(child, XObj):
                    t = t * single(child)
        return t

    inner = single(value)
    if inner.isIdentity():
        return None
    return QTransform().translate(-ox, -oy) * inner * QTransform().translate(ox, oy)


def blur_image(image: QImage, radius: float) -> QImage:
    """Gaussian-style blur using QGraphicsBlurEffect."""
    if radius <= 0.5:
        return image
    from PyQt6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(image))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    result = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(0)
    p = QPainter(result)
    scene.render(p, QRectF(0, 0, image.width(), image.height()), QRectF(0, 0, image.width(), image.height()))
    p.end()
    return result


def drop_shadow(image: QImage, effect: XObj, resolve: Resolver) -> tuple[QImage, QPointF] | None:
    """Return the shadow image for ``image`` and its offset, per WPF DropShadowEffect."""
    color = to_color(effect.props.get("Color", "Black"), resolve) or QColor("black")
    opacity = _num(effect, "Opacity", 1.0, resolve) * color.alphaF()
    if opacity <= 0:
        return None
    depth = _num(effect, "ShadowDepth", 5.0, resolve)
    direction = math.radians(_num(effect, "Direction", 315.0, resolve))
    blur = _num(effect, "BlurRadius", 5.0, resolve)
    shadow = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    shadow.fill(0)
    p = QPainter(shadow)
    p.drawImage(0, 0, image)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    solid = QColor(color)
    solid.setAlphaF(1.0)
    p.fillRect(shadow.rect(), solid)
    p.end()
    shadow = blur_image(shadow, blur * image.devicePixelRatio())
    if opacity < 1:
        faded = QImage(shadow.size(), QImage.Format.Format_ARGB32_Premultiplied)
        faded.fill(0)
        p = QPainter(faded)
        p.setOpacity(opacity)
        p.drawImage(0, 0, shadow)
        p.end()
        shadow = faded
    shadow.setDevicePixelRatio(image.devicePixelRatio())
    return shadow, QPointF(math.cos(direction) * depth, -math.sin(direction) * depth)


def rounded_path(rect: QRectF, radii: tuple[tuple[float, float], ...]) -> QPainterPath:
    """Rectangle with elliptical corners; radii = ((tlx, tly), (trx, try), (brx, bry), (blx, bly))."""
    path = QPainterPath()
    if rect.width() <= 0 or rect.height() <= 0:
        return path
    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = radii
    if not any((tlx, tly, trx, try_, brx, bry, blx, bly)):
        path.addRect(rect)
        return path
    # Scale radii down when they overlap, as WPF does.
    w, h = rect.width(), rect.height()
    f = min(
        1.0,
        w / (tlx + trx) if tlx + trx > w else 1.0,
        w / (blx + brx) if blx + brx > w else 1.0,
        h / (tly + bly) if tly + bly > h else 1.0,
        h / (try_ + bry) if try_ + bry > h else 1.0,
    )
    tlx, tly, trx, try_, brx, bry, blx, bly = (v * f for v in (tlx, tly, trx, try_, brx, bry, blx, bly))
    x, y, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
    path.moveTo(x + tlx, y)
    path.lineTo(r - trx, y)
    if trx and try_:
        path.arcTo(QRectF(r - 2 * trx, y, 2 * trx, 2 * try_), 90, -90)
    path.lineTo(r, b - bry)
    if brx and bry:
        path.arcTo(QRectF(r - 2 * brx, b - 2 * bry, 2 * brx, 2 * bry), 0, -90)
    path.lineTo(x + blx, b)
    if blx and bly:
        path.arcTo(QRectF(x, b - 2 * bly, 2 * blx, 2 * bly), -90, -90)
    path.lineTo(x, y + tly)
    if tlx and tly:
        path.arcTo(QRectF(x, y, 2 * tlx, 2 * tly), 180, -90)
    path.closeSubpath()
    return path


__all__ = ["Qt", "blur_image", "drop_shadow", "make_brush", "rounded_path", "to_color", "to_geometry", "to_transform"]
