"""Installing community theme packs that ship Themes/ + Resources/."""

from __future__ import annotations

from pathlib import Path

from retrobar.theme import Theme
from retrobar.xaml.values import Thickness


def _pack(tmp_path: Path) -> Path:
    root = tmp_path / "pack"
    (root / "Themes").mkdir(parents=True)
    (root / "Resources").mkdir()
    (root / "Themes" / "Demo Aero.xaml").write_text(
        """<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
                    xmlns:system="clr-namespace:System;assembly=mscorlib">
    <system:Boolean x:Key="UseFloatingStartButton">True</system:Boolean>
    <system:Double x:Key="TaskbarHeight">40</system:Double>
    <BitmapImage x:Key="TaskbarImage" UriSource="../Resources/glass.png" />
    <Style TargetType="ToggleButton" x:Key="StartButton">
        <Setter Property="Width" Value="60" />
        <Setter Property="Height" Value="58" />
        <Setter Property="Margin" Value="-3,-8,11,-8" />
    </Style>
</ResourceDictionary>
"""
    )
    from PyQt6.QtGui import QColor, QImage

    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor(70, 160, 220, 180))
    image.save(str(root / "Resources" / "glass.png"))
    return root


def test_install_theme_pack_copies_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    theme = Theme()
    installed = theme.install_theme(_pack(tmp_path))
    assert installed == ["Demo Aero"]
    assert (theme.theme_install_dir() / "Demo Aero.xaml").is_file()
    assert (theme.resources_dir() / "glass.png").is_file()
    theme.load("Demo Aero", "English")
    image = theme.load_image(theme.resource("TaskbarImage"), None)
    assert image is not None and not image.isNull()
    assert theme.start_button_overflow(40) == Thickness(10.0, 9.0, 10.0, 9.0)


def test_install_single_xaml_finds_sibling_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    pack = _pack(tmp_path)
    theme = Theme()
    installed = theme.install_theme(pack / "Themes" / "Demo Aero.xaml")
    assert installed == ["Demo Aero"]
    assert (theme.resources_dir() / "glass.png").is_file()
