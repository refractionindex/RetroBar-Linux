"""Load XAML ResourceDictionary files into styles, templates and resource values."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import markup, values
from .markup import NULL, ResourceRef

log = logging.getLogger(__name__)

X_NS = "http://schemas.microsoft.com/winfx/2006/xaml"
MISSING = object()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class XObj:
    """A XAML object element whose interpretation is deferred to the code that uses it."""

    type: str
    props: dict[str, Any] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)
    text: str | None = None
    base: Path | None = None
    name: str | None = None

    def get(self, prop: str, default: Any = None) -> Any:
        return self.props.get(prop, default)


@dataclass
class Setter:
    prop: str
    value: Any
    target: str | None = None
    base: Path | None = None


@dataclass
class Condition:
    prop: str | None = None
    binding: markup.Binding | None = None
    value: Any = None


@dataclass
class Trigger:
    conditions: list[Condition]
    setters: list[Setter]


@dataclass
class ControlTemplate:
    target_type: str
    root: XObj | None
    triggers: list[Trigger]
    key: Any = None
    source: Path | None = None


@dataclass
class Style:
    target_type: str
    setters: list[Setter]
    triggers: list[Trigger]
    based_on: Style | None = None
    key: Any = None
    source: Path | None = None

    def all_setters(self) -> dict[str, Setter]:
        """Setters with BasedOn applied; derived styles override their base."""
        cached = getattr(self, "_setter_map", None)
        if cached is not None:
            return cached
        result = dict(self.based_on.all_setters()) if self.based_on else {}
        for setter in self.setters:
            result[setter.prop] = setter
        object.__setattr__(self, "_setter_map", result)
        return result

    def all_triggers(self) -> list[Trigger]:
        base = self.based_on.all_triggers() if self.based_on else []
        return base + self.triggers


# Properties named with an owner prefix that should be treated as the plain property.
_OWNERS = {
    "Control",
    "FrameworkElement",
    "UIElement",
    "TextElement",
    "TextBlock",
    "Border",
    "Panel",
    "Shape",
    "Path",
    "Image",
    "ButtonBase",
    "ToggleButton",
    "Button",
    "ContentControl",
    "ContentPresenter",
    "Window",
    "Grid",
    "Rectangle",
    "StackPanel",
}
_ATTACHED_KEEP = {"Grid.Row", "Grid.Column", "Grid.RowSpan", "Grid.ColumnSpan", "DockPanel.Dock", "Canvas.Left", "Canvas.Top", "Canvas.Right", "Canvas.Bottom"}


def normalize_prop(name: str) -> str:
    name = name.strip()
    if name.startswith("(") and name.endswith(")"):
        name = name[1:-1]
    if "." in name and name not in _ATTACHED_KEEP:
        owner, _, prop = name.partition(".")
        if owner in _OWNERS:
            return prop
    return name


# Typed values whose text content is converted when the dictionary is loaded.
def _text_value(type_name: str, text: str, obj: XObj) -> Any:
    text = text.strip()
    match type_name:
        case "Double" | "Single":
            return values.parse_double(text)
        case "Int32" | "Int64" | "Int16":
            return int(text)
        case "Boolean":
            return values.parse_bool(text)
        case "String":
            return text
        case "Thickness":
            return values.Thickness.parse(text)
        case "CornerRadius":
            return values.CornerRadius.parse(text)
        case "Color":
            return values.parse_color(text)
        case "FontWeight":
            return values.parse_font_weight(text)
        case "FontFamily" | "FontStyle" | "FlowDirection" | "Visibility" | "HorizontalAlignment" | "VerticalAlignment" | "Orientation":
            return text
        case "Geometry" | "PathGeometry" | "StreamGeometry":
            return values.parse_geometry(text)
    return text


def _attr_object_value(obj: XObj) -> Any:
    """Objects like <Thickness Right="2"/> defined with attributes instead of text."""
    if obj.type == "Thickness":
        g = lambda k: values.parse_double(obj.props.get(k, "0"))  # noqa: E731
        return values.Thickness(g("Left"), g("Top"), g("Right"), g("Bottom"))
    return obj


class ResourceDictionary:
    def __init__(self, source: Path | None = None) -> None:
        self.source = source
        self.entries: dict[Any, Any] = {}
        self.merged: list[ResourceDictionary] = []

    def lookup(self, key: Any) -> Any:
        if key in self.entries:
            return self.entries[key]
        for merged in reversed(self.merged):
            found = merged.lookup(key)
            if found is not MISSING:
                return found
        return MISSING

    def keys(self) -> set[Any]:
        result = set(self.entries)
        for merged in self.merged:
            result |= merged.keys()
        return result


Lookup = Callable[[Any], Any]


class Loader:
    """Builds ResourceDictionary objects. ``parent_lookup`` emulates application resources that
    were merged earlier, which WPF consults when resolving StaticResource at load time."""

    def __init__(self, bundled_root: Path, parent_lookup: Lookup | None = None) -> None:
        self.bundled_root = bundled_root
        self.parent_lookup = parent_lookup or (lambda key: MISSING)
        self._loading: set[Path] = set()

    # --- files -----------------------------------------------------------------------------
    def load_file(self, path: Path) -> ResourceDictionary:
        path = path.resolve()
        if path in self._loading:
            raise ValueError(f"Circular MergedDictionaries reference: {path}")
        self._loading.add(path)
        try:
            root = ET.parse(path).getroot()
            if local_name(root.tag) != "ResourceDictionary":
                raise ValueError(f"{path.name} is not a ResourceDictionary")
            rd = ResourceDictionary(path)
            self._fill_dictionary(rd, root, path.parent, self.parent_lookup)
            return rd
        finally:
            self._loading.discard(path)

    def resolve_uri(self, uri: str, base: Path | None) -> Path | None:
        uri = unquote(uri.strip())
        if "component/" in uri:
            return self.bundled_root / uri.split("component/", 1)[1]
        if uri.startswith("file://"):
            return Path(uri[7:])
        candidate = Path(uri.replace("\\", "/"))
        if candidate.is_absolute():
            return candidate
        if base is not None:
            return (base / candidate).resolve()
        return (self.bundled_root / candidate).resolve()

    # --- dictionary content ----------------------------------------------------------------
    def _fill_dictionary(self, rd: ResourceDictionary, node: ET.Element, base: Path, outer: Lookup) -> None:
        def lookup(key: Any) -> Any:
            found = rd.lookup(key)
            return found if found is not MISSING else outer(key)

        for child in node:
            tag = local_name(child.tag)
            if tag == "ResourceDictionary.MergedDictionaries":
                for md in child:
                    src = md.get("Source")
                    if src:
                        path = self.resolve_uri(src, base)
                        if path is None or not path.is_file():
                            log.warning("Merged dictionary not found: %s (from %s)", src, rd.source)
                            continue
                        sub = ResourceDictionary(path)
                        sub_root = ET.parse(path).getroot()
                        self._fill_dictionary(sub, sub_root, path.parent, lookup)
                        rd.merged.append(sub)
                    else:
                        sub = ResourceDictionary(rd.source)
                        self._fill_dictionary(sub, md, base, lookup)
                        rd.merged.append(sub)
                continue
            if "." in tag:
                continue
            key = child.get(f"{{{X_NS}}}Key")
            obj = self.build(child, base, lookup)
            if key is None:
                if isinstance(obj, Style):
                    key = ("type", obj.target_type)
                elif isinstance(obj, ControlTemplate):
                    key = ("template", obj.target_type)
                else:
                    continue
            else:
                key = self._key(key)
            if isinstance(obj, (Style, ControlTemplate)):
                obj.key = key
            rd.entries[key] = obj

    @staticmethod
    def _key(raw: str) -> Any:
        parsed = markup.parse(raw)
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, markup.Extension) and parsed.name in ("x:Type", "Type"):
            return ("type", str(parsed.get("TypeName", 0, "")).split(":")[-1])
        return parsed

    # --- objects ---------------------------------------------------------------------------
    def build(self, node: ET.Element, base: Path, lookup: Lookup) -> Any:
        tag = local_name(node.tag)
        if tag == "Style":
            return self._style(node, base, lookup)
        if tag == "ControlTemplate":
            return self._template(node, base, lookup)
        if tag == "ResourceDictionary":
            rd = ResourceDictionary(None)
            self._fill_dictionary(rd, node, base, lookup)
            return rd
        obj = self._xobj(node, base, lookup)
        if obj.text is not None and not obj.children and not any(k for k in obj.props if k != "Name"):
            return _text_value(obj.type, obj.text, obj)
        return _attr_object_value(obj)

    def _value(self, raw: str, lookup: Lookup) -> Any:
        parsed = markup.parse(raw)
        if isinstance(parsed, ResourceRef) and not parsed.dynamic:
            found = lookup(parsed.key)
            if found is MISSING:
                log.debug("StaticResource %r not found at load time; resolving dynamically", parsed.key)
                return ResourceRef(parsed.key, dynamic=True)
            return found
        return parsed

    def _xobj(self, node: ET.Element, base: Path, lookup: Lookup) -> XObj:
        obj = XObj(local_name(node.tag), base=base)
        for attr, raw in node.attrib.items():
            name = local_name(attr)
            if attr.startswith(f"{{{X_NS}}}"):
                if name == "Name":
                    obj.name = raw
                continue
            if name == "Name":
                obj.name = raw
                continue
            obj.props[normalize_prop(name)] = self._value(raw, lookup)
        for child in node:
            ctag = local_name(child.tag)
            if "." in ctag:
                prop = normalize_prop(ctag.split(".", 1)[1]) if ctag.split(".", 1)[0] == obj.type else normalize_prop(ctag)
                items = [self.build(c, base, lookup) for c in child]
                if child.text and child.text.strip() and not items:
                    obj.props[prop] = self._value(child.text.strip(), lookup)
                elif len(items) == 1 and prop not in ("ColumnDefinitions", "RowDefinitions", "Children", "GradientStops"):
                    obj.props[prop] = items[0]
                else:
                    obj.props[prop] = items
            else:
                obj.children.append(self.build(child, base, lookup))
        if node.text and node.text.strip() and not len(node):
            obj.text = node.text.strip()
        return obj

    def _setters(self, nodes: list[ET.Element], base: Path, lookup: Lookup) -> list[Setter]:
        result = []
        for node in nodes:
            if local_name(node.tag) != "Setter":
                continue
            prop = normalize_prop(node.get("Property", ""))
            raw = node.get("Value")
            if raw is not None:
                value = self._value(raw, lookup)
            else:
                value_nodes = [c for c in node if local_name(c.tag) == "Setter.Value"]
                inner = list(value_nodes[0]) if value_nodes else []
                value = self.build(inner[0], base, lookup) if inner else None
            result.append(Setter(prop, value, node.get("TargetName"), base))
        return result

    def _condition_value(self, raw: str | None) -> Any:
        if raw is None:
            return None
        parsed = markup.parse(raw)
        return parsed

    def _triggers(self, container: ET.Element | None, base: Path, lookup: Lookup) -> list[Trigger]:
        if container is None:
            return []
        triggers = []
        for node in container:
            tag = local_name(node.tag)
            setters_nodes: list[ET.Element] = []
            conditions: list[Condition] = []
            for child in node:
                ctag = local_name(child.tag)
                if ctag.endswith(".Setters"):
                    setters_nodes.extend(child)
                elif ctag.endswith(".Conditions"):
                    for cond in child:
                        binding = cond.get("Binding")
                        conditions.append(
                            Condition(
                                prop=normalize_prop(cond.get("Property")) if cond.get("Property") else None,
                                binding=markup.parse(binding) if binding else None,
                                value=self._condition_value(cond.get("Value")),
                            )
                        )
                elif ctag == "Setter":
                    setters_nodes.append(child)
            if tag == "Trigger":
                conditions = [Condition(prop=normalize_prop(node.get("Property", "")), value=self._condition_value(node.get("Value")))]
            elif tag == "DataTrigger":
                conditions = [Condition(binding=markup.parse(node.get("Binding", "")), value=self._condition_value(node.get("Value")))]
            elif tag not in ("MultiTrigger", "MultiDataTrigger"):
                log.debug("Ignoring unsupported trigger type %s", tag)
                continue
            triggers.append(Trigger(conditions, self._setters(setters_nodes, base, lookup)))
        return triggers

    def _style(self, node: ET.Element, base: Path, lookup: Lookup) -> Style:
        target = self._target_type(node.get("TargetType"))
        based_on = None
        if node.get("BasedOn"):
            parsed = self._value(node.get("BasedOn"), lookup)
            if isinstance(parsed, Style):
                based_on = parsed
            elif isinstance(parsed, ResourceRef):
                found = lookup(parsed.key)
                based_on = found if isinstance(found, Style) else None
                if based_on is None:
                    log.warning("BasedOn style %r not found", parsed.key)
        setters = self._setters(list(node), base, lookup)
        triggers_node = next((c for c in node if local_name(c.tag) == "Style.Triggers"), None)
        style = Style(target, setters, self._triggers(triggers_node, base, lookup), based_on, source=base)
        return style

    def _template(self, node: ET.Element, base: Path, lookup: Lookup) -> ControlTemplate:
        target = self._target_type(node.get("TargetType"))
        root = None
        triggers_node = None
        for child in node:
            tag = local_name(child.tag)
            if tag == "ControlTemplate.Triggers":
                triggers_node = child
            elif tag == "ControlTemplate.Resources":
                continue
            elif root is None:
                built = self.build(child, base, lookup)
                root = built if isinstance(built, XObj) else None
        return ControlTemplate(target, root, self._triggers(triggers_node, base, lookup), source=base)

    @staticmethod
    def _target_type(raw: str | None) -> str:
        if not raw:
            return ""
        parsed = markup.parse(raw)
        return str(parsed).split(":")[-1] if not isinstance(parsed, markup.Extension) else str(parsed.get("TypeName", 0, "")).split(":")[-1]


__all__ = [
    "MISSING",
    "NULL",
    "Condition",
    "ControlTemplate",
    "Loader",
    "ResourceDictionary",
    "Setter",
    "Style",
    "Trigger",
    "XObj",
    "normalize_prop",
]
