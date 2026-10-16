"""Markup extensions: {DynamicResource}, {StaticResource}, {Binding}, {TemplateBinding}, {x:Static}, ..."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class Extension:
    name: str
    positional: tuple[Any, ...] = ()
    named: tuple[tuple[str, Any], ...] = ()

    def get(self, key: str, index: int | None = None, default: Any = None) -> Any:
        for k, v in self.named:
            if k == key:
                return v
        if index is not None and index < len(self.positional):
            return self.positional[index]
        return default


@dataclass(frozen=True)
class ResourceRef:
    key: Any
    dynamic: bool = True


@dataclass(frozen=True)
class TemplateBindingRef:
    prop: str


@dataclass(frozen=True)
class StaticMember:
    """{x:Static Owner.Member}"""

    owner: str
    member: str


NULL = object()  # {x:Null}


@dataclass(frozen=True)
class Binding:
    path: str = ""
    source: str = "datacontext"  # datacontext | self | window | templated_parent
    string_format: str | None = None
    extra: tuple[tuple[str, Any], ...] = field(default=())


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def expect(self, ch: str) -> None:
        self.skip_ws()
        if self.peek() != ch:
            raise ValueError(f"Expected {ch!r} at {self.pos} in {self.text!r}")
        self.pos += 1

    def parse_extension(self) -> Extension:
        self.expect("{")
        self.skip_ws()
        start = self.pos
        while self.pos < len(self.text) and not self.text[self.pos].isspace() and self.text[self.pos] not in ",}":
            self.pos += 1
        name = self.text[start : self.pos]
        positional: list[Any] = []
        named: list[tuple[str, Any]] = []
        while True:
            self.skip_ws()
            ch = self.peek()
            if ch == "}":
                self.pos += 1
                break
            if ch == ",":
                self.pos += 1
                continue
            if not ch:
                raise ValueError(f"Unterminated markup extension: {self.text!r}")
            value = self.parse_value()
            self.skip_ws()
            if self.peek() == "=" and isinstance(value, str):
                self.pos += 1
                self.skip_ws()
                named.append((value.strip(), self.parse_value()))
            else:
                positional.append(value)
        return Extension(name, tuple(positional), tuple(named))

    def parse_value(self) -> Any:
        self.skip_ws()
        ch = self.peek()
        if ch == "{":
            return self.parse_extension()
        if ch in "'\"":
            quote = ch
            self.pos += 1
            start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] != quote:
                self.pos += 1
            value = self.text[start : self.pos]
            self.pos += 1
            return value
        start = self.pos
        depth = 0
        while self.pos < len(self.text):
            c = self.text[self.pos]
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    break
                depth -= 1
            elif c in ",=" and depth == 0:
                break
            self.pos += 1
        return self.text[start : self.pos].strip()


def _type_name(value: Any) -> str:
    if isinstance(value, Extension) and value.name in ("x:Type", "Type"):
        value = value.get("TypeName", 0, "")
    return str(value).split(":")[-1]


def _binding(ext: Extension) -> Binding:
    path = str(ext.get("Path", 0, "") or "")
    source = "datacontext"
    rel = ext.get("RelativeSource")
    if isinstance(rel, Extension):
        mode = str(rel.get("Mode", 0, "") or "")
        if mode == "Self":
            source = "self"
        elif mode == "TemplatedParent":
            source = "templated_parent"
        elif mode == "FindAncestor":
            source = "ancestor:" + _type_name(rel.get("AncestorType", None, "Window"))
    fmt = ext.get("StringFormat")
    if isinstance(fmt, str) and fmt.startswith("{}"):
        fmt = fmt[2:]
    return Binding(path=path, source=source, string_format=fmt)


def _convert(ext: Extension) -> Any:
    name = ext.name.split(":")[-1] if ext.name.startswith("x:") else ext.name
    if name in ("DynamicResource", "StaticResource"):
        key = ext.get("ResourceKey", 0)
        if isinstance(key, Extension):
            key = _convert(key)
        return ResourceRef(key, dynamic=name == "DynamicResource")
    if name == "TemplateBinding":
        prop = str(ext.get("Property", 0, ""))
        return TemplateBindingRef(prop.split(".")[-1])
    if name == "Binding":
        return _binding(ext)
    if name == "Null":
        return NULL
    if name == "Static":
        member = str(ext.get("Member", 0, ""))
        owner, _, member_name = member.rpartition(".")
        return StaticMember(owner.split(":")[-1], member_name)
    if name == "Type":
        return _type_name(ext)
    return ext


@lru_cache(maxsize=4096)
def parse(text: str) -> Any:
    """Return a converted extension object for markup text, or the plain string."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    if stripped.startswith("{}"):
        return stripped[2:]
    return _convert(_Parser(stripped).parse_extension())
