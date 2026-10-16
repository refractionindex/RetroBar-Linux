"""Theme and language dictionaries: discovery, merge order, and resource lookup (DictionaryManager.cs)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QLocale
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QIcon, QImage, QPalette

from . import paths
from .xaml.markup import ResourceRef, StaticMember
from .xaml.resources import MISSING, ControlTemplate, Loader, ResourceDictionary, Style, XObj

if TYPE_CHECKING:
    from .ui.elements import Control

log = logging.getLogger(__name__)

DICT_DEFAULT = "System"
LANG_FALLBACK = "English"
THEME_FOLDER = "Themes"
LANG_FOLDER = "Languages"
EXT = ".xaml"

# Windows fonts that themes ask for, and metric-compatible or look-alike substitutes.
FONT_SUBSTITUTES = {
    "Microsoft Sans Serif": ["MS Sans Serif", "Liberation Sans", "Arimo", "DejaVu Sans"],
    "MS Sans Serif": ["Microsoft Sans Serif", "Liberation Sans", "DejaVu Sans"],
    "Tahoma": ["Wine Tahoma", "Liberation Sans", "DejaVu Sans"],
    "Segoe UI": ["Selawik", "Noto Sans", "Liberation Sans"],
    "Franklin Gothic": ["Franklin Gothic Medium", "Libre Franklin", "Liberation Sans Narrow", "Liberation Sans"],
    "Franklin Gothic Medium": ["Libre Franklin", "Liberation Sans"],
    "Trebuchet MS": ["Fira Sans", "Liberation Sans"],
    "Verdana": ["DejaVu Sans"],
    "Arial": ["Liberation Sans", "Arimo"],
    "Lucida Console": ["DejaVu Sans Mono", "Liberation Mono"],
}


def install_font_substitutes() -> None:
    for family, subs in FONT_SUBSTITUTES.items():
        QFont.insertSubstitutions(family, subs)


def install_icon_theme() -> None:
    """Make freedesktop icon themes reachable even without a platform theme plugin (e.g. offscreen)."""
    search = list(QIcon.themeSearchPaths())
    data_dirs = [str(paths.xdg_data_home())] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for directory in [os.path.expanduser("~/.icons")] + [os.path.join(d, "icons") for d in data_dirs if d]:
        if directory not in search and os.path.isdir(directory):
            search.append(directory)
    QIcon.setThemeSearchPaths(search)
    if not QIcon.themeName() or QIcon.themeName() == "hicolor":
        QIcon.setThemeName(_kde_icon_theme() or "breeze")
    if not QIcon.fallbackThemeName():
        QIcon.setFallbackThemeName("hicolor")


def _kde_icon_theme() -> str | None:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kdeglobals"
    try:
        section = ""
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Icons]" and line.startswith("Theme="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _system_colors() -> dict[str, QColor]:
    pal = QGuiApplication.palette()
    role = QPalette.ColorRole
    colors = {
        "Control": pal.color(role.Button),
        "ControlLight": pal.color(role.Midlight),
        "ControlLightLight": pal.color(role.Light),
        "ControlDark": pal.color(role.Dark),
        "ControlDarkDark": pal.color(role.Shadow),
        "ControlText": pal.color(role.ButtonText),
        "Highlight": pal.color(role.Highlight),
        "HighlightText": pal.color(role.HighlightedText),
        "Info": pal.color(role.ToolTipBase),
        "InfoText": pal.color(role.ToolTipText),
        "Window": pal.color(role.Base),
        "WindowText": pal.color(role.Text),
        "Menu": pal.color(role.Window),
        "MenuText": pal.color(role.WindowText),
        "MenuHighlight": pal.color(role.Highlight),
        "MenuBar": pal.color(role.Window),
        "GrayText": pal.color(QPalette.ColorGroup.Disabled, role.Text),
        "ActiveCaption": pal.color(role.Highlight),
        "ActiveCaptionText": pal.color(role.HighlightedText),
        "InactiveCaption": pal.color(role.Mid),
        "InactiveCaptionText": pal.color(role.Light),
        "Desktop": pal.color(role.Dark),
        "AppWorkspace": pal.color(role.Mid),
        "HotTrack": pal.color(role.Link),
        "ScrollBar": pal.color(role.Midlight),
        "WindowFrame": pal.color(role.Shadow),
        "ActiveBorder": pal.color(role.Button),
        "InactiveBorder": pal.color(role.Button),
        "GradientActiveCaption": pal.color(role.Highlight),
        "GradientInactiveCaption": pal.color(role.Mid),
    }
    return colors


class Theme:
    def __init__(self, bundled: Path | None = None) -> None:
        self.bundled = bundled or paths.bundled_root()
        self.user_dir = paths.user_data_dir()
        self.stack: list[ResourceDictionary] = []
        self.overrides: dict[Any, Any] = {}
        self.system_dict: ResourceDictionary | None = None
        self.name = ""
        self.language = ""
        self._images: dict[str, QImage | None] = {}
        self._fonts: dict[tuple, QFont] = {}
        self._checked: dict[int, ControlTemplate] = {}
        self._warned: set[str] = set()
        self._colors = _system_colors()

    # --- discovery ----------------------------------------------------------------------------
    def _dirs(self, folder: str) -> list[Path]:
        return [self.bundled / folder, self.user_dir / folder]

    def _list(self, folder: str, default: str) -> list[str]:
        names = [default]
        for directory in self._dirs(folder):
            if directory.is_dir():
                for file in sorted(directory.glob(f"*{EXT}")):
                    if file.stem not in names:
                        names.append(file.stem)
        return names

    def themes(self) -> list[str]:
        return self._list(THEME_FOLDER, DICT_DEFAULT)

    def languages(self) -> list[str]:
        return [DICT_DEFAULT, *self._list(LANG_FOLDER, LANG_FALLBACK)]

    def _find(self, folder: str, name: str) -> Path | None:
        for directory in self._dirs(folder):
            candidate = directory / f"{name}{EXT}"
            if candidate.is_file():
                return candidate
        # Case-insensitive match, e.g. a system locale name that differs in case from the file.
        lowered = name.casefold()
        for directory in self._dirs(folder):
            if directory.is_dir():
                for file in directory.glob(f"*{EXT}"):
                    if file.stem.casefold() == lowered:
                        return file
        return None

    def theme_install_dir(self) -> Path:
        return self.user_dir / THEME_FOLDER

    # --- loading ------------------------------------------------------------------------------
    def _stack_lookup(self, key: Any) -> Any:
        for rd in reversed(self.stack):
            found = rd.lookup(key)
            if found is not MISSING:
                return found
        return MISSING

    def _load_into_stack(self, path: Path) -> ResourceDictionary | None:
        loader = Loader(self.bundled, self._stack_lookup)
        try:
            rd = loader.load_file(path)
        except Exception as exc:  # noqa: BLE001 - a broken theme must not take down the taskbar
            log.error("Error loading dictionary %s: %s", path, exc)
            return None
        self.stack.append(rd)
        return rd

    def system_language_candidates(self) -> list[str]:
        """Names the Windows version would try: the parent culture's native name, then the specific one."""
        loc = QLocale()
        lang = loc.language()
        if lang == QLocale.Language.English:
            territory = loc.nativeTerritoryName()
            return ["English", f"English ({QLocale.territoryToString(loc.territory())})", f"English ({territory})"]
        if lang == QLocale.Language.Chinese:
            simplified = loc.script() == QLocale.Script.SimplifiedHanScript or loc.territory() == QLocale.Country.China
            return ["中文(简体)" if simplified else "中文(繁體)"]
        if lang == QLocale.Language.NorwegianBokmal:
            return ["Norsk (bokmål)"]
        if lang == QLocale.Language.Serbian:
            return ["srpski" if loc.script() == QLocale.Script.LatinScript else "српски"]
        native = loc.nativeLanguageName()
        return [native, f"{native} ({loc.nativeTerritoryName()})"]

    def load(self, theme_name: str, language: str) -> None:
        self.stack = []
        self.overrides = {}
        self._checked = {}
        self._images = {}
        self.name = theme_name
        self.language = language
        self._colors = _system_colors()

        fallback = self._find(LANG_FOLDER, LANG_FALLBACK)
        if fallback:
            self._load_into_stack(fallback)
        candidates = self.system_language_candidates() if language == DICT_DEFAULT else [language]
        for name in candidates:
            if name == LANG_FALLBACK:
                continue
            path = self._find(LANG_FOLDER, name)
            if path:
                self._load_into_stack(path)

        system = self._find(THEME_FOLDER, DICT_DEFAULT)
        if system is None:
            raise FileNotFoundError(f"System.xaml not found under {self.bundled}")
        self.system_dict = self._load_into_stack(system)
        if theme_name.startswith(DICT_DEFAULT):
            self.overrides["GlobalFontFamily"] = QGuiApplication.font().family()
        if theme_name != DICT_DEFAULT:
            path = self._find(THEME_FOLDER, theme_name)
            if path is None:
                log.warning("Theme %r not found; using System", theme_name)
            else:
                self._load_into_stack(path)

    # --- lookup -------------------------------------------------------------------------------
    def lookup(self, key: Any) -> Any:
        if key in self.overrides:
            return self.overrides[key]
        return self._stack_lookup(key)

    def resource(self, key: str, default: Any = None) -> Any:
        found = self.lookup(key)
        return default if found is MISSING else found

    def string(self, key: str, default: str | None = None) -> str:
        value = self.resource(key)
        return str(value) if value is not None else (default if default is not None else key)

    def string_list(self, key: str) -> list[str]:
        """An <x:Array> of strings, e.g. location_values."""
        value = self.resource(key)
        if isinstance(value, XObj):
            return [str(child.text or "") if isinstance(child, XObj) else str(child) for child in value.children]
        if isinstance(value, (list, tuple)):
            return [child.text if isinstance(child, XObj) else str(child) for child in value]
        return []

    def tooltip_text(self, key: str, fallback: str) -> str:
        """Text of a <ToolTip x:Key=...> resource such as StartButtonTip (themes pick their own string)."""
        value = self.resource(key)
        content: Any = value.props.get("Content") if isinstance(value, XObj) else value
        for _ in range(4):
            if isinstance(content, ResourceRef):
                content = self.resource(content.key)
            else:
                break
        if isinstance(content, str) and content:
            return content
        return self.string(fallback)

    def number(self, key: str, default: float = 0.0) -> float:
        value = self.resource(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.resource(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() == "true"
        return default

    def flow_direction(self) -> str:
        value = self.resource("flow_direction")
        return str(value) if value else "LeftToRight"

    def static_member(self, member: StaticMember) -> Any:
        if member.owner == "SystemColors":
            name = member.member
            for suffix in ("BrushKey", "ColorKey", "Brush", "Color"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            color = self._colors.get(name)
            if color is None:
                log.debug("Unknown system color %s", member.member)
                return QColor("black")
            return QColor(color)
        if member.owner == "SystemFonts":
            return QGuiApplication.font().family()
        log.debug("Unsupported x:Static %s.%s", member.owner, member.member)
        return MISSING

    # --- images & fonts -----------------------------------------------------------------------
    def load_image(self, value: Any, base: Any) -> QImage | QIcon | None:
        if value is None:
            return None
        if isinstance(value, (QImage, QIcon)):
            return value
        if isinstance(value, XObj):
            if value.type == "BitmapImage":
                return self.load_image(value.props.get("UriSource"), value.base or base)
            return None
        uri = str(value).strip()
        if uri.startswith("pack://application:,,,"):
            uri = uri[len("pack://application:,,,") :]
            if "component/" not in uri:
                base = self.bundled
                uri = uri.lstrip("/")
        path = Loader(self.bundled).resolve_uri(uri, Path(base) if base else None)
        if path is None:
            return None
        key = str(path)
        if key not in self._images:
            image = QImage(key)
            if image.isNull():
                log.warning("Could not load image %s", key)
                self._images[key] = None
            else:
                self._images[key] = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                self._images[key].setDotsPerMeterX(image.dotsPerMeterX())
                self._images[key].setDotsPerMeterY(image.dotsPerMeterY())
        return self._images[key]

    def font(self, family: str | None, size: float, weight: int, italic: bool, aliased: bool) -> QFont:
        key = (family, round(size, 2), weight, italic, aliased)
        font = self._fonts.get(key)
        if font is None:
            font = QFont(family) if family else QFont(QGuiApplication.font())
            font.setPixelSize(max(1, round(size)))
            font.setWeight(QFont.Weight(min(900, max(100, round(weight / 100) * 100))))
            font.setItalic(italic)
            font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            if aliased:
                font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
            self._fonts[key] = font
        return font

    def checked_template(self, template: ControlTemplate, control: Control) -> ControlTemplate:
        """Fall back to System.xaml's template when a theme template uses unsupported elements."""
        cached = self._checked.get(id(template))
        if cached is not None:
            return cached
        from .ui.elements import unsupported_types

        result = template
        missing = unsupported_types(template.root) if template.root is not None else set()
        if missing:
            style = control.style
            fallback = None
            if style is not None and style.key is not None and self.system_dict is not None:
                base_style = self.system_dict.lookup(style.key)
                if isinstance(base_style, Style):
                    setter = base_style.all_setters().get("Template")
                    if setter is not None and isinstance(setter.value, ControlTemplate):
                        fallback = setter.value
            warn_key = f"{style.key if style else control.type}:{sorted(missing)}"
            if warn_key not in self._warned:
                self._warned.add(warn_key)
                log.warning(
                    "Template for %s uses unsupported elements %s; %s",
                    style.key if style else control.type,
                    ", ".join(sorted(missing)),
                    "using the System template" if fallback else "rendering them as empty panels",
                )
            if fallback is not None:
                result = fallback
        self._checked[id(template)] = result
        return result
