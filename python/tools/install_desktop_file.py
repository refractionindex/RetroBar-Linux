"""Write the retrobar.desktop entry KWin needs before it lets RetroBar see other windows.

KWin only exposes org_kde_plasma_window_management to clients whose executable matches a desktop
file listing that interface in X-KDE-Wayland-Interfaces. Run this with the interpreter that will
run RetroBar (for example ``.venv/bin/python tools/install_desktop_file.py``).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

INTERFACES = "org_kde_plasma_window_management,org_kde_plasma_virtual_desktop_management"


def desktop_entry(python: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=RetroBar\n"
        "Comment=Classic Windows taskbar for KDE Plasma\n"
        f"Exec={python} -m retrobar\n"
        "Icon=preferences-desktop-theme\n"
        "Categories=Utility;\n"
        f"X-KDE-Wayland-Interfaces={INTERFACES}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--autostart", action="store_true", help="also start RetroBar when Plasma starts")
    args = parser.parse_args()

    python = os.path.abspath(sys.executable)
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    targets = [data_home / "applications" / "retrobar.desktop"]
    if args.autostart:
        targets.append(config_home / "autostart" / "retrobar.desktop")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(desktop_entry(python), encoding="utf-8")
        print(f"wrote {target}")
    if os.path.islink(python):
        print(f"warning: {python} is a symlink; KWin resolves it, so create the venv with --copies", file=sys.stderr)
    tool = shutil.which("kbuildsycoca6")
    if tool:
        subprocess.run([tool], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
