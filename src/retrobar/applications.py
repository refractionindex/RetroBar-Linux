"""Installed applications from freedesktop .desktop files: menu folders, search and recent documents."""

from __future__ import annotations

import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import QObject, QTimer, QFileSystemWatcher, pyqtSignal
from PyQt6.QtGui import QIcon

from . import paths

log = logging.getLogger(__name__)

LOST_AND_FOUND = "Lost & Found"
# Freedesktop main categories to Kickoff-style folders; the first match wins.
FOLDERS: list[tuple[str, tuple[str, ...]]] = [
    ("Games", ("Game",)),
    ("Science & Math", ("Science", "Math")),
    ("Development", ("Development",)),
    ("Education", ("Education",)),
    ("Graphics", ("Graphics",)),
    ("Internet", ("Network",)),
    ("Multimedia", ("AudioVideo", "Audio", "Video")),
    ("Office", ("Office",)),
    ("Settings", ("Settings",)),
    ("System", ("System",)),
    ("Utilities", ("Utility", "Accessibility")),
]
_WORD = re.compile(r"[\w']+")


def application_dirs() -> list[Path]:
    """$XDG_DATA_HOME first, then $XDG_DATA_DIRS: earlier directories win."""
    dirs = [paths.xdg_data_home()] + [Path(d) for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    seen: list[Path] = []
    for d in dirs:
        app_dir = d / "applications"
        if app_dir.is_dir() and app_dir not in seen:
            seen.append(app_dir)
    return seen


def read_entry(path: Path) -> dict[str, str]:
    """Keys of the [Desktop Entry] group, including localized ones such as Name[de]."""
    entry: dict[str, str] = {}
    section = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                entry.setdefault(key.strip(), value.strip())
    except OSError:
        pass
    return entry


def locale_keys() -> list[str]:
    """Suffixes to try for localized keys, most specific first (lang_COUNTRY@MODIFIER ... lang)."""
    value = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    value = value.split(".", 1)[0]
    if not value or value in ("C", "POSIX"):
        return []
    lang, _, modifier = value.partition("@")
    language, _, country = lang.partition("_")
    keys = []
    if country and modifier:
        keys.append(f"{language}_{country}@{modifier}")
    if country:
        keys.append(f"{language}_{country}")
    if modifier:
        keys.append(f"{language}@{modifier}")
    keys.append(language)
    return keys


def localized(entry: dict[str, str], key: str, locales: list[str]) -> str:
    for suffix in locales:
        value = entry.get(f"{key}[{suffix}]")
        if value:
            return value
    return entry.get(key, "")


def _split_list(value: str) -> list[str]:
    return [part for part in re.split(r"(?<!\\);", value) if part]


def _bool(value: str) -> bool:
    return value.strip().lower() == "true"


def _executable_exists(command: str) -> bool:
    if os.path.isabs(command):
        return os.access(command, os.X_OK)
    return shutil.which(command) is not None


def folder_for(categories: list[str]) -> str:
    for folder, mains in FOLDERS:
        if any(c in mains for c in categories):
            return folder
    return LOST_AND_FOUND


@dataclass
class Application:
    desktop_id: str
    path: Path
    name: str
    generic_name: str = ""
    comment: str = ""
    keywords: list[str] = field(default_factory=list)
    icon_name: str = ""
    categories: list[str] = field(default_factory=list)
    exec: str = ""
    terminal: bool = False
    _icon: QIcon | None = field(default=None, repr=False, compare=False)

    @property
    def folder(self) -> str:
        return folder_for(self.categories)

    @property
    def executable(self) -> str:
        for token in self.exec.split():
            if "=" in token and not token.startswith("/"):
                continue  # env VAR=value
            if os.path.basename(token) == "env":
                continue
            return os.path.basename(token.strip("\"'"))
        return ""

    @property
    def icon(self) -> QIcon:
        if self._icon is None:
            name = self.icon_name
            if name and os.path.isabs(name) and os.path.isfile(name):
                self._icon = QIcon(name)
            else:
                self._icon = QIcon.fromTheme(name) if name else QIcon()
            if self._icon.isNull():
                self._icon = QIcon.fromTheme("application-x-executable")
        return self._icon


def desktop_id(path: Path, app_dir: Path) -> str:
    """The spec's desktop file ID: the path below applications/ with '/' replaced by '-'."""
    return str(path.relative_to(app_dir)).replace(os.sep, "-")


def scan(dirs: list[Path] | None = None, desktops: list[str] | None = None) -> list[Application]:
    if desktops is None:
        desktops = [d for d in (os.environ.get("XDG_CURRENT_DESKTOP") or "").split(":") if d]
    locales = locale_keys()
    seen: set[str] = set()
    apps: list[Application] = []
    for app_dir in dirs if dirs is not None else application_dirs():
        for path in sorted(app_dir.rglob("*.desktop")):
            did = desktop_id(path, app_dir)
            if did in seen:
                continue
            seen.add(did)  # a hidden user entry still masks the system one
            entry = read_entry(path)
            if entry.get("Type", "Application") != "Application":
                continue
            if _bool(entry.get("NoDisplay", "")) or _bool(entry.get("Hidden", "")):
                continue
            only = _split_list(entry.get("OnlyShowIn", ""))
            if only and not any(d in only for d in desktops):
                continue
            if any(d in _split_list(entry.get("NotShowIn", "")) for d in desktops):
                continue
            try_exec = entry.get("TryExec", "")
            if try_exec and not _executable_exists(try_exec):
                continue
            name = localized(entry, "Name", locales)
            if not name or not entry.get("Exec"):
                continue
            apps.append(
                Application(
                    desktop_id=did,
                    path=path,
                    name=name,
                    generic_name=localized(entry, "GenericName", locales),
                    comment=localized(entry, "Comment", locales),
                    keywords=_split_list(localized(entry, "Keywords", locales)),
                    icon_name=localized(entry, "Icon", locales),
                    categories=_split_list(entry.get("Categories", "")),
                    exec=entry.get("Exec", ""),
                    terminal=_bool(entry.get("Terminal", "")),
                )
            )
    apps.sort(key=lambda a: a.name.casefold())
    return apps


def _score(app: Application, query: str) -> int:
    """Lower is better; -1 means no match."""
    name = app.name.casefold()
    if name.startswith(query):
        return 0
    if any(word.startswith(query) for word in _WORD.findall(name)):
        return 1
    if query in name:
        return 2
    generic = app.generic_name.casefold()
    if generic.startswith(query) or any(w.startswith(query) for w in _WORD.findall(generic)):
        return 3
    if any(k.casefold().startswith(query) for k in app.keywords):
        return 4
    if app.executable.casefold().startswith(query):
        return 5
    if query in generic or any(query in k.casefold() for k in app.keywords) or query in app.comment.casefold():
        return 6
    return -1


def search(apps: list[Application], query: str, limit: int = 15) -> list[Application]:
    query = query.strip().casefold()
    if not query:
        return []
    ranked = [(score, app.name.casefold(), app) for app in apps if (score := _score(app, query)) >= 0]
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [app for _, _, app in ranked[:limit]]


def folders(apps: list[Application]) -> list[tuple[str, list[Application]]]:
    grouped: dict[str, list[Application]] = {}
    for app in apps:
        grouped.setdefault(app.folder, []).append(app)
    names = sorted((n for n in grouped if n != LOST_AND_FOUND), key=str.casefold)
    if LOST_AND_FOUND in grouped:
        names.append(LOST_AND_FOUND)
    return [(n, grouped[n]) for n in names]


@dataclass
class Document:
    path: Path
    modified: str

    @property
    def name(self) -> str:
        return self.path.name


def recent_documents(limit: int = 15, xbel: Path | None = None) -> list[Document]:
    """Most recently used local files from recently-used.xbel that still exist."""
    xbel = xbel or paths.xdg_data_home() / "recently-used.xbel"
    try:
        root = ET.parse(xbel).getroot()
    except (OSError, ET.ParseError):
        return []
    docs: dict[Path, str] = {}
    for bookmark in root.iter("bookmark"):
        url = urlparse(bookmark.get("href", ""))
        if url.scheme != "file":
            continue
        path = Path(unquote(url.path))
        stamp = max(bookmark.get("modified", ""), bookmark.get("visited", ""))
        if stamp > docs.get(path, ""):
            docs[path] = stamp
    ordered = sorted(docs.items(), key=lambda kv: kv[1], reverse=True)
    result = []
    for path, stamp in ordered:
        if path.is_file():
            result.append(Document(path, stamp))
            if len(result) >= limit:
                break
    return result


class Catalog(QObject):
    """Cached application list that rescans after .desktop files change."""

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._apps: list[Application] | None = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._dirty)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(500)
        self._debounce.timeout.connect(self.changed.emit)
        self._watch()

    def _watch(self) -> None:
        dirs = [str(d) for d in application_dirs()]
        for d in list(dirs):
            dirs += [str(p) for p in Path(d).iterdir() if p.is_dir()] if os.access(d, os.R_OK) else []
        missing = [d for d in dirs if d not in self._watcher.directories()]
        if missing:
            self._watcher.addPaths(missing)

    def _dirty(self, _path: str) -> None:
        self._apps = None
        self._debounce.start()

    @property
    def apps(self) -> list[Application]:
        if self._apps is None:
            self._apps = scan()
            self._watch()
            log.debug("Found %d applications", len(self._apps))
        return self._apps

    def folders(self) -> list[tuple[str, list[Application]]]:
        return folders(self.apps)

    def search(self, query: str, limit: int = 15) -> list[Application]:
        return search(self.apps, query, limit)
