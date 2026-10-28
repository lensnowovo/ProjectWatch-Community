"""Reproducible vector/raster exports of the approved flowing-P brand.

Maintainer-only renderer: cubic paths are the source, not a cropped concept image.
Pillow is already part of the desktop build environment; no network is used.
"""
from pathlib import Path

from PIL import Image, ImageDraw

INK, PAPER, BRASS = '#203b32', '#f4efdf', '#b39760'


def strands(small=False):
    result = []
    for i in ([0, 2, 4] if small else range(5)):
        d = i * 9
        result.append(([
            ('M', 38 + i * 4, 38 + d),
            ('C', 48 + i * 4, 48 + d, 70, 48 + d, 88, 48 + d),
            ('L', 145, 48 + d),
            ('C', 200 - i * 7, 48 + d, 218 - i * 9, 75 + i * 3, 218 - i * 9, 112),
            ('C', 218 - i * 9, 151 - i * 4, 189 - i * 5, 169 - d, 145, 169 - d),
            ('C', 116 - i * 5, 169 - d, 106 - i * 8, 179 - d, 106 - i * 8, 203),
            ('C', 106 - i * 8, 224, 99 - i * 8, 234, 84 - i * 8, 238),
        ], False))
    result.append(([
        ('M', 143, 97), ('C', 103, 104, 49, 119, 49, 163),
        ('L', 49, 207), ('C', 49, 223, 43, 232, 32, 238),
    ], True))
    return result


def svg(small=False, tile=False):
    width = 8 if small else 4.5
    items = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">']
    if tile:
        items.append(f'<rect x="8" y="8" width="240" height="240" rx="48" fill="{INK}"/>')
    # Keep icon artwork comfortably inside its tile, rather than clipping small strokes.
    items.append('<g transform="translate(24 12) scale(.82)">')
    for segments, accent in strands(small):
        path = ' '.join(command + ' ' + ' '.join(map(str, values)) for command, *values in segments)
        color = BRASS if accent else PAPER if tile else INK
        items.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>')
    items.append('</g></svg>\n')
    return ''.join(items)


def raster(size, small=False):
    # Flatten known cubic paths at build time, then supersample for crisp icon edges.
    factor = size * 4 / 256
    image = Image.new('RGBA', (size * 4, size * 4))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(tuple(v * factor for v in (8, 8, 248, 248)),
                           radius=48 * factor, fill=INK)
    for segments, accent in strands(small):
        points = []
        cursor = (0, 0)
        for command, *v in segments:
            if command in ('M', 'L'):
                cursor = tuple(v)
                points.append(cursor)
            else:
                start = cursor
                for step in range(1, 81):
                    t = step / 80
                    points.append(tuple((1-t)**3 * start[k] + 3*(1-t)**2*t*v[k]
                                        + 3*(1-t)*t*t*v[k+2] + t**3*v[k+4] for k in (0, 1)))
                cursor = tuple(v[4:6])
        coords = [((x * .82 + 24) * factor, (y * .82 + 12) * factor) for x, y in points]
        width = max(1, round((8 if small else 4.5) * .82 * factor))
        color = BRASS if accent else PAPER
        draw.line(coords, fill=color, width=width, joint='curve')
        radius = width / 2
        # Fill joins explicitly: Pillow's wide polyline joints can leave hairline
        # wedges along flattened curves at large export sizes.
        for x, y in coords:
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def build(repo):
    static = repo/'src/project_watch/static'
    assets = repo/'frontend/src/assets'
    assets.mkdir(exist_ok=True)
    (assets/'brand-mark.svg').write_text(svg(), encoding='utf-8')
    (assets/'brand-icon.svg').write_text(svg(small=True, tile=True), encoding='utf-8')
    (static/'favicon.svg').write_text(svg(small=True, tile=True), encoding='utf-8')
    raster(1024).save(static/'brand-desktop.png')
    raster(64, small=True).save(static/'brand-tray.png')


if __name__ == '__main__':
    build(Path(__file__).resolve().parents[1])
