# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded OpenFreeMap vector tile requests, disk cache and MVT decoding."""

from concurrent.futures import ThreadPoolExecutor
import gzip
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import ssl
import struct
import sys
from tempfile import NamedTemporaryFile
import time
from urllib.request import Request, urlopen

from .core import HALF

WORLD = 2 * HALF
SOURCE = 'https://tiles.openfreemap.org/planet/latest'
ATTRIBUTION = 'OpenFreeMap · © OpenMapTiles · © OpenStreetMap contributors'
MAX_ZOOM = 14
MAX_TILES = 16
MAX_BYTES = 12_000_000
LAYERS = {'water', 'waterway', 'transportation', 'transportation_name', 'boundary', 'building', 'place'}
TAGS = {'name', 'name:latin', 'name:en', 'class', 'subclass', 'capital'}
_last_prune = {}


def tile_bounds(tile):
    zoom, column, row = tile
    span = WORLD / (1 << zoom)
    return column * span - HALF, HALF - (row + 1) * span, span


def tile_plan(bounds, pixels, max_zoom=MAX_ZOOM):
    west, south, east, north = bounds
    if not all(math.isfinite(value) for value in bounds) or east <= west or north <= south:
        raise ValueError('The OSM map needs a view with a measurable ground extent.')
    resolution = max((east - west) / max(pixels[0], 1), (north - south) / max(pixels[1], 1))
    west, east = max(-HALF, west), min(HALF, east)
    south, north = max(-HALF, south), min(HALF, north)
    if east <= west or north <= south:
        return ()
    if east - west >= WORLD * 0.9 and north - south >= WORLD * 0.9:
        return ((0, 0, 0),)
    zoom = max(0, min(max_zoom, math.floor(math.log2(WORLD / (384 * resolution)))))
    while True:
        count = 1 << zoom
        span = WORLD / count
        x0 = max(0, math.floor((west + HALF) / span))
        x1 = min(count - 1, math.ceil((east + HALF) / span) - 1)
        y0 = max(0, math.floor((HALF - north) / span))
        y1 = min(count - 1, math.ceil((HALF - south) / span) - 1)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= MAX_TILES:
            return tuple((zoom, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
        zoom -= 1


def read_url(url, limit):
    context = ssl.create_default_context()
    if sys.platform == 'darwin' and Path('/etc/ssl/cert.pem').is_file():
        context.load_verify_locations(cafile='/etc/ssl/cert.pem')
    request = Request(url, headers={'User-Agent': 'BlenderGIS-Marine-Layers/OSM-World'})
    with urlopen(request, timeout=12, context=context) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('The OSM tile exceeded the download limit.')
    return data


def source_template(data):
    metadata = json.loads(data)
    template = metadata['tiles'][0]
    match = re.fullmatch(r'https://tiles\.openfreemap\.org/planet/([\w-]+)/\{z\}/\{x\}/\{y\}\.pbf', template)
    if not match or metadata.get('scheme', 'xyz') != 'xyz':
        raise ValueError('OpenFreeMap returned an unsupported tile address.')
    return match[1], template


def write_cache(path, data):
    with NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as temp:
        temporary_path = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def read_source(folder, offline):
    path = folder / 'source.json'
    if not offline:
        try:
            if not path.is_file() or time.time() - path.stat().st_mtime >= 3600:
                fresh = read_url(SOURCE, 250_000)
                source_template(fresh)
                write_cache(path, fresh)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            if not path.is_file():
                raise
    if not path.is_file():
        raise ValueError('The OSM cache has no source yet. Turn off Cache only and load a view.')
    return source_template(path.read_bytes())


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('The vector tile contains a truncated integer.')


def fields(data):
    offset = 0
    while offset < len(data):
        key, offset = varint(data, offset)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            end = offset + size
            if end > len(data):
                raise ValueError('The vector tile contains a truncated field.')
            value, offset = data[offset:end], end
        else:
            raise ValueError('The vector tile uses an unsupported field type.')
        yield field, value


def integers(data):
    offset = 0
    while offset < len(data):
        value, offset = varint(data, offset)
        yield value


def zigzag(value):
    return (value >> 1) ^ -(value & 1)


def tile_value(data):
    for kind, value in fields(data):
        if kind == 1:
            return value.decode('utf8', errors='replace')
        if kind in (2, 3):
            return struct.unpack('<f' if kind == 2 else '<d', value)[0]
        if kind in (4, 5):
            return value
        if kind == 6:
            return zigzag(value)
        if kind == 7:
            return bool(value)
    return None


def geometry_paths(data, kind):
    values = iter(integers(data))
    x, y = 0, 0
    paths = []
    for command in values:
        operation, count = command & 7, command >> 3
        if operation == 7:
            continue
        if operation not in (1, 2) or not count:
            raise ValueError('The vector tile contains an invalid geometry command.')
        if operation == 2 and not paths:
            raise ValueError('The vector tile has a line without a starting point.')
        for _ in range(count):
            dx, dy = next(values, None), next(values, None)
            if dx is None or dy is None:
                raise ValueError('The vector tile contains truncated geometry.')
            x, y = x + zigzag(dx), y + zigzag(dy)
            if operation == 1:
                paths.append([])
            paths[-1].append((x, y))
    minimum = {1: 1, 2: 2, 3: 3}[kind]
    return [path for path in paths if len(path) >= minimum]


def decode_tile(data):
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=BytesIO(data)) as compressed:
            data = compressed.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('The expanded OSM tile exceeded the data limit.')
    layers = {}
    for field, layer in fields(data):
        if field != 3:
            continue
        name, extent, features, keys, values = '', 4096, [], [], []
        for tag, value in fields(layer):
            if tag == 1:
                name = value.decode('utf8', errors='replace')
                if name not in LAYERS:
                    break
            elif tag == 2:
                features.append(value)
            elif tag == 3:
                keys.append(value.decode('utf8', errors='replace'))
            elif tag == 4:
                values.append(value)
            elif tag == 5:
                extent = value
        if name not in LAYERS:
            continue
        if not 1 <= extent <= 65536:
            raise ValueError('The vector tile has an invalid coordinate extent.')
        entries = []
        for feature in features:
            record = dict(fields(feature))
            kind, shape = record.get(3), record.get(4)
            if kind not in (1, 2, 3) or not shape:
                continue
            indices = iter(integers(record.get(2, b'')))
            tags = {keys[key]: tile_value(values[value]) for key, value in zip(indices, indices)
                    if key < len(keys) and keys[key] in TAGS and value < len(values)}
            paths = geometry_paths(shape, kind)
            if paths:
                entries.append((kind, paths, tags))
        layers[name] = extent, entries
    return layers


def prune(folder, limit):
    now = time.monotonic()
    key = str(folder), limit
    if now - _last_prune.get(key, -120) < 120:
        return
    _last_prune[key] = now
    candidates = []
    for path in (*folder.glob('*/*/*/*.pbf'), *folder.glob('terrarium/*/*/*.png')):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        candidates.append((info.st_mtime, info.st_size, path))
    remaining = sum(size for _, size, _ in candidates)
    for _, size, path in sorted(candidates):
        if remaining <= limit:
            break
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        remaining -= size


def fetch_tiles(folder, tiles, offline=False, cache_mb=512, prune_cache=True):
    folder = Path(folder)
    if not offline:
        folder.mkdir(parents=True, exist_ok=True)
    version, template = read_source(folder, offline)

    def fetch(tile):
        z, x, y = tile
        path = folder / version / str(z) / str(x) / f'{y}.pbf'
        try:
            decoded = decode_tile(path.read_bytes())
        except FileNotFoundError:
            if offline:
                return tile, None
        except (OSError, ValueError, EOFError, struct.error):
            if offline:
                raise
        else:
            if not offline:
                try:
                    os.utime(path, None)
                except FileNotFoundError:
                    pass
            return tile, decoded
        data = read_url(template.format(z=z, x=x, y=y), MAX_BYTES)
        decoded = decode_tile(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_cache(path, data)
        return tile, decoded

    with ThreadPoolExecutor(max_workers=4) as pool:
        output = dict(pool.map(fetch, tiles))
    if not offline and prune_cache:
        prune(folder, cache_mb * 1024 * 1024)
    return {tile: data for tile, data in output.items() if data is not None}
