from __future__ import annotations

import json
from datetime import datetime

import pytest
from PyQt6.QtCore import QPointF

from retrobar import dotnet_format
from retrobar.settings import DEFAULTS, Edge, Settings
from retrobar.theme import Theme
from retrobar.ui import elements
from retrobar.xaml import markup, values
from retrobar.xaml.markup import NULL, Binding, ResourceRef, StaticMember, TemplateBindingRef

WHEN = datetime(2026, 10, 11, 18, 4, 5, 123000)


@pytest.fixture(autouse=True)
def reset_clock_pattern():
    yield
    dotnet_format.set_short_time_pattern(None)


# --- .NET date formats ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("ddd MMM d  h:mm:ss tt", "Sun Oct 11  6:04:05 PM"),
        ("dddd, MMMM dd yyyy", "Sunday, October 11 2026"),
        ("HH:mm", "18:04"),
        ("hh:mm t", "06:04 P"),
        ("M/d/yy", "10/11/26"),
        ("'at' H\\h fff", "at 18h 123"),
    ],
)
def test_custom_formats(fmt, expected):
    assert dotnet_format.format_datetime(WHEN, fmt) == expected


def test_standard_formats_follow_locale():
    assert dotnet_format.format_datetime(WHEN, "t") == "6:04 PM"
    assert dotnet_format.format_datetime(WHEN, "T") == "6:04:05 PM"
    assert dotnet_format.format_datetime(WHEN, "d") == "10/11/26"
    assert "October" in dotnet_format.format_datetime(WHEN, "D")


def test_short_time_override_and_designators():
    dotnet_format.set_short_time_pattern("h:mm tt", am="a.m.", pm="p.m.")
    assert dotnet_format.format_datetime(WHEN, "t") == "6:04 p.m."
    dotnet_format.set_short_time_pattern("T")
    assert dotnet_format.format_datetime(WHEN, "t") == "6:04:05 PM"


# --- settings ------------------------------------------------------------------------------------


def test_settings_defaults_and_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    assert s.Theme == DEFAULTS["Theme"]
    assert s.edge is Edge.BOTTOM
    seen = []
    s.subscribe(seen.append)
    s.Theme = "Windows XP Blue"
    s.Edge = int(Edge.LEFT)
    s.Edge = int(Edge.LEFT)  # unchanged values do not notify
    assert seen == ["Theme", "Edge"]
    again = Settings(path)
    assert again.Theme == "Windows XP Blue"
    assert again.edge is Edge.LEFT


def test_settings_accept_windows_file(tmp_path):
    """A settings.json written by the Windows version (BOM, extra keys, ints for doubles) loads and survives saving."""
    path = tmp_path / "settings.json"
    data = {"Theme": "Windows 2000", "TaskbarScale": 2, "AutoHide": "yes", "PinnedQuickLaunchItems": ["a.lnk"]}
    path.write_text("\ufeff" + json.dumps(data), encoding="utf-8")
    s = Settings(path)
    assert s.Theme == "Windows 2000"
    assert s.TaskbarScale == 2.0 and isinstance(s.TaskbarScale, float)
    assert s.AutoHide is DEFAULTS["AutoHide"]  # wrong type is ignored
    s.LockTaskbar = not s.LockTaskbar
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["PinnedQuickLaunchItems"] == ["a.lnk"]


def test_unreadable_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert Settings(path).Theme == DEFAULTS["Theme"]


# --- XAML values and markup ----------------------------------------------------------------------


def test_thickness_and_corner_radius():
    assert values.Thickness.parse("1") == values.Thickness(1, 1, 1, 1)
    assert values.Thickness.parse("1,2") == values.Thickness(1, 2, 1, 2)
    assert values.Thickness.parse("1 2 3 4") == values.Thickness(1, 2, 3, 4)
    assert values.CornerRadius.parse("3,0,0,3").mirrored() == values.CornerRadius(0, 3, 3, 0)


def test_colors():
    assert values.parse_color("#80FF0000").alpha() == 0x80
    assert values.parse_color("#F00").red() == 255
    assert values.parse_color("Yellow").name() == "#ffff00"


def test_geometry_path_data():
    path = values.parse_geometry("M 0,0 L 10,0 L 10,10 Z")
    assert path.boundingRect().width() == pytest.approx(10)
    arc = values.parse_geometry("M0,5 A5,5 0 0 1 10,5")
    assert arc.boundingRect().height() == pytest.approx(5, abs=0.1)
    assert values.parse_geometry("F1 M 0,0 h 4 v 4 H 0 z").contains(QPointF(2, 2))


def test_markup_extensions():
    assert markup.parse("{DynamicResource ButtonFace}") == ResourceRef("ButtonFace")
    assert markup.parse("{StaticResource Clock}") == ResourceRef("Clock", dynamic=False)
    assert markup.parse("{TemplateBinding Background}") == TemplateBindingRef("Background")
    assert markup.parse("{x:Static SystemColors.ControlBrushKey}") == StaticMember("SystemColors", "ControlBrushKey")
    assert markup.parse("{x:Null}") is NULL
    assert markup.parse("{}{0:t}") == "{0:t}"
    binding = markup.parse("{Binding Path=Orientation, RelativeSource={RelativeSource AncestorType=Window, Mode=FindAncestor}}")
    assert binding == Binding(path="Orientation", source="ancestor:Window")
    assert markup.parse("plain text") == "plain text"


# --- layout --------------------------------------------------------------------------------------


def test_grid_star_column_gets_space_left_by_auto_column():
    theme = Theme()
    theme.load("System", "English")
    elements.set_theme(theme)
    grid = elements.make("Grid")
    grid.col_defs = ["Auto", "*"]
    icon = elements.make("Border", Width=16.0, Grid__Column=0)
    label = elements.make("TextBlock", Text="A title much longer than the space available", TextTrimming="CharacterEllipsis", Grid__Column=1)
    grid.add(icon)
    grid.add(label)
    elements.invalidate()
    width, _ = grid.measure(100, 20)
    assert width <= 100
    grid.arrange(0, 0, 100, 20)
    assert icon.rect.width() == 16
    assert label.rect.left() == 16
    assert label.rect.right() <= 100
