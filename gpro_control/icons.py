"""The app's own icons.

Tray: a monochrome line-art mouse, drawn like Plasma's own 22px tray icons,
whose body fills up with the battery level. Red when low; while charging a
lightning bolt is cut out of the fill. App: a dark rounded tile with the
same mouse in a cyan-to-violet glow.

Everything is drawn here as SVG (original artwork, no icon pack), so the tray
can show the live level. It's rendered to pixmaps with GdkPixbuf.
"""

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

TRAY_SIZES = (16, 22, 24, 32, 44, 64)

LIGHT_FG = "#fcfcfc"  # on dark panels (Breeze Dark text colour)
DARK_FG = "#232629"  # on light panels (Breeze text colour)
LOW_RED = "#da4453"  # Breeze negative

# 22x22 grid, same as Breeze tray icons.
_BODY = '<rect x="5.75" y="1.75" width="10.5" height="18.5" rx="5.25"'
_INNER = '<rect x="7.5" y="3.5" width="7" height="15" rx="3.5"'
_FILL_TOP, _FILL_BOTTOM = 9.25, 18.5  # the gauge lives below the wheel
_BOLT = "M11.9 10 L8.9 14.6 H10.8 L10.1 17.6 L13.1 13 H11.2 Z"


def tray_svg(percent=None, charging=False, fg=LIGHT_FG):
    """Tray icon; `percent` None means no reading (dimmed outline)."""
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22">',
        "<defs>",
        f'<clipPath id="inner">{_INNER}/></clipPath>',
        f'<mask id="bolt" maskUnits="userSpaceOnUse" x="0" y="0" width="22" height="22">'
        f'<rect width="22" height="22" fill="#fff"/><path d="{_BOLT}" fill="#000" stroke="#000" stroke-width="1.2" stroke-linejoin="round"/></mask>',
        "</defs>",
    ]
    if percent is None:
        out.append(f'<g opacity="0.5">{_BODY} fill="none" stroke="{fg}" stroke-width="1.5"/>'
                   f'<path d="M11 5 V7.5" stroke="{fg}" stroke-width="1.5" stroke-linecap="round"/></g>')
        out.append("</svg>")
        return "".join(out)

    level = max(0, min(100, percent))
    top = _FILL_BOTTOM - (_FILL_BOTTOM - _FILL_TOP) * level / 100
    color = LOW_RED if level < 20 and not charging else fg
    mask = ' mask="url(#bolt)"' if charging else ""
    out += [
        f'{_BODY} fill="none" stroke="{fg}" stroke-width="1.5"/>',
        f'<path d="M11 5 V7.5" stroke="{fg}" stroke-width="1.5" stroke-linecap="round"/>',
        f'<g clip-path="url(#inner)"{mask}><rect x="0" y="{top:.2f}" width="22" height="22" fill="{color}"/></g>',
    ]
    if charging:
        # The part of the bolt above the fill line is drawn solid, so the bolt
        # reads at any level: a hole in the fill, a shape above it.
        out.append(f'<g clip-path="url(#inner)"><clipPath id="above"><rect width="22" height="{top:.2f}"/></clipPath>'
                   f'<path d="{_BOLT}" fill="{fg}" clip-path="url(#above)"/></g>')
    out.append("</svg>")
    return "".join(out)


def app_svg():
    """App icon for the menu, window and notifications (128px grid)."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
        "<defs>"
        '<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#2b3140"/><stop offset="1" stop-color="#12151c"/></linearGradient>'
        '<linearGradient id="glow" x1="0" y1="1" x2="0.4" y2="0">'
        '<stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#a78bfa"/></linearGradient>'
        '<linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#ffffff" stop-opacity="0.10"/><stop offset="0.5" stop-color="#ffffff" stop-opacity="0"/></linearGradient>'
        '<clipPath id="inner"><rect x="50" y="34" width="28" height="60" rx="14"/></clipPath>'
        '<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter>'
        "</defs>"
        '<rect x="6" y="6" width="116" height="116" rx="28" fill="url(#tile)"/>'
        '<rect x="6" y="6" width="116" height="116" rx="28" fill="url(#sheen)"/>'
        '<rect x="6.5" y="6.5" width="115" height="115" rx="27.5" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>'
        # soft glow behind the gauge
        '<rect x="50" y="58" width="28" height="36" rx="14" fill="url(#glow)" opacity="0.55" filter="url(#blur)"/>'
        # mouse outline, wheel, gauge
        '<rect x="43" y="27" width="42" height="74" rx="21" fill="none" stroke="#f1f3f7" stroke-width="6"/>'
        '<path d="M64 39 V50" stroke="#f1f3f7" stroke-width="6" stroke-linecap="round"/>'
        '<g clip-path="url(#inner)"><rect x="40" y="60" width="48" height="40" fill="url(#glow)"/></g>'
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
