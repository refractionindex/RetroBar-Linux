# Be warned

This project is unstable and crash prone. Use at your own risk.

# RetroBar for Linux (Python)

A Python 3.13 port of RetroBar that runs natively on KDE Plasma (Wayland). It reads the same XAML
themes and language files as the Windows version, so every bundled theme and any third-party
`.xaml` theme work without changes.

## Requirements

- KDE Plasma 6 on Wayland. KWin provides `zwlr_layer_shell_v1` and `org_kde_plasma_window_management`.
- Python 3.13 with PyQt6 and dbus-python from the distribution:
  `sudo apt install python3-pyqt6 python3-dbus`
- `pywayland` (installed by pip from a binary wheel)

## Setup

```sh
cd python
python3 -m venv --copies --system-site-packages .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python tools/install_desktop_file.py            # add --autostart to start with Plasma
```

KWin only gives the window list to programs that a desktop file lists in
`X-KDE-Wayland-Interfaces`, matched by executable path. `install_desktop_file.py` writes
`~/.local/share/applications/retrobar.desktop` for the interpreter that runs it. `--copies` keeps
`.venv/bin/python` a real file, so KWin sees that path rather than `/usr/bin/python3.13`. Without the
desktop file the bar still appears, but it shows no task buttons.

## Running

```sh
.venv/bin/python -m retrobar           # or .venv/bin/retrobar
.venv/bin/python -m retrobar --debug   # verbose logging
```

Only one instance runs per session. It owns `io.github.dremin.RetroBar` on the session bus. Exit
from the taskbar's context menu ("Exit RetroBar") or with `Ctrl+C`.

`retrobar --start-menu` toggles the Start menu of the running instance. To open it from the
keyboard, bind that command to a key in System Settings > Keyboard > Shortcuts > Add New > Command.
Plasma keeps the Meta key for its own launcher.

## Files

| What | Where |
| --- | --- |
| Settings | `$XDG_CONFIG_HOME/retrobar/settings.json` (same keys as the Windows `settings.json`) |
| Extra themes | `$XDG_DATA_HOME/RetroBar/Themes/*.xaml` |
| Extra languages | `$XDG_DATA_HOME/RetroBar/Languages/*.xaml` |
| Bundled themes and languages | `../RetroBar/Themes`, `../RetroBar/Languages` (or `RETROBAR_DATA_DIR`) |

Themes installed from the Properties dialog are copied into the extra-themes folder.

## What maps to what

- **Start button** opens a classic cascading Start menu. It has a banner with the distribution name and a
  search box. Below them are Programs (installed applications in Kickoff-style category folders),
  Documents (recently used files), Settings, Run... (KRunner), Log Off and Shut Down (Plasma's logout
  prompt). Start typing to search by name, generic name, keywords or command; matches cascade beside
  the box. Use the arrow keys, Enter and Escape to move around. Context menus also take arrow keys and
  underlined access keys.
  Clear "Use the classic Start menu" on the Advanced tab of Properties (`ClassicStartMenu`) to open
  the Plasma application launcher instead. A non-empty `StartMenuCommand` overrides both.
- **Task buttons** come from KWin over `org_kde_plasma_window_management`. Left click activates or
  minimizes, middle click follows "middle-click action", right click opens the window menu.
- **Notification area** is a StatusNotifier host. It shows SNI tray icons, their dbusmenu menus and
  their tooltips.
- **Clock** click opens a calendar. A double click opens the Plasma date and time settings.
- **Show desktop** toggles KWin's show-desktop mode.
- **Fonts**: Windows font names in themes map to installed equivalents (Liberation and DejaVu).
  `System` themes use the desktop font.

## Development

```sh
.venv/bin/python -m pytest                              # loads and renders every theme offscreen
.venv/bin/python tools/render_themes.py                 # PNG per theme in /tmp/retrobar-themes
.venv/bin/python tools/render_themes.py --edge left     # vertical layout
.venv/bin/python tools/contact_sheet.py                 # combine them into one image
.venv/bin/python tools/gen_protocols.py                 # regenerate src/retrobar/protocol/
```

The `src/retrobar/` layout:

- `xaml/` parses XAML resource dictionaries, styles, templates, triggers and markup extensions.
- `ui/elements.py` is a small WPF-style element tree with WPF layout and property precedence.
- `ui/brushes.py` and `ui/elements.py` paint it with `QPainter`.
- `ui/view.py` builds the taskbar tree that RetroBar's `Taskbar.xaml` describes.
- `wayland/` holds the layer-shell panel, popups, keyboard input (xkbcommon) and KWin window tracking.
- `ui/menu.py` draws classic menus; `ui/start_menu.py` builds the Start menu on top of them.
- `applications.py` scans `.desktop` files into the Programs folders and ranks search results.
- `tray.py` is the StatusNotifier host. `app.py` wires everything together.

## Limitations

- KDE Plasma on Wayland only. Other compositors lack `org_kde_plasma_window_management`.
- Task buttons cannot be reordered by dragging, and the taskbar cannot be resized or moved by
  dragging. Use Properties to change the edge, rows or width.
- Menus are drawn in the classic style from system colors, not from the theme.
- The Start menu has no Favorites, Find or Help entries, and Programs folders cannot be rearranged.
  Its strings are English unless a language file defines the `start_menu_*` keys.
- Window thumbnails, jump lists and the Windows-only Explorer features are not available.
