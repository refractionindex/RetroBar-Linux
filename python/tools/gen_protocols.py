"""Regenerate the pywayland bindings in src/retrobar/protocol from protocols/*.xml."""

import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
XML_DIR = ROOT / "protocols"
OUT_DIR = ROOT / "src" / "retrobar" / "protocol"
CUSTOM = {
    "wlr-layer-shell-unstable-v1.xml": "wlr_layer_shell_unstable_v1",
    "plasma-window-management.xml": "plasma_window_management",
    "org-kde-plasma-virtual-desktop.xml": "org_kde_plasma_virtual_desktop",
}
# Interfaces shipped by pywayland itself; the generated code must reuse those classes.
SHARED = {"wayland", "xdg_shell"}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        inputs = ["/usr/share/wayland/wayland.xml", str(XML_DIR / "xdg-shell.xml")]
        for name in CUSTOM:
            # plasma-window-management names an argument "is", which is a Python keyword.
            xml = (XML_DIR / name).read_text().replace('name="is"', 'name="id"')
            patched = pathlib.Path(tmp) / name
            patched.write_text(xml)
            inputs.append(str(patched))
        subprocess.run(
            [sys.executable, "-m", "pywayland.scanner", "-i", *inputs, "-o", tmp],
            check=True,
        )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "__init__.py").write_text('"""Generated Wayland protocol bindings. Run tools/gen_protocols.py to refresh."""\n')
        for module in CUSTOM.values():
            source = (pathlib.Path(tmp) / f"{module}.py").read_text()
            for shared in SHARED:
                source = re.sub(rf"^from \.{shared} import", f"from pywayland.protocol.{shared} import", source, flags=re.M)
            (OUT_DIR / f"{module}.py").write_text(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
