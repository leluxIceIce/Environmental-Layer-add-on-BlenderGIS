# SPDX-License-Identifier: GPL-3.0-or-later
"""Colour mapping and disk assets for Environmental Layer image sequences."""

from html import escape
import struct
import uuid
import zlib
from zipfile import ZipFile


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
    """Map source RGBs to a local ramp using their order in the WMTS legend."""
    if palette not in LOCAL_RAMPS and palette not in ART_RAMPS:
        return None, provider_colors
    source = [tuple(rgb) for rgb in provider_colors]
    samples = {}
    for index, rgb in enumerate(source):
        samples.setdefault(rgb, []).append(index / (len(source) - 1))
    # Duplicate RGBs are indistinguishable in the tiles. Use the middle of
    # their positions, keeping the range endpoints when their RGBs differ.
    positions = {rgb: sum(steps) / len(steps) for rgb, steps in samples.items()}
    if source[0] != source[-1]:
        positions[source[0]], positions[source[-1]] = 0, 1
    if palette in ART_RAMPS:
        lookup = {
            rgb: tuple(round(255 * linear_to_srgb(value)) for value in sample_ramp(ART_RAMPS[palette], position))
            for rgb, position in positions.items()
        }
    else:
        lookup = {
            rgb: tuple(round(value) for value in sample_ramp(LOCAL_RAMPS[palette], position))
            for rgb, position in positions.items()
        }
    return lookup, [lookup[rgb] for rgb in source]


def recolor_rgba(pixels, lookup):
    """Recolour visible pixels in place, keeping the WMTS alpha mask."""
    import numpy as np

    visible = pixels[:, :, 3] > 0
    rgb = pixels[visible, :3].astype(np.uint32)
    # Group repeated RGBs so palette matching runs once per distinct colour.
    packed = (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]
    codes, inverse = np.unique(packed, return_inverse=True)
    unique_rgb = np.column_stack((codes >> 16, (codes >> 8) & 255, codes & 255)).astype(np.int32)
    mapped = np.empty((len(codes), 3), dtype=np.uint8)
    missing = []
    for index, rgb in enumerate(unique_rgb):
        color = lookup.get(tuple(rgb))
        if color is None:
            missing.append(index)
        else:
            mapped[index] = color
    if missing:
        # PNGs can contain interpolated colours. Use the nearest source RGB
        # and bound the memory used by the distance calculation.
        source = np.asarray(list(lookup), dtype=np.int32)
        colors = np.asarray(list(lookup.values()), dtype=np.uint8)
        for start in range(0, len(missing), 1024):
            indices = missing[start:start + 1024]
            delta = unique_rgb[indices, None, :] - source[None, :, :]
            nearest = np.argmin(np.sum(delta * delta, axis=2), axis=1)
            mapped[indices] = colors[nearest]
    pixels[visible, :3] = mapped[inverse]


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


def frame_filenames(frames):
    names = [frame['file'] for frame in frames]
    expected = [f'frame_{index:04d}.png' for index in range(1, len(names) + 1)]
    if not names or names != expected:
        raise ValueError('The timeline manifest has invalid frame filenames.')
    return names


def write_archive(path, entries):
    """Publish the ZIP only after every entry has been written."""
    temporary = path.parent / f'.timeline-{uuid.uuid4().hex}.zip'
    try:
        with ZipFile(temporary, 'w') as bundle:
            for name, data in entries:
                if isinstance(data, bytes):
                    bundle.writestr(name, data)
                else:
                    bundle.write(data, name)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_bundle(folder, frames):
    names = frame_filenames(frames) + ['legend.svg', 'timeline.json']
    write_archive(folder / 'timeline.zip', ((name, folder / name) for name in names))


def legend_svg(frame, colors):
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
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="624" height="160" viewBox="0 0 624 160">
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
"""
