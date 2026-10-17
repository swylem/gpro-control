"""The app's icon: a mouse whose body is a battery gauge.

One design everywhere. The app icon (menu, window, taskbar, notifications)
is a dark mouse with a green gauge. The tray icon is the same mouse drawn
as panel-style line art. Its fill height is the battery percentage, and the
colour is the state: green, amber when getting low, red when low, blue with
a lightning bolt while charging.

Everything is drawn here as SVG (original artwork, no icon pack) and rendered
with GdkPixbuf.
"""

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ICON_NAME = "io.github.gpro_control"
TRAY_SIZES = (16, 22, 24, 32, 44, 64)

LIGHT_FG = "#fcfcfc"  # outline on dark panels (Breeze Dark text colour)
DARK_FG = "#232629"  # outline on light panels (Breeze text colour)

GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"
BLUE = "#3b82f6"  # charging


def state_color(percent, charging=False):
    if charging:
        return BLUE
    if percent < 20:
        return RED
    if percent < 50:
        return AMBER
    return GREEN


# Tray: 22x22 grid, same as Breeze tray icons.
_BODY = '<rect x="5.75" y="1.75" width="10.5" height="18.5" rx="5.25"'
_INNER = '<rect x="7.5" y="3.5" width="7" height="15" rx="3.5"'
_WHEEL = '<path d="M11 5 V7.5" stroke-linecap="round"'
_FILL_TOP, _FILL_BOTTOM = 3.5, 18.5  # 100% fills the whole body, wheel included
_BOLT = "M11.9 8.6 L8.7 13.6 H10.8 L10.1 17.2 L13.3 12.2 H11.2 Z"


def tray_svg(percent=None, charging=False, fg=LIGHT_FG):
    """Tray icon; `percent` None means no reading (dimmed outline)."""
    if percent is None:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22"><g opacity="0.5">'
            f'{_BODY} fill="none" stroke="{fg}" stroke-width="1.5"/>{_WHEEL} stroke="{fg}" stroke-width="1.5"/>'
            "</g></svg>"
        )
    level = max(0, min(100, percent))
    top = _FILL_BOTTOM - (_FILL_BOTTOM - _FILL_TOP) * level / 100
    color = state_color(level, charging)
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22">',
        f'<defs><clipPath id="inner">{_INNER}/></clipPath>'
        '<mask id="cut" maskUnits="userSpaceOnUse" x="0" y="0" width="22" height="22">'
        '<rect width="22" height="22" fill="#fff"/>'
        f'{_WHEEL} stroke="#000" stroke-width="3"/>'  # keep the wheel readable over the fill
        + (f'<path d="{_BOLT}" fill="#000" stroke="#000" stroke-width="1.4" stroke-linejoin="round"/>' if charging else "")
        + "</mask></defs>",
        f'<g clip-path="url(#inner)" mask="url(#cut)"><rect x="0" y="{top:.2f}" width="22" height="22" fill="{color}"/></g>',
        f'{_BODY} fill="none" stroke="{fg}" stroke-width="1.5"/>',
        f'{_WHEEL} stroke="{fg}" stroke-width="1.5"/>',
    ]
    if charging:
        out.append(f'<path d="{_BOLT}" fill="{fg}"/>')
    out.append("</svg>")
    return "".join(out)


def app_svg():
    """App icon (128px grid): a dark mouse with a green battery gauge.
    Dark body with a light rim reads on light and dark menus alike."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
        "<defs>"
        '<linearGradient id="body" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#3a4150"/><stop offset="1" stop-color="#1b1f27"/></linearGradient>'
        '<linearGradient id="gauge" x1="0" y1="1" x2="0" y2="0">'
        f'<stop offset="0" stop-color="#16a34a"/><stop offset="1" stop-color="{GREEN}"/></linearGradient>'
        '<clipPath id="inner"><rect x="42" y="16" width="44" height="98" rx="22"/></clipPath>'
        '<filter id="shadow" x="-30%" y="-20%" width="160%" height="150%">'
        '<feDropShadow dx="0" dy="3" stdDeviation="3" flood-color="#000" flood-opacity="0.35"/></filter>'
        "</defs>"
        '<rect x="34" y="8" width="60" height="114" rx="30" fill="url(#body)" filter="url(#shadow)"/>'
        '<g clip-path="url(#inner)"><rect x="30" y="58" width="68" height="64" fill="url(#gauge)"/></g>'
        '<rect x="34" y="8" width="60" height="114" rx="30" fill="none" stroke="#eef1f5" stroke-width="6"/>'
        '<path d="M64 24 V42" stroke="#eef1f5" stroke-width="7" stroke-linecap="round"/>'
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


def tray_pixmaps(percent=None, charging=False, fg=LIGHT_FG):
    """[(width, height, argb_bytes), ...] for the tray, cached per state."""
    key = (percent, charging, fg)
    if key not in _cache:
        svg = tray_svg(percent, charging, fg)
        _cache[key] = [(s, s, argb32(render(svg, s))) for s in TRAY_SIZES]
    return _cache[key]
