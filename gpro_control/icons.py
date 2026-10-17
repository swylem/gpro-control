"""The app's own icon: a mouse outline that fills up with the battery level.

Drawn here as SVG so the tray can show the live level, and rendered to
pixmaps for the tray (StatusNotifierItem IconPixmap) with GdkPixbuf.
"""

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

TRAY_SIZES = (16, 22, 24, 32, 48, 64)

# Mouse body in a 64x64 box: a capsule with the button split and a wheel.
_BODY = "M32 3 C20 3 11 11 11 23 V41 C11 54 20 61 32 61 C44 61 53 54 53 41 V23 C53 11 44 3 32 3 Z"
_SPLIT = "M11 27 H53 M32 3 V27"
_WHEEL = '<rect x="29" y="10" width="6" height="11" rx="3"'
_BOLT = "M36 29 L23 45 H31 L28 58 L42 39 H34 Z"

GREEN, AMBER, RED = "#3fb950", "#e3b341", "#f85149"


def level_color(percent):
    if percent < 20:
        return RED
    if percent < 50:
        return AMBER
    return GREEN


def mouse_svg(percent=None, charging=False):
    """Tray icon. `percent` None means no reading (outline only, dimmed)."""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">',
        f'<defs><clipPath id="body"><path d="{_BODY}"/></clipPath></defs>',
    ]
    if percent is not None:
        top = 61 - 58 * max(0, min(100, percent)) / 100
        parts.append(f'<rect x="0" y="{top:.1f}" width="64" height="64" fill="{level_color(percent)}" clip-path="url(#body)"/>')
    opacity = "1" if percent is not None else "0.55"
    # Dark halo under a light stroke keeps the outline readable on light and dark panels.
    parts += [
        f'<g fill="none" stroke-linecap="round" stroke-linejoin="round" opacity="{opacity}">',
        f'<path d="{_BODY}" stroke="#1b1e20" stroke-width="7"/>',
        f'<path d="{_SPLIT}" stroke="#1b1e20" stroke-width="5.5"/>',
        f'<path d="{_BODY}" stroke="#eff0f1" stroke-width="4"/>',
        f'<path d="{_SPLIT}" stroke="#eff0f1" stroke-width="2.5"/>',
        "</g>",
        f'{_WHEEL} fill="#eff0f1" stroke="#1b1e20" stroke-width="1.5" opacity="{opacity}"/>',
    ]
    if charging:
        parts.append(f'<path d="{_BOLT}" fill="#ffffff" stroke="#1b1e20" stroke-width="2.5" stroke-linejoin="round"/>')
    parts.append("</svg>")
    return "".join(parts)


def app_svg():
    """Static app icon for menus, the window and notifications."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<defs>'
        '<linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#3daee9"/><stop offset="1" stop-color="#1d78c4"/></linearGradient>'
        f'<clipPath id="body"><path d="{_BODY}"/></clipPath>'
        '</defs>'
        f'<path d="{_BODY}" fill="url(#g)"/>'
        f'<rect x="0" y="41" width="64" height="24" fill="{GREEN}" clip-path="url(#body)"/>'
        f'<path d="{_SPLIT}" fill="none" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" opacity="0.9"/>'
        f'{_WHEEL} fill="#ffffff"/>'
        f'<path d="{_BODY}" fill="none" stroke="#1b1e20" stroke-width="2.5" stroke-linejoin="round"/>'
        "</svg>"
    )


def render(svg, size):
    loader = GdkPixbuf.PixbufLoader()
    loader.set_size(size, size)
    loader.write(svg.encode())
    loader.close()
    pixbuf = loader.get_pixbuf()
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    return pixbuf


def argb32(pixbuf):
    """Pixbuf (RGBA rows, maybe padded) -> ARGB32 in network byte order, as SNI wants."""
    w, h, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
    src = pixbuf.get_pixels()
    rgba = b"".join(src[y * stride : y * stride + w * 4] for y in range(h))
    out = bytearray(len(rgba))
    out[0::4] = rgba[3::4]
    out[1::4] = rgba[0::4]
    out[2::4] = rgba[1::4]
    out[3::4] = rgba[2::4]
    return bytes(out)


_cache = {}


def tray_pixmaps(percent=None, charging=False):
    """[(width, height, argb_bytes), ...] for the tray, cached per state."""
    key = (percent, charging)
    if key not in _cache:
        svg = mouse_svg(percent, charging)
        _cache[key] = [(s, s, argb32(render(svg, s))) for s in TRAY_SIZES]
    return _cache[key]
