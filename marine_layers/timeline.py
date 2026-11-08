# SPDX-License-Identifier: GPL-3.0-or-later
"""Colour mapping and disk assets for Environmental Layer image sequences."""

from html import escape
import struct
import zlib


PALETTE_ITEMS = [
    ('NEON', 'Neon green', 'Artistic ramp from a grey Marine map'),
    ('CYAN', 'Cyan', 'Artistic ramp from a grey Marine map'),
    ('algae', 'Algae', ''),
    ('thermal', 'Thermal', ''),
    ('viridis', 'Viridis', ''),
    ('gray', 'Greyscale', ''),
    ('WHITE_RED', 'White to Red', 'White through coral to deep red'),
    ('TURBID', 'Sediment · Turbid', 'Pale sand through ochre to dark brown'),
    ('cividis', 'Cividis', 'Dark blue through grey to yellow'),
    ('ice', 'Ice', 'Deep navy through blue to white'),
    ('magma', 'Magma', 'Dark violet through orange to pale cream'),
]
PALETTE_LABELS = {key: label for key, label, _ in PALETTE_ITEMS}

# The legacy Blender ramps take encoded grey as input and output linear RGB.
ART_RAMPS = {
    'NEON': ((0.005, 0.012, 0.008), (0.48, 1.0, 0.005)),
    'CYAN': ((0.002, 0.014, 0.022), (0.35, 0.9, 1.0)),
}
LOCAL_RAMPS = {
    'WHITE_RED': ((255, 255, 255), (255, 213, 200), (247, 132, 109), (211, 53, 45), (155, 9, 24)),
    # Five samples from cmocean Turbid (Kristen M. Thyng, MIT).
    'TURBID': ((233, 246, 171), (202, 174, 88), (161, 112, 59), (98, 69, 52), (34, 31, 27)),
}


def sample_ramp(stops, fraction):
    position = fraction * (len(stops) - 1)
    index = min(int(position), len(stops) - 2)
    weight = position - index
    return tuple(a + (b - a) * weight for a, b in zip(stops[index], stops[index + 1]))


def linear_to_srgb(value):
    return 12.92 * value if value < 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def palette_mapping(palette, provider_colors):
    """Return a grey-byte lookup (when needed) and the displayed legend RGBs."""
    if palette not in LOCAL_RAMPS and palette not in ART_RAMPS:
        return None, provider_colors
    if any(red != green or green != blue for red, green, blue in provider_colors):
        raise ValueError('This WMTS legend is not greyscale. Choose another palette.')
    greys = [rgb[0] for rgb in provider_colors]
    if palette in ART_RAMPS:
        lookup = [tuple(round(255 * linear_to_srgb(value)) for value in sample_ramp(ART_RAMPS[palette], grey / 255))
                  for grey in range(256)]
    else:
        # Invert the actual provider ramp. Grey intensity need not vary linearly
        # with value, and several legend steps can share an 8-bit grey code.
        if (greys[0] == greys[-1]
                or greys not in (sorted(greys), sorted(greys, reverse=True))):
            raise ValueError('The WMTS greyscale is not monotonic. Choose another palette.')
        samples = {}
        for i, grey in enumerate(greys):
            samples.setdefault(grey, []).append(i / (len(greys) - 1))
        positions = {grey: sum(steps) / len(steps) for grey, steps in samples.items()}
        positions[greys[0]], positions[greys[-1]] = 0, 1
        lookup = []
        for grey in range(256):
            nearest = min(positions, key=lambda sample: abs(sample - grey))
            lookup.append(tuple(round(v) for v in sample_ramp(LOCAL_RAMPS[palette], positions[nearest])))
    return lookup, [lookup[grey] for grey in greys]


def write_png(path, pixels):
    """Write an RGBA byte array in Blender's bottom-to-top row order."""
    height, width, _ = pixels.shape
    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data)))
    rows = b''.join(b'\0' + row.tobytes() for row in pixels[::-1])
    header = struct.pack('!IIBBBBB', width, height, 8, 6, 0, 0, 0)
    with path.open('wb') as file:
        file.write(b'\x89PNG\r\n\x1a\n')
        file.write(chunk(b'IHDR', header))
        file.write(chunk(b'sRGB', b'\0'))
        file.write(chunk(b'IDAT', zlib.compress(rows, level=3)))
        file.write(chunk(b'IEND', b''))


def write_legend(path, frame, colors):
    layer = frame['layer']
    variable = layer['id'].rsplit('/', 1)[-1]
    title = escape(f'{variable} · {PALETTE_LABELS[frame["palette"]]}')
    unit = escape(layer['unit'])
    low, high = frame['range']
    ticks = ''.join(
        f'<text x="{24 + 144 * i}" y="106" text-anchor="{anchor}">{low + (high - low) * i / 4:.5g}</text>'
        for i, anchor in enumerate(('start', 'middle', 'middle', 'middle', 'end'))
    )
    swatches = ''.join(
        f'<rect x="{24 + 576 * i / len(colors):.6f}" y="53" width="{576 / len(colors) + 0.1:.6f}" '
        f'height="29" fill="#{r:02x}{g:02x}{b:02x}"/>'
        for i, (r, g, b) in enumerate(colors)
    )
    path.write_text(f"""<svg xmlns="http://www.w3.org/2000/svg" width="624" height="160" viewBox="0 0 624 160">
<defs><pattern id="nodata" width="8" height="8" patternUnits="userSpaceOnUse">
<rect width="8" height="8" fill="#ffffff"/><path d="M0 0h4v4H0zM4 4h4v4H4z" fill="#bbbbbb"/>
</pattern></defs>
<rect width="624" height="160" fill="#ffffff"/>
<g font-family="sans-serif" font-size="13" fill="#202124">
<text x="24" y="33" font-size="18">{title}</text>
<text x="600" y="33" text-anchor="end">{unit}</text>
{swatches}
{ticks}
<rect x="24" y="125" width="20" height="15" fill="url(#nodata)" stroke="#aaaaaa"/>
<text x="52" y="137">NoData · transparent</text>
<text x="600" y="137" text-anchor="end">Contains modified Copernicus Marine Service information</text>
</g></svg>
""", encoding='utf8')
