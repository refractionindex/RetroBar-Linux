"""A small WPF-compatible element tree: property precedence, styles, templates, triggers, layout and painting.

Only what RetroBar themes use is implemented. The goal is that an unmodified theme ResourceDictionary
renders the same structure it does in WPF.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QIcon, QImage, QPainter, QPainterPath, QPen, QTransform

from ..xaml import markup, values
from ..xaml.markup import NULL, Binding, ResourceRef, StaticMember, TemplateBindingRef
from ..xaml.resources import MISSING, ControlTemplate, Style, Trigger, XObj
from ..xaml.values import CornerRadius, Thickness
from . import brushes

if TYPE_CHECKING:
    from ..theme import Theme

log = logging.getLogger(__name__)

INF = math.inf
NAN = math.nan

_theme: Theme | None = None
_generation = 0
_in_progress: set[tuple[int, str]] = set()


def set_theme(theme: Theme) -> None:
    global _theme
    _theme = theme
    brushes.invalidate_cache()
    invalidate()


def theme() -> Theme:
    assert _theme is not None, "No theme loaded"
    return _theme


def invalidate() -> None:
    """Drop all cached property values; call after any state the property system depends on changes."""
    global _generation
    _generation += 1


THICKNESS_PROPS = {"Margin", "Padding", "BorderThickness"}
DOUBLE_PROPS = {
    "Width",
    "Height",
    "MinWidth",
    "MinHeight",
    "MaxWidth",
    "MaxHeight",
    "FontSize",
    "Opacity",
    "StrokeThickness",
    "RadiusX",
    "RadiusY",
    "Canvas.Left",
    "Canvas.Top",
    "Canvas.Right",
    "Canvas.Bottom",
    "StrokeDashOffset",
}
INT_PROPS = {"Grid.Row", "Grid.Column", "Grid.RowSpan", "Grid.ColumnSpan"}
BOOL_PROPS = {"IsChecked", "IsPressed", "IsMouseOver", "IsEnabled", "LastChildFill", "SnapsToDevicePixels", "UseLayoutRounding", "IsLocked", "IsScaled"}
INHERITED = {
    "FontFamily",
    "FontSize",
    "FontWeight",
    "FontStyle",
    "Foreground",
    "FlowDirection",
    "DataContext",
    "TextOptions.TextRenderingMode",
    "TextOptions.TextFormattingMode",
    "RenderOptions.EdgeMode",
}
BASE_DEFAULTS: dict[str, Any] = {
    "Visibility": "Visible",
    "HorizontalAlignment": "Stretch",
    "VerticalAlignment": "Stretch",
    "Width": NAN,
    "Height": NAN,
    "MinWidth": 0.0,
    "MinHeight": 0.0,
    "MaxWidth": INF,
    "MaxHeight": INF,
    "Margin": values.ZERO,
    "Padding": values.ZERO,
    "BorderThickness": values.ZERO,
    "CornerRadius": CornerRadius(),
    "Opacity": 1.0,
    "FontSize": 12.0,
    "FontWeight": 400,
    "FontStyle": "Normal",
    "Foreground": QColor("black"),
    "FlowDirection": "LeftToRight",
    "IsMouseOver": False,
    "IsPressed": False,
    "IsChecked": False,
    "IsEnabled": True,
    "Grid.Row": 0,
    "Grid.Column": 0,
    "Grid.RowSpan": 1,
    "Grid.ColumnSpan": 1,
    "DockPanel.Dock": "Left",
    "TextOptions.TextRenderingMode": "Auto",
    "StrokeThickness": 1.0,
    "RenderTransformOrigin": QPointF(0, 0),
}


# --- value matching & bindings -------------------------------------------------------------------


def _matches(actual: Any, expected: Any) -> bool:
    if expected is NULL or expected is None:
        return actual is None
    if isinstance(expected, str):
        if isinstance(actual, bool):
            return expected.strip().lower() == ("true" if actual else "false")
        if isinstance(actual, (int, float)):
            try:
                return float(expected) == float(actual)
            except ValueError:
                return False
        if actual is None:
            return False
        return str(actual) == expected.strip()
    return actual == expected


def format_datetime(value: datetime, fmt: str | None) -> str:
    from ..dotnet_format import format_datetime as fmt_dt

    return fmt_dt(value, fmt)


def eval_binding(binding: Binding, el: Element) -> Any:
    source: Any
    if binding.source == "self":
        source = el
    elif binding.source == "templated_parent":
        source = el.tp
    elif binding.source.startswith("ancestor:"):
        wanted = binding.source.split(":", 1)[1]
        source = el.find_ancestor(wanted)
    else:
        source = el.get("DataContext")
    if source is None:
        return None
    value: Any = source
    for part in binding.path.split(".") if binding.path else []:
        if value is None:
            return None
        if isinstance(value, Element):
            if part == "ActualWidth":
                value = value.rect.width()
            elif part == "ActualHeight":
                value = value.rect.height()
            else:
                value = value.get(part)
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
    if binding.string_format is not None:
        if isinstance(value, datetime):
            return format_datetime(value, binding.string_format)
        try:
            return format(value, binding.string_format)
        except (TypeError, ValueError):
            return str(value)
    return value


# --- elements -------------------------------------------------------------------------------------


class Element:
    TYPE = "FrameworkElement"
    DEFAULTS: dict[str, Any] = {}

    def __init__(self, type_name: str | None = None) -> None:
        self.type = type_name or self.TYPE
        self.local: dict[str, Any] = {}
        self.tvals: dict[str, Any] = {}
        self.base = None
        self.tp: Control | None = None
        self.tp_name: str | None = None
        self.parent: Element | None = None
        self.children: list[Element] = []
        self.rect = QRectF()
        self.layout_clip: QRectF | None = None
        self.desired = (0.0, 0.0)
        self._unclipped = (0.0, 0.0)
        self._cache: dict[str, Any] = {}
        self._gen = -1
        self._trig_cache: dict[int, bool] = {}
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.tag: Any = None

    def __repr__(self) -> str:
        name = f" {self.tp_name}" if self.tp_name else ""
        return f"<{self.type}{name}>"

    # --- tree ---------------------------------------------------------------------------------
    def add(self, child: Element) -> Element:
        if child.parent is not None and child in child.parent.children:
            child.parent.children.remove(child)
        child.parent = self
        self.children.append(child)
        return child

    def set_children(self, children: list[Element]) -> None:
        for child in self.children:
            if child.parent is self:
                child.parent = None
        self.children = []
        for child in children:
            self.add(child)

    def walk(self) -> Iterator[Element]:
        yield self
        for child in self.children:
            yield from child.walk()

    def ancestors(self) -> Iterator[Element]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def find_ancestor(self, type_name: str) -> Element | None:
        for node in self.ancestors():
            if node.type == type_name or (type_name == "Window" and isinstance(node, Window)):
                return node
        return None

    def root(self) -> Element:
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    # --- properties ---------------------------------------------------------------------------
    def set(self, prop: str, value: Any) -> None:
        if self.local.get(prop, MISSING) is value:
            return
        self.local[prop] = value
        invalidate()

    def default(self, prop: str) -> Any:
        if prop in self.DEFAULTS:
            return self.DEFAULTS[prop]
        for cls in type(self).__mro__:
            d = getattr(cls, "DEFAULTS", None)
            if d and prop in d:
                return d[prop]
        return BASE_DEFAULTS.get(prop)

    @property
    def style(self) -> Style | None:
        if self._gen == _generation and "__style" in self._cache:
            return self._cache["__style"]
        raw = self.local.get("Style", MISSING)
        if raw is MISSING:
            raw = self.tvals.get("Style", MISSING)
        style = None
        if raw is not MISSING:
            resolved = self._resolve(raw, "Style")
            style = resolved if isinstance(resolved, Style) else None
        else:
            implicit = theme().lookup(("type", self.type))
            style = implicit if isinstance(implicit, Style) else None
        if self._gen != _generation:
            self._cache.clear()
            self._trig_cache.clear()
            self._gen = _generation
        self._cache["__style"] = style
        return style

    def get(self, prop: str) -> Any:
        if self._gen != _generation:
            self._cache.clear()
            self._trig_cache.clear()
            self._gen = _generation
        try:
            return self._cache[prop]
        except KeyError:
            pass
        key = (id(self), prop)
        reentrant = key in _in_progress
        _in_progress.add(key)
        try:
            value = self._compute(prop, reentrant)
        finally:
            if not reentrant:
                _in_progress.discard(key)
        if not reentrant:
            self._cache[prop] = value
        return value

    def _layers(self, prop: str, skip_triggers: bool) -> Iterator[tuple[Any, Any]]:
        if prop in self.local:
            yield self.local[prop], None
        tp = self.tp
        if not skip_triggers and tp is not None and self.tp_name and tp.current_template is not None:
            for trig in reversed(tp.current_template.triggers):
                for setter in trig.setters:
                    if setter.target == self.tp_name and setter.prop == prop and tp.trigger_active(trig):
                        yield setter.value, setter.base
        if prop in self.tvals:
            yield self.tvals[prop], self.base
        style = self.style
        if not skip_triggers and style is not None:
            for trig in reversed(style.all_triggers()):
                for setter in trig.setters:
                    if setter.target is None and setter.prop == prop and self.trigger_active(trig):
                        yield setter.value, setter.base
        template = getattr(self, "current_template", None)
        if not skip_triggers and template is not None:
            for trig in reversed(template.triggers):
                for setter in trig.setters:
                    if setter.target is None and setter.prop == prop and self.trigger_active(trig):
                        yield setter.value, setter.base
        if style is not None:
            setter = style.all_setters().get(prop)
            if setter is not None:
                yield setter.value, setter.base

    def _compute(self, prop: str, skip_triggers: bool) -> Any:
        for raw, base in self._layers(prop, skip_triggers):
            value = self._resolve(raw, prop)
            if value is MISSING:
                continue
            return self._convert(prop, value, base)
        if prop in INHERITED and self.parent is not None:
            return self.parent.get(prop)
        return self.default(prop)

    def _resolve(self, value: Any, prop: str) -> Any:
        for _ in range(16):
            if isinstance(value, ResourceRef):
                found = theme().lookup(value.key)
                if found is MISSING:
                    return MISSING
                value = found
            elif isinstance(value, TemplateBindingRef):
                if self.tp is None:
                    return MISSING
                value = self.tp.get(value.prop)
                return value
            elif isinstance(value, Binding):
                return eval_binding(value, self)
            elif isinstance(value, StaticMember):
                value = theme().static_member(value)
            elif value is NULL:
                return None
            else:
                return value
        return MISSING

    def resolver(self) -> Callable[[Any, str], Any]:
        def resolve(value: Any, prop: str) -> Any:
            result = self._resolve(value, prop)
            return None if result is MISSING else result

        return resolve

    def _convert(self, prop: str, value: Any, base: Any) -> Any:
        if value is None:
            return None
        try:
            if prop in THICKNESS_PROPS:
                return value if isinstance(value, Thickness) else Thickness.parse(str(value))
            if prop == "CornerRadius":
                return value if isinstance(value, CornerRadius) else CornerRadius.parse(str(value))
            if prop in DOUBLE_PROPS:
                return float(value) if isinstance(value, (int, float)) else values.parse_double(str(value))
            if prop in INT_PROPS:
                return int(value)
            if prop in BOOL_PROPS:
                return value if isinstance(value, bool) else values.parse_bool(str(value))
            if prop == "FontWeight":
                return value if isinstance(value, int) else values.parse_font_weight(str(value))
            if prop == "FontFamily":
                return value.text if isinstance(value, XObj) else str(value)
            if prop == "RenderTransformOrigin":
                return value if isinstance(value, QPointF) else values.parse_point(str(value))
            if prop == "Data":
                return brushes.to_geometry(value, self.resolver())
            if prop == "Source":
                return theme().load_image(value, base if base is not None else self.base)
        except (TypeError, ValueError) as exc:
            log.debug("Bad value %r for %s on %s: %s", value, prop, self, exc)
            return self.default(prop)
        return value

    def trigger_active(self, trig: Trigger) -> bool:
        if self._gen != _generation:
            self._cache.clear()
            self._trig_cache.clear()
            self._gen = _generation
        key = id(trig)
        cached = self._trig_cache.get(key)
        if cached is not None:
            return cached
        result = True
        for cond in trig.conditions:
            if cond.prop:
                actual = self.get(cond.prop)
            elif cond.binding is not None:
                actual = eval_binding(cond.binding, self) if isinstance(cond.binding, Binding) else None
            else:
                actual = None
            expected = cond.value
            if isinstance(expected, ResourceRef):
                expected = self._resolve(expected, "Value")
            if not _matches(actual, expected):
                result = False
                break
        self._trig_cache[key] = result
        return result

    # --- layout -------------------------------------------------------------------------------
    @property
    def collapsed(self) -> bool:
        return self.get("Visibility") == "Collapsed"

    def prepare(self) -> None:
        """Hook for building template/content children before measuring."""

    def _minmax(self) -> tuple[float, float, float, float]:
        w, h = self.get("Width"), self.get("Height")
        min_w, max_w = self.get("MinWidth") or 0.0, self.get("MaxWidth")
        min_h, max_h = self.get("MinHeight") or 0.0, self.get("MaxHeight")
        max_w = INF if max_w is None else max_w
        max_h = INF if max_h is None else max_h
        max_w = max(min(INF if w is None or math.isnan(w) else w, max_w), min_w)
        min_w = max(min(max_w, 0.0 if w is None or math.isnan(w) else w), min_w)
        max_h = max(min(INF if h is None or math.isnan(h) else h, max_h), min_h)
        min_h = max(min(max_h, 0.0 if h is None or math.isnan(h) else h), min_h)
        return min_w, max_w, min_h, max_h

    def measure(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        if self.collapsed:
            self.desired = (0.0, 0.0)
            self._unclipped = (0.0, 0.0)
            return self.desired
        self.prepare()
        m: Thickness = self.get("Margin")
        min_w, max_w, min_h, max_h = self._minmax()
        fw = min(max(max(0.0, avail_w - m.horizontal), min_w), max_w)
        fh = min(max(max(0.0, avail_h - m.vertical), min_h), max_h)
        dw, dh = self.measure_override(fw, fh)
        dw = min(max(dw, min_w), max_w)
        dh = min(max(dh, min_h), max_h)
        self._unclipped = (dw, dh)
        cw = max(0.0, min(dw + m.horizontal, avail_w))
        ch = max(0.0, min(dh + m.vertical, avail_h))
        self.desired = (cw, ch)
        return self.desired

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        dw = dh = 0.0
        for child in self.children:
            cw, ch = child.measure(w, h)
            dw, dh = max(dw, cw), max(dh, ch)
        return dw, dh

    def arrange(self, x: float, y: float, w: float, h: float) -> None:
        if self.collapsed:
            self.rect = QRectF(x, y, 0, 0)
            return
        m: Thickness = self.get("Margin")
        ix, iy = x + m.left, y + m.top
        iw, ih = max(0.0, w - m.horizontal), max(0.0, h - m.vertical)
        min_w, max_w, min_h, max_h = self._minmax()
        uw, uh = self._unclipped
        ha, va = self.get("HorizontalAlignment"), self.get("VerticalAlignment")
        aw = iw if ha == "Stretch" else uw
        ah = ih if va == "Stretch" else uh
        aw = max(min(max(aw, uw), max_w), min_w)
        ah = max(min(max(ah, uh), max_h), min_h)
        if ha == "Left" or (ha == "Stretch" and aw > iw):
            ox = 0.0
        elif ha == "Right":
            ox = iw - aw
        else:
            ox = (iw - aw) / 2
        if va == "Top" or (va == "Stretch" and ah > ih):
            oy = 0.0
        elif va == "Bottom":
            oy = ih - ah
        else:
            oy = (ih - ah) / 2
        left, top = round(ix + ox), round(iy + oy)
        right, bottom = round(ix + ox + aw), round(iy + oy + ah)
        self.rect = QRectF(left, top, max(0, right - left), max(0, bottom - top))
        # Like WPF, an element larger than its layout slot is clipped to the slot.
        self.layout_clip = QRectF(round(ix), round(iy), round(iw), round(ih)) if aw > iw + 0.5 or ah > ih + 0.5 else None
        self.arrange_override(QRectF(self.rect))

    def arrange_override(self, rect: QRectF) -> None:
        for child in self.children:
            child.arrange(rect.x(), rect.y(), rect.width(), rect.height())

    # --- painting -------------------------------------------------------------------------------
    def brush(self, prop: str, rect: QRectF) -> QBrush | None:
        value = self.get(prop)
        if value is None:
            return None
        return brushes.make_brush(value, rect, self.resolver(), theme().load_image)

    def render(self, p: QPainter) -> None:
        if self.collapsed or self.get("Visibility") == "Hidden":
            return
        opacity = self.get("Opacity")
        transform = None
        if self.get("RenderTransform") is not None:
            transform = brushes.to_transform(
                self.get("RenderTransform"), self.rect.size(), self.get("RenderTransformOrigin"), self.resolver()
            )
        effect = self.get("Effect")
        p.save()
        if self.layout_clip is not None:
            p.setClipRect(self.layout_clip, Qt.ClipOperation.IntersectClip)
        if opacity is not None and opacity < 1:
            p.setOpacity(p.opacity() * opacity)
        if transform is not None:
            p.translate(self.rect.topLeft())
            p.setTransform(transform, True)
            p.translate(-self.rect.topLeft())
        if isinstance(effect, XObj) and effect.type == "DropShadowEffect":
            self._render_with_shadow(p, effect)
        else:
            self._render_content(p)
        p.restore()

    def _render_content(self, p: QPainter) -> None:
        self.paint(p)
        clip = self.child_clip()
        if clip is not None:
            p.save()
            p.setClipRect(clip, Qt.ClipOperation.IntersectClip)
        for child in self.children:
            child.render(p)
        if clip is not None:
            p.restore()

    def child_clip(self) -> QRectF | None:
        return None

    def _render_with_shadow(self, p: QPainter, effect: XObj) -> None:
        pad = 12
        bounds = self.rect.adjusted(-pad, -pad, pad, pad)
        dpr = max(1.0, abs(p.transform().m11()))
        image = QImage(max(1, math.ceil(bounds.width() * dpr)), max(1, math.ceil(bounds.height() * dpr)), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        image.setDevicePixelRatio(dpr)
        sub = QPainter(image)
        sub.translate(-bounds.x(), -bounds.y())
        self._render_content(sub)
        sub.end()
        shadow = brushes.drop_shadow(image, effect, self.resolver())
        if shadow is not None:
            shadow_image, offset = shadow
            p.drawImage(bounds.topLeft() + offset, shadow_image)
        p.drawImage(bounds.topLeft(), image)

    def paint(self, p: QPainter) -> None:
        """Draw this element's own content; children are drawn afterwards."""

    # --- hit testing ----------------------------------------------------------------------------
    def hit(self, pt: QPointF) -> Element | None:
        if self.collapsed or self.get("Visibility") == "Hidden":
            return None
        clip = self.child_clip()
        if clip is None or clip.contains(pt):
            for child in reversed(self.children):
                found = child.hit(pt)
                if found is not None:
                    return found
        if self.rect.contains(pt) and self.hit_testable():
            return self
        return None

    def hit_testable(self) -> bool:
        return True


class Panel(Element):
    TYPE = "Panel"

    def paint(self, p: QPainter) -> None:
        b = self.brush("Background", self.rect)
        if b is not None:
            p.fillRect(self.rect, b)


class Border(Element):
    TYPE = "Border"

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        bt: Thickness = self.get("BorderThickness")
        pad: Thickness = self.get("Padding")
        ew, eh = bt.horizontal + pad.horizontal, bt.vertical + pad.vertical
        dw = dh = 0.0
        for child in self.children:
            cw, ch = child.measure(max(0.0, w - ew), max(0.0, h - eh))
            dw, dh = max(dw, cw), max(dh, ch)
        return dw + ew, dh + eh

    def arrange_override(self, rect: QRectF) -> None:
        bt: Thickness = self.get("BorderThickness")
        pad: Thickness = self.get("Padding")
        inner = rect.adjusted(bt.left + pad.left, bt.top + pad.top, -(bt.right + pad.right), -(bt.bottom + pad.bottom))
        for child in self.children:
            child.arrange(inner.x(), inner.y(), max(0.0, inner.width()), max(0.0, inner.height()))

    def paint(self, p: QPainter) -> None:
        rect = self.rect
        if rect.width() <= 0 or rect.height() <= 0:
            return
        bt: Thickness = self.get("BorderThickness")
        cr: CornerRadius = self.get("CornerRadius")
        inner = rect.adjusted(bt.left, bt.top, -bt.right, -bt.bottom)
        if inner.width() < 0 or inner.height() < 0:
            inner = QRectF(rect.center(), QSizeF(0, 0))
        rounded = not cr.is_zero()
        l, t, r, b = bt.left / 2, bt.top / 2, bt.right / 2, bt.bottom / 2

        def outer_radii() -> tuple[tuple[float, float], ...]:
            def rad(c: float, a: float, b_: float) -> tuple[float, float]:
                return (c + a, c + b_) if c else (0.0, 0.0)

            return (rad(cr.top_left, l, t), rad(cr.top_right, r, t), rad(cr.bottom_right, r, b), rad(cr.bottom_left, l, b))

        def inner_radii() -> tuple[tuple[float, float], ...]:
            return (
                (max(0.0, cr.top_left - l), max(0.0, cr.top_left - t)),
                (max(0.0, cr.top_right - r), max(0.0, cr.top_right - t)),
                (max(0.0, cr.bottom_right - r), max(0.0, cr.bottom_right - b)),
                (max(0.0, cr.bottom_left - l), max(0.0, cr.bottom_left - b)),
            )

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, rounded)
        background = self.brush("Background", inner)
        if background is not None and inner.width() > 0 and inner.height() > 0:
            if rounded:
                p.fillPath(brushes.rounded_path(inner, inner_radii()), background)
            else:
                p.fillRect(inner, background)
        if bt.left or bt.top or bt.right or bt.bottom:
            border = self.brush("BorderBrush", rect)
            if border is not None:
                if rounded:
                    path = brushes.rounded_path(rect, outer_radii())
                    hole = brushes.rounded_path(inner, inner_radii())
                    path = path.subtracted(hole)
                    p.fillPath(path, border)
                else:
                    path = QPainterPath()
                    path.setFillRule(Qt.FillRule.OddEvenFill)
                    path.addRect(rect)
                    path.addRect(inner)
                    p.fillPath(path, border)
        p.restore()


class ContentPresenter(Element):
    TYPE = "ContentPresenter"

    def __init__(self, type_name: str | None = None) -> None:
        super().__init__(type_name)
        self._text_child: TextBlock | None = None

    def content(self) -> Any:
        if "Content" in self.local or "Content" in self.tvals:
            return self.get("Content")
        return self.tp.get("Content") if self.tp is not None else None

    def prepare(self) -> None:
        content = self.content()
        if isinstance(content, Element):
            if self.children != [content]:
                self.set_children([content])
        elif content is None:
            if self.children:
                self.set_children([])
        else:
            if self._text_child is None:
                self._text_child = TextBlock()
            self._text_child.local["Text"] = str(content)
            if self.children != [self._text_child]:
                self.set_children([self._text_child])

    def hit_testable(self) -> bool:
        return False


class ScrollContentPresenter(ContentPresenter):
    TYPE = "ScrollContentPresenter"

    def _scroller(self) -> ScrollViewer | None:
        return self.tp if isinstance(self.tp, ScrollViewer) else None

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        sv = self._scroller()
        scrolling = sv is not None and sv.can_scroll
        dw = dh = 0.0
        for child in self.children:
            cw, ch = child.measure(w, INF if scrolling else h)
            dw, dh = max(dw, cw), max(dh, ch)
        if sv is not None:
            sv.extent_height = dh
        return dw, min(dh, h)

    def arrange_override(self, rect: QRectF) -> None:
        sv = self._scroller()
        offset = sv.offset if sv is not None and sv.can_scroll else 0.0
        if sv is not None:
            sv.viewport_height = rect.height()
        for child in self.children:
            height = max(rect.height(), child.desired[1]) if sv is not None and sv.can_scroll else rect.height()
            child.arrange(rect.x(), rect.y() - offset, rect.width(), height)

    def child_clip(self) -> QRectF | None:
        return QRectF(self.rect)


class Control(Element):
    TYPE = "Control"

    def __init__(self, type_name: str | None = None) -> None:
        super().__init__(type_name)
        self.current_template: ControlTemplate | None = None
        self._text_child: TextBlock | None = None

    def prepare(self) -> None:
        template = self.get("Template")
        if not isinstance(template, ControlTemplate):
            template = None
        if template is not None:
            template = theme().checked_template(template, self)
        if template is self.current_template and (self.children or template is None):
            if template is None:
                self._attach_content()
            return
        self.current_template = template
        if template is not None and template.root is not None:
            root = instantiate(template.root, self)
            self.set_children([root])
            invalidate()
        else:
            self._attach_content()

    def _attach_content(self) -> None:
        content = self.get("Content")
        if isinstance(content, Element):
            if self.children != [content]:
                self.set_children([content])
        elif content is None:
            if self.children:
                self.set_children([])
        else:
            if self._text_child is None:
                self._text_child = TextBlock()
            self._text_child.local["Text"] = str(content)
            if self.children != [self._text_child]:
                self.set_children([self._text_child])


class ScrollViewer(Control):
    TYPE = "ScrollViewer"

    def __init__(self, type_name: str | None = None) -> None:
        super().__init__(type_name)
        self.can_scroll = False
        self.offset = 0.0
        self.extent_height = 0.0
        self.viewport_height = 0.0
        self.local["ComputedVerticalScrollBarVisibility"] = "Collapsed"

    def set_scrollable(self, enabled: bool) -> None:
        if enabled != self.can_scroll:
            self.can_scroll = enabled
            if not enabled:
                self.offset = 0.0
            self.set("ComputedVerticalScrollBarVisibility", "Visible" if enabled else "Collapsed")

    def page(self, direction: int) -> None:
        if not self.can_scroll:
            return
        step = max(1.0, self.viewport_height)
        limit = max(0.0, self.extent_height - self.viewport_height)
        self.offset = min(limit, max(0.0, self.offset + direction * step))
        invalidate()


class Window(Element):
    TYPE = "Window"

    def paint(self, p: QPainter) -> None:
        b = self.brush("Background", self.rect)
        if b is not None:
            p.fillRect(self.rect, b)


class TextBlock(Element):
    TYPE = "TextBlock"
    SYMBOL_FONTS = {
        "webdings": {"3": "\u25c0", "4": "\u25b6", "5": "\u25b2", "6": "\u25bc"},
        "marlett": {"0": "\u2581", "1": "\u25a1", "2": "\u2750", "r": "\u2715", "3": "\u25c0", "4": "\u25b6", "5": "\u25b2", "6": "\u25bc", "a": "\u2713", "h": "\u25cf"},
    }

    def text(self) -> str:
        text = self.get("Text")
        if text is None:
            return ""
        text = str(text)
        family = (self.get("FontFamily") or "").strip().lower()
        mapping = self.SYMBOL_FONTS.get(family)
        if mapping:
            text = "".join(mapping.get(ch, ch) for ch in text)
        return text

    def font(self) -> QFont:
        family = self.get("FontFamily")
        symbol = (family or "").strip().lower() in self.SYMBOL_FONTS
        size = self.get("FontSize") or 12.0
        return theme().font(
            None if symbol else family,
            size * (0.8 if symbol else 1.0),
            self.get("FontWeight") or 400,
            self.get("FontStyle") in ("Italic", "Oblique"),
            self.get("TextOptions.TextRenderingMode") == "Aliased",
        )

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        fm = QFontMetricsF(self.font())
        pad: Thickness = self.get("Padding")
        text = self.text()
        if self.get("TextWrapping") in ("Wrap", "WrapWithOverflow") and math.isfinite(w):
            br = fm.boundingRect(QRectF(0, 0, max(1.0, w - pad.horizontal), 1e6), int(Qt.TextFlag.TextWordWrap), text)
            tw, th = br.width(), max(br.height(), fm.height())
        else:
            tw = fm.horizontalAdvance(text) if text else 0.0
            th = fm.height()
        tw = math.ceil(tw)
        if self.get("TextTrimming") not in (None, "None"):
            tw = min(tw, max(0.0, w - pad.horizontal))
        return tw + pad.horizontal, math.ceil(th) + pad.vertical

    def paint(self, p: QPainter) -> None:
        bg = self.brush("Background", self.rect)
        if bg is not None:
            p.fillRect(self.rect, bg)
        text = self.text()
        if not text:
            return
        pad: Thickness = self.get("Padding")
        area = self.rect.adjusted(pad.left, pad.top, -pad.right, -pad.bottom)
        font = self.font()
        fm = QFontMetricsF(font)
        align = {"Center": Qt.AlignmentFlag.AlignHCenter, "Right": Qt.AlignmentFlag.AlignRight}.get(
            self.get("TextAlignment") or "Left", Qt.AlignmentFlag.AlignLeft
        )
        flags = align | Qt.AlignmentFlag.AlignTop
        if self.get("TextWrapping") in ("Wrap", "WrapWithOverflow"):
            flags |= Qt.TextFlag.TextWordWrap
        elif self.get("TextTrimming") not in (None, "None") and fm.horizontalAdvance(text) > area.width() + 0.5:
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0.0, area.width()))
        fg = self.brush("Foreground", self.rect) or QBrush(QColor("black"))
        p.save()
        p.setFont(font)
        p.setPen(QPen(fg, 1))
        p.drawText(area, int(flags), text)
        p.restore()


class Image(Element):
    TYPE = "Image"

    def _source_size(self) -> QSizeF:
        src = self.get("Source")
        if isinstance(src, QImage) and not src.isNull():
            dpm = src.dotsPerMeterX()
            scale = (dpm * 0.0254 / 96.0) if dpm > 0 else 1.0
            if scale < 0.5 or scale > 4:
                scale = 1.0
            return QSizeF(src.width() / scale, src.height() / scale)
        if isinstance(src, QIcon) and not src.isNull():
            return QSizeF(16, 16)
        return QSizeF(0, 0)

    def _fit(self, size: QSizeF, w: float, h: float) -> QSizeF:
        stretch = self.get("Stretch") or "Uniform"
        if size.width() <= 0 or size.height() <= 0 or stretch == "None":
            return size
        sx = w / size.width() if math.isfinite(w) else INF
        sy = h / size.height() if math.isfinite(h) else INF
        if stretch == "Uniform":
            s = min(sx, sy)
            if not math.isfinite(s):
                s = 1.0
            return QSizeF(size.width() * s, size.height() * s)
        if stretch == "UniformToFill":
            s = max(sx if math.isfinite(sx) else 0, sy if math.isfinite(sy) else 0) or 1.0
            return QSizeF(size.width() * s, size.height() * s)
        return QSizeF(w if math.isfinite(w) else size.width(), h if math.isfinite(h) else size.height())

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        fitted = self._fit(self._source_size(), w, h)
        return fitted.width(), fitted.height()

    def paint(self, p: QPainter) -> None:
        src = self.get("Source")
        if src is None:
            return
        fitted = self._fit(self._source_size(), self.rect.width(), self.rect.height())
        target = QRectF(
            self.rect.x() + (self.rect.width() - fitted.width()) / 2,
            self.rect.y() + (self.rect.height() - fitted.height()) / 2,
            fitted.width(),
            fitted.height(),
        )
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if isinstance(src, QImage):
            p.drawImage(target, src)
        elif isinstance(src, QIcon):
            dpr = max(1.0, abs(p.transform().m11()))
            pix = src.pixmap(QSizeF(target.size()).toSize(), dpr)
            p.drawPixmap(target, pix, QRectF(pix.rect()))
        p.restore()


class Shape(Element):
    TYPE = "Shape"

    def geometry(self) -> QPainterPath | None:
        return None

    def _stroke(self) -> tuple[QPen | None, float]:
        thickness = self.get("StrokeThickness") or 0.0
        brush = self.brush("Stroke", self.rect) if thickness > 0 else None
        if brush is None:
            return None, 0.0
        pen = QPen(brush, thickness)
        cap = {"Round": Qt.PenCapStyle.RoundCap, "Square": Qt.PenCapStyle.SquareCap}.get(
            str(self.get("StrokeStartLineCap") or ""), Qt.PenCapStyle.FlatCap
        )
        pen.setCapStyle(cap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        dashes = self.get("StrokeDashArray")
        if dashes:
            nums = values.split_numbers(str(dashes))
            if nums:
                if len(nums) % 2:
                    nums = nums * 2
                pen.setDashPattern([max(0.01, n) for n in nums])
                pen.setDashOffset(self.get("StrokeDashOffset") or 0.0)
        return pen, thickness

    def _aliased(self) -> bool:
        return self.get("RenderOptions.EdgeMode") == "Aliased"


class Path(Shape):
    TYPE = "Path"
    DEFAULTS = {"Stretch": "None"}

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        geom = self.get("Data")
        if geom is None:
            return 0.0, 0.0
        _, st = self._stroke()
        bounds = geom.boundingRect()
        if (self.get("Stretch") or "None") == "None":
            return max(0.0, bounds.right()) + st / 2, max(0.0, bounds.bottom()) + st / 2
        return min(w, bounds.width() + st) if math.isfinite(w) else bounds.width() + st, min(h, bounds.height() + st) if math.isfinite(h) else bounds.height() + st

    def paint(self, p: QPainter) -> None:
        geom = self.get("Data")
        if geom is None:
            return
        pen, st = self._stroke()
        stretch = self.get("Stretch") or "None"
        transform = QTransform().translate(self.rect.x(), self.rect.y())
        bounds = geom.boundingRect()
        if stretch != "None" and bounds.width() > 0 and bounds.height() > 0:
            avail = self.rect.adjusted(st / 2, st / 2, -st / 2, -st / 2)
            sx, sy = avail.width() / bounds.width(), avail.height() / bounds.height()
            if stretch == "Uniform":
                sx = sy = min(sx, sy)
            elif stretch == "UniformToFill":
                sx = sy = max(sx, sy)
            transform = QTransform().translate(avail.x(), avail.y()).scale(sx, sy).translate(-bounds.x(), -bounds.y())
        path = transform.map(geom)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, not self._aliased())
        fill = brushes.make_brush(self.get("Fill"), path.boundingRect(), self.resolver(), theme().load_image)
        if fill is not None:
            p.fillPath(path, fill)
        if pen is not None:
            p.strokePath(path, pen)
        p.restore()


class Rectangle(Shape):
    TYPE = "Rectangle"
    DEFAULTS = {"Stretch": "Fill"}

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        _, st = self._stroke()
        return st, st

    def shape_path(self) -> QPainterPath:
        _, st = self._stroke()
        r = self.rect.adjusted(st / 2, st / 2, -st / 2, -st / 2)
        path = QPainterPath()
        rx, ry = self.get("RadiusX") or 0.0, self.get("RadiusY") or 0.0
        if rx or ry:
            path.addRoundedRect(r, rx, ry)
        else:
            path.addRect(r)
        return path

    def paint(self, p: QPainter) -> None:
        pen, _ = self._stroke()
        path = self.shape_path()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, not self._aliased() and bool(self.get("RadiusX")))
        fill = self.brush("Fill", self.rect)
        if fill is not None:
            p.fillPath(path, fill)
        if pen is not None:
            p.strokePath(path, pen)
        p.restore()


class Ellipse(Rectangle):
    TYPE = "Ellipse"

    def shape_path(self) -> QPainterPath:
        _, st = self._stroke()
        path = QPainterPath()
        path.addEllipse(self.rect.adjusted(st / 2, st / 2, -st / 2, -st / 2))
        return path

    def paint(self, p: QPainter) -> None:
        pen, _ = self._stroke()
        path = self.shape_path()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, not self._aliased())
        fill = self.brush("Fill", self.rect)
        if fill is not None:
            p.fillPath(path, fill)
        if pen is not None:
            p.strokePath(path, pen)
        p.restore()


def _grid_length(raw: Any) -> tuple[str, float]:
    text = str(raw if raw is not None else "*").strip()
    if text.lower() == "auto":
        return "auto", 0.0
    if text.endswith("*"):
        return "star", float(text[:-1] or 1)
    return "pixel", float(text)


class Grid(Panel):
    TYPE = "Grid"

    def __init__(self, type_name: str | None = None) -> None:
        super().__init__(type_name)
        self.col_defs: list[XObj] = []
        self.row_defs: list[XObj] = []
        self._cols: list[float] = []
        self._rows: list[float] = []

    def _defs(self, defs: list[XObj], size_prop: str) -> list[tuple[str, float]]:
        if not defs:
            return [("star", 1.0)]
        result = []
        for d in defs:
            raw = d.props.get(size_prop, "*") if isinstance(d, XObj) else d
            result.append(_grid_length(raw))
        return result

    def _cell(self, child: Element, ncols: int, nrows: int) -> tuple[int, int, int, int]:
        c = min(max(0, child.get("Grid.Column")), ncols - 1)
        r = min(max(0, child.get("Grid.Row")), nrows - 1)
        cs = max(1, min(child.get("Grid.ColumnSpan"), ncols - c))
        rs = max(1, min(child.get("Grid.RowSpan"), nrows - r))
        return c, r, cs, rs

    @staticmethod
    def _solve(defs: list[tuple[str, float]], content: list[float], avail: float) -> list[float]:
        sizes = []
        for (kind, val), need in zip(defs, content):
            sizes.append(val if kind == "pixel" else need if kind == "auto" else 0.0)
        stars = [i for i, (kind, _) in enumerate(defs) if kind == "star"]
        if stars:
            if math.isfinite(avail):
                remaining = max(0.0, avail - sum(s for i, s in enumerate(sizes) if defs[i][0] != "star"))
                total = sum(defs[i][1] for i in stars) or 1.0
                for i in stars:
                    sizes[i] = remaining * defs[i][1] / total
            else:
                unit = max((content[i] / defs[i][1] for i in stars if defs[i][1] > 0), default=0.0)
                for i in stars:
                    sizes[i] = unit * defs[i][1]
        return sizes

    @staticmethod
    def _span(defs: list[tuple[str, float]], sizes: list[float | None], start: int, span: int) -> float:
        """Space offered to a child across tracks: pixel sizes, auto = unbounded, star = its solved share."""
        total = 0.0
        for i in range(start, start + span):
            kind, value = defs[i]
            if kind == "pixel":
                total += value
            elif kind == "auto" or sizes[i] is None:
                return INF
            else:
                total += sizes[i]  # type: ignore[operator]
        return total

    @staticmethod
    def _star_sizes(defs: list[tuple[str, float]], need: list[float], avail: float) -> list[float | None]:
        fixed = sum(v if k == "pixel" else need[i] for i, (k, v) in enumerate(defs) if k != "star")
        stars = [i for i, (k, _) in enumerate(defs) if k == "star"]
        total = sum(defs[i][1] for i in stars) or 1.0
        sizes: list[float | None] = [None] * len(defs)
        if math.isfinite(avail):
            remaining = max(0.0, avail - fixed)
            for i in stars:
                sizes[i] = remaining * defs[i][1] / total
        return sizes

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        """WPF order: children in auto/pixel tracks first, then star tracks get what is left."""
        cols = self._defs(self.col_defs, "Width")
        rows = self._defs(self.row_defs, "Height")
        col_need = [0.0] * len(cols)
        row_need = [0.0] * len(rows)
        cells = [(child, self._cell(child, len(cols), len(rows))) for child in self.children]

        def has_star(defs: list[tuple[str, float]], start: int, span: int) -> bool:
            return any(defs[i][0] == "star" for i in range(start, start + span))

        def record(c: int, r: int, cs: int, rs: int, dw: float, dh: float) -> None:
            if cs == 1:
                col_need[c] = max(col_need[c], dw)
            if rs == 1:
                row_need[r] = max(row_need[r], dh)

        star_col = [has_star(cols, c, cs) for _, (c, _, cs, _) in cells]
        star_row = [has_star(rows, r, rs) for _, (_, r, _, rs) in cells]
        col_sizes: list[float | None] = [None] * len(cols)
        row_sizes: list[float | None] = [None] * len(rows)

        def measure_group(pick) -> None:
            for i, (child, (c, r, cs, rs)) in enumerate(cells):
                if pick(star_col[i], star_row[i]):
                    dw, dh = child.measure(self._span(cols, col_sizes, c, cs), self._span(rows, row_sizes, r, rs))
                    record(c, r, cs, rs, dw, dh)

        measure_group(lambda sc, sr: not sc and not sr)
        row_sizes = self._star_sizes(rows, row_need, h)
        measure_group(lambda sc, sr: not sc and sr)
        col_sizes = self._star_sizes(cols, col_need, w)
        measure_group(lambda sc, sr: sc)

        def desired(defs: list[tuple[str, float]], need: list[float]) -> float:
            fixed = sum(v if k == "pixel" else need[i] for i, (k, v) in enumerate(defs) if k != "star")
            stars = [i for i, (k, _) in enumerate(defs) if k == "star"]
            if not stars:
                return fixed
            unit = max((need[i] / defs[i][1] for i in stars if defs[i][1] > 0), default=0.0)
            return fixed + unit * sum(defs[i][1] for i in stars)

        self._col_need, self._row_need = col_need, row_need
        return desired(cols, col_need), desired(rows, row_need)

    def arrange_override(self, rect: QRectF) -> None:
        cols = self._defs(self.col_defs, "Width")
        rows = self._defs(self.row_defs, "Height")
        col_need = getattr(self, "_col_need", [0.0] * len(cols))
        row_need = getattr(self, "_row_need", [0.0] * len(rows))
        col_sizes = self._solve(cols, col_need, rect.width())
        row_sizes = self._solve(rows, row_need, rect.height())
        xs = [rect.x()]
        for s in col_sizes:
            xs.append(xs[-1] + s)
        ys = [rect.y()]
        for s in row_sizes:
            ys.append(ys[-1] + s)
        for child in self.children:
            c, r, cs, rs = self._cell(child, len(cols), len(rows))
            child.arrange(xs[c], ys[r], xs[c + cs] - xs[c], ys[r + rs] - ys[r])

    def hit_testable(self) -> bool:
        return self.get("Background") is not None


class StackPanel(Panel):
    TYPE = "StackPanel"
    DEFAULTS = {"Orientation": "Vertical"}

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        horizontal = self.get("Orientation") == "Horizontal"
        main = cross = 0.0
        for child in self.children:
            cw, ch = child.measure(INF if horizontal else w, h if horizontal else INF)
            if horizontal:
                main += cw
                cross = max(cross, ch)
            else:
                main += ch
                cross = max(cross, cw)
        return (main, cross) if horizontal else (cross, main)

    def arrange_override(self, rect: QRectF) -> None:
        horizontal = self.get("Orientation") == "Horizontal"
        pos = rect.x() if horizontal else rect.y()
        for child in self.children:
            cw, ch = child.desired
            if horizontal:
                child.arrange(pos, rect.y(), cw, rect.height())
                pos += cw
            else:
                child.arrange(rect.x(), pos, rect.width(), ch)
                pos += ch

    def hit_testable(self) -> bool:
        return self.get("Background") is not None


class DockPanel(Panel):
    TYPE = "DockPanel"
    DEFAULTS = {"LastChildFill": True}

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        parent_w = parent_h = used_w = used_h = 0.0
        for child in self.children:
            cw, ch = child.measure(max(0.0, w - used_w), max(0.0, h - used_h))
            dock = child.get("DockPanel.Dock")
            if dock in ("Left", "Right"):
                parent_h = max(parent_h, used_h + ch)
                used_w += cw
            else:
                parent_w = max(parent_w, used_w + cw)
                used_h += ch
        return max(parent_w, used_w), max(parent_h, used_h)

    def arrange_override(self, rect: QRectF) -> None:
        left = top = right = bottom = 0.0
        fill = self.get("LastChildFill")
        n = len(self.children)
        for i, child in enumerate(self.children):
            rx, ry = rect.x() + left, rect.y() + top
            rw = max(0.0, rect.width() - left - right)
            rh = max(0.0, rect.height() - top - bottom)
            cw, ch = child.desired
            if fill and i == n - 1:
                child.arrange(rx, ry, rw, rh)
                continue
            dock = child.get("DockPanel.Dock")
            if dock == "Left":
                child.arrange(rx, ry, min(cw, rw), rh)
                left += min(cw, rw)
            elif dock == "Right":
                child.arrange(rx + rw - min(cw, rw), ry, min(cw, rw), rh)
                right += min(cw, rw)
            elif dock == "Top":
                child.arrange(rx, ry, rw, min(ch, rh))
                top += min(ch, rh)
            else:
                child.arrange(rx, ry + rh - min(ch, rh), rw, min(ch, rh))
                bottom += min(ch, rh)

    def hit_testable(self) -> bool:
        return self.get("Background") is not None


class WrapPanel(Panel):
    TYPE = "WrapPanel"
    DEFAULTS = {"Orientation": "Horizontal"}

    def _lines(self, w: float, h: float) -> list[list[Element]]:
        horizontal = self.get("Orientation") == "Horizontal"
        limit = w if horizontal else h
        lines: list[list[Element]] = [[]]
        used = 0.0
        for child in self.children:
            if child.collapsed:
                continue
            size = child.desired[0] if horizontal else child.desired[1]
            if lines[-1] and used + size > limit + 0.01:
                lines.append([])
                used = 0.0
            lines[-1].append(child)
            used += size
        return [line for line in lines if line]

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        horizontal = self.get("Orientation") == "Horizontal"
        for child in self.children:
            child.measure(w, h)
        main = cross = 0.0
        for line in self._lines(w, h):
            line_main = sum(c.desired[0] if horizontal else c.desired[1] for c in line)
            line_cross = max(c.desired[1] if horizontal else c.desired[0] for c in line)
            main = max(main, line_main)
            cross += line_cross
        return (main, cross) if horizontal else (cross, main)

    def arrange_override(self, rect: QRectF) -> None:
        horizontal = self.get("Orientation") == "Horizontal"
        offset = rect.y() if horizontal else rect.x()
        for line in self._lines(rect.width(), rect.height()):
            line_cross = max(c.desired[1] if horizontal else c.desired[0] for c in line)
            pos = rect.x() if horizontal else rect.y()
            for child in line:
                cw, ch = child.desired
                if horizontal:
                    child.arrange(pos, offset, cw, line_cross)
                    pos += cw
                else:
                    child.arrange(offset, pos, line_cross, ch)
                    pos += ch
            offset += line_cross
        for child in self.children:
            if child.collapsed:
                child.arrange(rect.x(), rect.y(), 0, 0)

    def hit_testable(self) -> bool:
        return self.get("Background") is not None


class Canvas(Panel):
    TYPE = "Canvas"

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        for child in self.children:
            child.measure(INF, INF)
        return 0.0, 0.0

    def arrange_override(self, rect: QRectF) -> None:
        for child in self.children:
            cw, ch = child.desired
            x = child.get("Canvas.Left")
            y = child.get("Canvas.Top")
            if x is None or math.isnan(x):
                right = child.get("Canvas.Right")
                x = rect.width() - cw - right if right is not None and not math.isnan(right) else 0.0
            if y is None or math.isnan(y):
                bottom = child.get("Canvas.Bottom")
                y = rect.height() - ch - bottom if bottom is not None and not math.isnan(bottom) else 0.0
            child.arrange(rect.x() + x, rect.y() + y, cw, ch)


class Viewbox(Element):
    TYPE = "Viewbox"

    def measure_override(self, w: float, h: float) -> tuple[float, float]:
        if not self.children:
            return 0.0, 0.0
        cw, ch = self.children[0].measure(INF, INF)
        if cw <= 0 or ch <= 0:
            return 0.0, 0.0
        s = min(w / cw if math.isfinite(w) else INF, h / ch if math.isfinite(h) else INF)
        s = 1.0 if not math.isfinite(s) else s
        self._scale = s
        return cw * s, ch * s

    def arrange_override(self, rect: QRectF) -> None:
        if not self.children:
            return
        child = self.children[0]
        cw, ch = child.desired
        s = min(rect.width() / cw, rect.height() / ch) if cw and ch else 1.0
        self._scale = s
        child.arrange(0, 0, cw, ch)
        self._child_offset = QPointF(rect.x() + (rect.width() - cw * s) / 2, rect.y() + (rect.height() - ch * s) / 2)

    def _render_content(self, p: QPainter) -> None:
        if not self.children:
            return
        p.save()
        p.translate(getattr(self, "_child_offset", self.rect.topLeft()))
        p.scale(getattr(self, "_scale", 1.0), getattr(self, "_scale", 1.0))
        self.children[0].render(p)
        p.restore()


ELEMENT_TYPES: dict[str, type[Element]] = {
    "Border": Border,
    "Grid": Grid,
    "StackPanel": StackPanel,
    "DockPanel": DockPanel,
    "WrapPanel": WrapPanel,
    "Canvas": Canvas,
    "Viewbox": Viewbox,
    "ContentPresenter": ContentPresenter,
    "ScrollContentPresenter": ScrollContentPresenter,
    "ItemsPresenter": ContentPresenter,
    "TextBlock": TextBlock,
    "Label": Control,
    "Image": Image,
    "Path": Path,
    "Rectangle": Rectangle,
    "Ellipse": Ellipse,
    "ContentControl": Control,
    "Button": Control,
    "ToggleButton": Control,
    "RepeatButton": Control,
    "GroupBox": Control,
    "Thumb": Control,
    "UserControl": Control,
    "ItemsControl": Control,
    "ScrollViewer": ScrollViewer,
    "Window": Window,
}
CONTROL_TYPES = {name for name, cls in ELEMENT_TYPES.items() if issubclass(cls, Control)}


def unsupported_types(obj: XObj) -> set[str]:
    """Element types in a template tree that this renderer does not implement."""
    found: set[str] = set()
    if obj.type not in ELEMENT_TYPES:
        found.add(obj.type)
    for child in obj.children:
        if isinstance(child, XObj):
            found |= unsupported_types(child)
    content = obj.props.get("Content")
    if isinstance(content, XObj) and content.type in ELEMENT_TYPES:
        found |= unsupported_types(content)
    return found


def instantiate(obj: XObj, tp: Control | None) -> Element:
    """Create a live element tree from a template's XObj tree."""
    cls = ELEMENT_TYPES.get(obj.type, Panel)
    el = cls(obj.type)
    el.tp = tp
    el.tp_name = obj.name
    el.base = obj.base
    for prop, value in obj.props.items():
        if prop == "ColumnDefinitions" and isinstance(el, Grid):
            el.col_defs = value if isinstance(value, list) else [value]
        elif prop == "RowDefinitions" and isinstance(el, Grid):
            el.row_defs = value if isinstance(value, list) else [value]
        elif prop == "Content" and isinstance(value, XObj) and value.type in ELEMENT_TYPES:
            el.tvals["Content"] = instantiate(value, tp)
        else:
            el.tvals[prop] = value
    element_children = [c for c in obj.children if isinstance(c, XObj)]
    if isinstance(el, Control) and element_children:
        el.tvals["Content"] = instantiate(element_children[0], tp)
    elif isinstance(el, TextBlock):
        if obj.text is not None and "Text" not in el.tvals:
            el.tvals["Text"] = obj.text
    else:
        for child in element_children:
            if child.type in ELEMENT_TYPES or child.type not in ("ColumnDefinition", "RowDefinition"):
                el.add(instantiate(child, tp))
    if isinstance(el, Control) and obj.text is not None and "Content" not in el.tvals:
        el.tvals["Content"] = obj.text
    return el


def make(type_name: str, style: str | None = None, **props: Any) -> Element:
    """Create a host-side element (outside any template), optionally with a DynamicResource style."""
    cls = ELEMENT_TYPES.get(type_name, Panel)
    el = cls(type_name)
    if style:
        el.local["Style"] = ResourceRef(style)
    for key, value in props.items():
        el.local[key.replace("__", ".")] = value
    return el


def parse_value(text: str) -> Any:
    return markup.parse(text)
