"""Every theme and language that ships with RetroBar loads and renders without the Windows runtime."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter

from retrobar.settings import Edge, Settings
from retrobar.theme import Theme
from retrobar.ui import elements
from retrobar.ui.view import TaskbarView, TaskData, TrayData
from retrobar.xaml.resources import ControlTemplate, Style

from .conftest import LANGUAGE_NAMES, THEME_NAMES

KEY_STYLES = ["TaskbarWindow", "Taskbar", "StartButton", "TaskButton", "TaskButtonActive", "TaskButtonFlashing", "Tray", "Clock", "TaskListScrollViewer"]
NOW = datetime(2026, 10, 11, 18, 40, 5)


def load(name: str, language: str = "English") -> Theme:
    theme = Theme()
    theme.load(name, language)
    elements.set_theme(theme)
    return theme


def sample_view(settings: Settings) -> TaskbarView:
    view = TaskbarView(settings)
    icon = QIcon.fromTheme("utilities-terminal")
    view.set_tasks(
        [
            TaskData(key=1, Title="Konsole", Icon=icon, State="Active"),
            TaskData(key=2, Title="A window title that is far too long to fit on a task button", Icon=icon),
            TaskData(key=3, Title="Kate", Icon=icon, State="Flashing"),
        ]
    )
    view.set_tray([TrayData(key="a", Title="Volume", Icon=QIcon.fromTheme("audio-volume-high"))])
    view.tick(NOW)
    return view


def render(view: TaskbarView, width: int, height: int) -> QImage:
    view.layout(width, height)
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    view.render(painter)
    painter.end()
    return image


def distinct_colors(image: QImage, rect) -> int:
    colors = set()
    r = rect.toAlignedRect().intersected(image.rect())
    for y in range(r.top(), r.bottom() + 1, 2):
        for x in range(r.left(), r.right() + 1, 2):
            colors.add(image.pixel(x, y))
    return len(colors)


def test_all_bundled_themes_are_listed():
    assert THEME_NAMES, "no bundled themes found"
    listed = Theme().themes()
    assert listed[0] == "System"
    assert set(THEME_NAMES) <= set(listed)


@pytest.mark.parametrize("name", THEME_NAMES)
def test_theme_loads(name, caplog):
    caplog.set_level(logging.WARNING, logger="retrobar")
    theme = load(name)
    for key in KEY_STYLES:
        style = theme.resource(key)
        assert isinstance(style, Style), f"{name}: {key} did not resolve to a Style"
    # Styles must reach a template through their BasedOn chain.
    for key in ("StartButton", "TaskButton", "Tray"):
        assert isinstance(_template_of(theme.resource(key)), ControlTemplate), f"{name}: {key} has no Template"
    assert theme.number("TaskbarHeight") > 0
    assert theme.number("TaskbarWidth") > 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


def _template_of(style: Style | None):
    while style is not None:
        for setter in style.setters:
            if setter.prop == "Template":
                return setter.value
        style = style.based_on
    return None


@pytest.mark.parametrize("edge", [Edge.BOTTOM, Edge.LEFT])
@pytest.mark.parametrize("name", THEME_NAMES)
def test_theme_renders(name, edge, isolated_dirs, caplog):
    caplog.set_level(logging.WARNING, logger="retrobar")
    load(name)
    settings = Settings(isolated_dirs / "settings.json")
    settings.Edge = int(edge)
    settings.LockTaskbar = True
    view = sample_view(settings)
    thickness = view.thickness()
    assert 20 <= thickness <= 120
    width, height = (1024, thickness) if view.horizontal else (thickness, 600)
    image = render(view, width, height)

    bounds = image.rect().toRectF()
    start = view.to_surface(view.start_button.rect)
    # The Vista orb is taller than the bar on purpose, so only require overlap.
    assert not start.isEmpty() and bounds.intersects(start)
    assert distinct_colors(image, start) > 2, "start button looks blank"
    for task in view.tasks:
        rect = view.task_rect(task.key)
        assert rect is not None and not rect.isEmpty(), f"task {task.key} not laid out"
        assert bounds.contains(rect)
    first, second = (view.task_rect(t.key) for t in view.tasks[:2])
    assert not first.intersects(second)
    clock = view.to_surface(view.clock.rect)
    assert not clock.isEmpty() and bounds.contains(clock)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


@pytest.mark.parametrize("name", ["Windows 95-98", "Windows XP Blue"])
def test_long_titles_are_trimmed_inside_their_button(name, isolated_dirs):
    load(name)
    settings = Settings(isolated_dirs / "settings.json")
    settings.Edge = int(Edge.LEFT)
    view = sample_view(settings)
    render(view, view.thickness(), 600)
    host = view._task_elements[2]
    label = next(e for e in host.walk() if e.type == "TextBlock")
    assert label.rect.right() <= host.rect.right() + 0.5


def test_settings_change_resizes_taskbar(isolated_dirs):
    load("Windows XP Blue")
    settings = Settings(isolated_dirs / "settings.json")
    settings.LockTaskbar = True
    view = TaskbarView(settings)
    one_row = view.thickness()
    settings.RowCount = 2
    assert view.thickness() > one_row
    settings.TaskbarScale = 2.0
    assert view.thickness() >= 2 * one_row


@pytest.mark.parametrize("language", LANGUAGE_NAMES)
def test_language_loads(language, caplog):
    caplog.set_level(logging.WARNING, logger="retrobar")
    theme = load("System", language)
    for key in ("retrobar_properties", "show_desktop", "start_text"):
        value = theme.resource(key)
        assert isinstance(value, str) and value.strip(), f"{language}: {key} missing"
    assert theme.flow_direction() in ("LeftToRight", "RightToLeft")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


def test_system_language_falls_back_to_english():
    theme = load("System", "System")
    assert theme.resource("retrobar_properties") == "RetroBar Properties"


@pytest.mark.parametrize("language", ["English", "Deutsch", "עברית"])
def test_properties_dialog_builds(language, isolated_dirs):
    from retrobar.properties import PropertiesDialog

    theme = load("Windows XP Blue", language)
    settings = Settings(isolated_dirs / "settings.json")
    dialog = PropertiesDialog(settings, theme)
    try:
        assert dialog.windowTitle() == theme.string("retrobar_properties").replace("_", "")
    finally:
        dialog.deleteLater()


def test_user_theme_from_xdg_data_dir(isolated_dirs):
    """A theme dropped into the user's theme folder works like one in %localappdata%\\RetroBar\\Themes."""
    folder = isolated_dirs / "data" / "RetroBar" / "Themes"
    folder.mkdir(parents=True)
    (folder / "Hot Dog Stand.xaml").write_text(
        """<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
    <SolidColorBrush x:Key="ButtonFace" Color="#FFFF0000" />
    <Style TargetType="TextBlock" x:Key="Clock" BasedOn="{StaticResource Clock}">
        <Setter Property="Foreground" Value="Yellow" />
    </Style>
</ResourceDictionary>
""",
        encoding="utf-8",
    )
    theme = Theme()
    assert "Hot Dog Stand" in theme.themes()
    theme.load("Hot Dog Stand", "English")
    elements.set_theme(theme)
    clock = theme.resource("Clock")
    assert isinstance(clock, Style) and clock.based_on is not None
    view = sample_view(Settings(isolated_dirs / "settings.json"))
    image = render(view, 1200, view.thickness())
    # Past the last task button, the taskbar background shows through.
    assert QColor(image.pixel(800, view.thickness() // 2)) == QColor("#FF0000")
