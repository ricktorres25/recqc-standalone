#!/usr/bin/env python3
# build_icons.py - render the installed app's icons (M5) from the page's brand mark.
#
# The mark is the mic in index.html's header (STYLE_GUIDE.md: the accent tints the brand
# mark), drawn in the dark theme's colours on the ground tone. Headless Chrome renders
# each icon to PNG, so nothing beyond the standard library and Chrome is needed.
#
#   icons/icon-192.png, icons/icon-512.png   "any": a rounded tile on the macOS icon
#       grid (824 of 1024 px, centred), transparent around it, so the Dock shows a tile
#       the size of its neighbours
#   icons/icon-maskable-512.png              "maskable": the ground tone to every edge,
#       the mark inside the central 80% circle the platform's mask keeps
#
# Run once after changing the mark, then py/build_precache.py (the icons are precached).
# Run:   python3 py/build_icons.py [--chrome <path>]
# Exit:  0 written, 1 a render failed or came out the wrong size, 2 Chrome not found
import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "icons"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# The dark theme's tokens (STYLE_GUIDE.md).
GROUND, EDGE, ACCENT, SURFACE, TEXT = "#171719", "#2f3f55", "#4a8ad6", "#131217", "#ffffff"

# The brand mark, as in index.html's header, on its 24-unit grid.
MARK = (f'<g fill="none" stroke="{TEXT}" stroke-width="1.8" stroke-linecap="round">'
        f'<rect x="8.5" y="2.5" width="7" height="12" rx="3.5" fill="{ACCENT}" stroke="{ACCENT}"/>'
        f'<path d="M10.6 6.5h2.8M10.6 9.5h2.8" stroke="{SURFACE}" stroke-width="1.4"/>'
        '<path d="M5 11.5a7 7 0 0 0 14 0"/><path d="M12 18.5v3M8.5 21.5h7"/></g>')


def icon_svg(maskable):
    """The icon on a 1024-unit canvas."""
    if maskable:
        # The mask keeps a circle of radius 40% of the size; the mark's 24 units fill 440.
        back = f'<rect width="1024" height="1024" fill="{GROUND}"/>'
        scale = 440 / 24
    else:
        # The macOS grid: an 824-unit tile at 100 in, corners at 185.
        back = (f'<rect x="100" y="100" width="824" height="824" rx="185" fill="{GROUND}" '
                f'stroke="{EDGE}" stroke-width="6"/>')
        scale = 520 / 24
    offset = (1024 - 24 * scale) / 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="100%" height="100%">'
            f'{back}<g transform="translate({offset:.2f} {offset:.2f}) scale({scale:.4f})">{MARK}</g></svg>')


def png_size(path):
    """(width, height) from a PNG's IHDR chunk."""
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path.name} is not a PNG")
    return struct.unpack(">II", head[16:24])


def render(chrome, svg, size, dest, scratch):
    page = scratch / f"{dest.stem}.html"
    page.write_text("<!doctype html><html><body style='margin:0;overflow:hidden;background:transparent'>"
                    f"<div style='width:{size}px;height:{size}px'>{svg}</div></body></html>", encoding="utf-8")
    subprocess.run([chrome, "--headless=new", "--hide-scrollbars", "--default-background-color=00000000",
                    f"--user-data-dir={scratch / 'profile'}", "--no-first-run", "--no-default-browser-check",
                    f"--window-size={size},{size}", f"--screenshot={dest}", page.as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if png_size(dest) != (size, size):
        raise ValueError(f"{dest.name} came out {png_size(dest)}, not {size} x {size}")


def main():
    ap = argparse.ArgumentParser(description="Render the app icons from the brand mark")
    ap.add_argument("--chrome", default=CHROME)
    args = ap.parse_args()
    if not Path(args.chrome).exists():
        print(f"NOT RUN  Chrome not found at {args.chrome}")
        return 2
    OUT.mkdir(exist_ok=True)
    jobs = [("icon-192.png", 192, False), ("icon-512.png", 512, False), ("icon-maskable-512.png", 512, True)]
    with tempfile.TemporaryDirectory(prefix="recqc-icons-") as tmp:
        for name, size, maskable in jobs:
            try:
                render(args.chrome, icon_svg(maskable), size, OUT / name, Path(tmp))
            except (subprocess.SubprocessError, OSError, ValueError) as e:
                print(f"FAIL     {name}: {e}")
                return 1
            print(f"wrote    icons/{name} ({size} x {size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
