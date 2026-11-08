# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch and sample Mapzen Terrarium elevation in the OSM tile grid."""

from array import array
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path

import bpy

from . import osm_tiles

SOURCE = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
ATTRIBUTION = 'Terrain: Mapzen / AWS Open Data'
CREDITS_URL = 'https://github.com/tilezen/joerd/blob/master/docs/attribution.md'
MIN_ZOOM = 6
SIZE = 256


def fetch_view(folder, tiles, offline, cache_mb, terrain):
    # Prune after both data sources have populated the cache.
    data = osm_tiles.fetch_tiles(folder, tiles, offline, cache_mb, prune_cache=False)
    paths = {}
    if data and terrain and tiles[0][0] >= MIN_ZOOM:
        # Open ocean tiles have no land to drape. Other tiles share a common grid.
        needed = [tile for tile, layers in data.items() if not ocean_only(layers)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            files = pool.map(lambda tile: fetch_elevation(folder, tile, offline), needed)
            paths = dict(zip(needed, files))
    if not offline:
        osm_tiles.prune(Path(folder), cache_mb * 1024 * 1024)
    return data, paths


def ocean_only(layers):
    extent, features = layers.get('water', (1, ()))
    if any(name in layers for name in ('building', 'place', 'transportation', 'waterway')):
        return False
    return any(kind == 3 and tags.get('class') == 'ocean' and len(paths) == 1
               and len(set(paths[0])) == 4
               and all((x <= 0 or x >= extent) and (y <= 0 or y >= extent) for x, y in paths[0])
               and len({(x <= 0, y <= 0) for x, y in paths[0]}) == 4
               for kind, paths, tags in features)


def fetch_elevation(folder, tile, offline):
    z, x, y = tile
    path = Path(folder) / 'terrarium' / str(z) / str(x) / f'{y}.png'
    try:
        if path.is_file():
            if not offline:
                os.utime(path, None)
            return path
        if not offline:
            data = osm_tiles.read_url(SOURCE.format(z=z, x=x, y=y), 1_000_000)
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            osm_tiles.write_cache(path, data)
            return path
    except (OSError, ValueError):
        return None
    return None


def read_pixels(path):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = 'Non-Color'
        channels = len(image.pixels) // (SIZE * SIZE)
        if tuple(image.size) != (SIZE, SIZE) or channels not in (3, 4):
            raise ValueError('Unsupported elevation tile image.')
        pixels = array('f', [0]) * (SIZE * SIZE * channels)
        image.pixels.foreach_get(pixels)
        return array('f', (round(pixels[i] * 255) * 256 + round(pixels[i+1] * 255)
                           + round(pixels[i+2] * 255) / 256 - 32768
                           for i in range(0, len(pixels), channels)))
    finally:
        bpy.data.images.remove(image)


class TerrainTiles:
    def __init__(self, paths):
        self.paths = paths
        self.pixels = {}

    def load(self, tile):
        if tile not in self.pixels:
            path = self.paths.get(tile)
            self.pixels[tile] = read_pixels(path) if path else None
        return self.pixels[tile]

    def sample(self, tile, u, v):
        z, column, row = tile
        x, y = u * SIZE - 0.5, v * SIZE - 0.5
        ix, iy = math.floor(x), math.floor(y)
        fx, fy = x - ix, y - iy

        total, weight_sum = 0.0, 0.0
        for px, py, weight in ((ix, iy, (1-fx)*(1-fy)), (ix+1, iy, fx*(1-fy)),
                               (ix, iy+1, (1-fx)*fy), (ix+1, iy+1, fx*fy)):
            dx, rx = divmod(px, SIZE)
            dy, ry = divmod(py, SIZE)
            neighbour = self.load((z, column + dx, row - dy))
            if neighbour is not None:
                total += neighbour[ry * SIZE + rx] * weight
                weight_sum += weight
        return total / weight_sum

    def grid(self, tile, metres_per_unit):
        if not self.paths.get(tile):
            return None
        divisions = 64 if tile[0] >= 12 else 24
        values = [self.sample(tile, x / divisions, y / divisions) / metres_per_unit
                  for y in range(divisions + 1) for x in range(divisions + 1)]
        return TerrainGrid(divisions, values)


class TerrainGrid:
    def __init__(self, divisions, values):
        self.divisions = divisions
        self.values = values

    def elevation(self, u, v):
        n = self.divisions
        x, y = min(n, max(0, u * n)), min(n, max(0, v * n))
        i, j = min(n-1, int(x)), min(n-1, int(y))
        fx, fy = x - i, y - j
        start = j * (n + 1) + i
        a, b = self.values[start], self.values[start + 1]
        c, d = self.values[start + n + 1], self.values[start + n + 2]
        if fy <= fx:
            return a + fx * (b-a) + fy * (d-b)
        return a + fy * (c-a) + fx * (d-c)

    def drape(self, points):
        """Split line segments wherever they cross a terrain triangle edge."""
        n = self.divisions
        yield points[0]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            stops = {1.0}
            for start, end in ((x0, x1), (y0, y1), (x0-y0, x1-y1)):
                low, high = sorted((start*n, end*n))
                stops.update((edge-start*n)/((end-start)*n)
                             for edge in range(math.floor(low)+1, math.ceil(high)))
            for fraction in sorted(stops):
                yield x0 + (x1-x0)*fraction, y0 + (y1-y0)*fraction
