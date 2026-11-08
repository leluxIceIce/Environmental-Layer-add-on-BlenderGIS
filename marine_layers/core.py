# SPDX-License-Identifier: GPL-3.0-or-later
"""Public Copernicus Marine WMTS access; also runs as an isolated download worker.

No Blender imports, credentials, pip packages, or disabled TLS verification.
"""
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = 'https://wmts.marine.copernicus.eu/teroWmts'
HALF = 20037508.342789244
HTTP_TIMEOUT = 35
HTTP_WORKERS = 4
NS = {'w': 'http://www.opengis.net/wmts/1.0', 'o': 'http://www.opengis.net/ows/1.1'}
PRODUCTS = {
    'NORTH_COAST_MONTHLY': ('North Sea coast — 100 m monthly', 'OCEANCOLOUR_NWS_BGC_HR_L4_NRT_009_209'),
    'NORTH_COAST_DAILY': ('North Sea coast — 100 m daily', 'OCEANCOLOUR_NWS_BGC_HR_L3_NRT_009_203'),
    'BALTIC_BIO': ('Baltic Sea — biology', 'BALTICSEA_ANALYSISFORECAST_BGC_003_007'),
    'BALTIC_PHY': ('Baltic Sea — physics', 'BALTICSEA_ANALYSISFORECAST_PHY_003_006'),
    'NORTH_BIO': ('North Sea — biology', 'NWSHELF_ANALYSISFORECAST_BGC_004_002'),
    'NORTH_PHY': ('North Sea — physics', 'NWSHELF_ANALYSISFORECAST_PHY_004_013'),
    'GLOBAL_BIO': ('Global ocean — biology', 'GLOBAL_ANALYSISFORECAST_BGC_001_028'),
    'GLOBAL_PHY': ('Global ocean — physics', 'GLOBAL_ANALYSISFORECAST_PHY_001_024'),
}


def request_bytes(url, limit=12_000_000):
    ctx = ssl.create_default_context()
    # Official Blender/Python builds on macOS may lack an installed CA bundle.
    # Add the system trust bundle; certificate verification remains enabled.
    if sys.platform == 'darwin' and Path('/etc/ssl/cert.pem').is_file():
        ctx.load_verify_locations(cafile='/etc/ssl/cert.pem')
    req = urllib.request.Request(url, headers={'User-Agent': 'BlenderGIS-Marine-Companion/0.1'})
    error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError('Response too large. Choose a single Marine product.')
            return data
        except (OSError, urllib.error.URLError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError('Marine service request failed: ' + str(error))


def url_for(**parameters):
    return BASE + '?' + urllib.parse.urlencode(parameters)


def parse_capabilities(data):
    root = ET.fromstring(data)
    layers = []
    for layer in root.findall('.//w:Layer', NS):
        identifier = layer.findtext('o:Identifier', '', NS)
        if 'EPSG:3857' not in [e.text for e in layer.findall('w:TileMatrixSetLink/w:TileMatrixSet', NS)]:
            continue
        dims = {}
        for dim in layer.findall('w:Dimension', NS):
            dims[dim.findtext('o:Identifier', '', NS).lower()] = {
                'default': dim.findtext('w:Default', '', NS),
                'values': [v.text for v in dim.findall('w:Value', NS) if v.text],
            }
        info = layer.find('.//w:VariableInformation', NS)
        def val(name, default=''):
            return info.findtext('w:' + name, default, NS) if info is not None else default
        period = layer.findtext('.//w:DataCubeInformation/w:admp_title', '', NS)
        title = val('Name') or layer.findtext('o:Title', identifier, NS)
        default_style = next((s.findtext('o:Identifier', '', NS)
                              for s in layer.findall('w:Style', NS)
                              if s.get('isDefault') == 'true'), 'cmap:viridis')
        lower = layer.findtext('o:WGS84BoundingBox/o:LowerCorner', '', NS)
        upper = layer.findtext('o:WGS84BoundingBox/o:UpperCorner', '', NS)
        bounds = [float(v) for v in (lower + ' ' + upper).split()] if lower and upper else None
        layers.append(dict(id=identifier, title=title, period=period,
                           unit=val('Unit'), minimum=val('MinimumValue'), maximum=val('MaximumValue'),
                           dimensions=dims, style=default_style, bounds=bounds,
                           catalogue_updated_utc=layer.findtext('.//w:DataCubeInformation/w:admp_updated', '', NS),
                           data_updated_utc=layer.findtext('.//w:DataCubeInformation/w:admp_updated_data', '', NS)))
    if not layers:
        raise ValueError('No Web Mercator map layers found for this product.')
    # Daily products first; keep full identifiers so distinct datasets never collide.
    return sorted(layers, key=lambda x: (0 if x['period'].lower() == 'daily' else 1,
                                        x['title'], x['id']))


def catalogue(product, cache_dir, ttl_seconds=3600, force_refresh=False):
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', product):
        raise ValueError('Choose a product or enter its product ID, not a URL.')
    path = Path(cache_dir) / ('catalogue_' + product + '.xml')
    if not force_refresh and path.is_file() and time.time() - path.stat().st_mtime < ttl_seconds:
        data = path.read_bytes()
    else:
        data = request_bytes(BASE + '/' + product + '?' + urllib.parse.urlencode(
            dict(SERVICE='WMTS', VERSION='1.0.0', REQUEST='GetCapabilities')))
        parse_capabilities(data)  # Never replace a valid cache with a service error.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return parse_capabilities(data)


def iso(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo:
        parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.replace(tzinfo=None)


def temporal_info(layer):
    identifier = layer['id']
    if 'P1M' in identifier:
        cadence, kind = 'Monthly', 'Monthly mean' if 'P1M-m' in identifier else 'Monthly composite'
    elif 'P1Y' in identifier:
        cadence, kind = 'Annual', 'Annual mean'
    elif 'P1D' in identifier:
        cadence, kind = 'Daily', 'Daily mean' if 'P1D-m' in identifier else 'Daily snapshot'
    elif re.search(r'PT\d+H', identifier):
        hours = re.search(r'PT(\d+)H', identifier).group(1)
        cadence, kind = hours + '-hourly', 'Snapshot' if '-i' in identifier else 'Time aggregate'
    else:
        cadence, kind = 'See dataset', layer.get('period', 'Dataset time')
    dim = layer.get('dimensions', {}).get('time', {})
    values = dim.get('values', [])
    starts, ends = [], []
    for value in values:
        parts = value.split('/')
        starts.append(parts[0]); ends.append(parts[1] if len(parts) > 1 else parts[0])
    return dict(cadence=cadence, kind=kind, start=min(starts) if starts else '',
                end=max(ends) if ends else '', latest=dim.get('default', ''))


def available_times(layer, limit=512):
    """Exact discrete times, or a bounded recent list from a regular interval."""
    values = layer.get('dimensions', {}).get('time', {}).get('values', [])
    output = []
    for value in values:
        if '/' not in value:
            output.append(value)
            continue
        parts = value.split('/')
        if len(parts) != 3:
            continue
        match = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?', parts[2])
        if not match:
            continue
        days, hours, minutes = [int(v or 0) for v in match.groups()]
        step = dt.timedelta(days=days, hours=hours, minutes=minutes)
        if step.total_seconds() <= 0:
            continue
        start, end = iso(parts[0]), iso(parts[1])
        count = int((end-start)/step)
        for index in range(max(0,count-limit+1),count+1):
            output.append((start+index*step).isoformat(timespec='seconds')+'Z')
    return sorted(set(output), key=iso)[-limit:]


def time_label(layer, timestamp):
    info = temporal_info(layer)
    value = iso(timestamp)
    if info['cadence'] == 'Monthly':
        return value.strftime('%B %Y') + ' · ' + info['kind'].lower()
    return value.strftime('%Y-%m-%d %H:%M') + ' UTC · ' + info['kind'].lower()


def resolve_time(layer, requested):
    dimension = layer['dimensions'].get('time')
    if not dimension:
        return ''
    chosen = requested.strip() or dimension['default']
    target = iso(chosen)
    values = dimension['values']
    # Fail visibly rather than silently substituting another day or forecast.
    for value in values:
        if '/' in value:
            start, end, *_ = value.split('/')
            if iso(start) <= target <= iso(end):
                return chosen
        elif iso(value) == target:
            return value
    if not values and chosen == dimension['default']:
        return chosen
    raise ValueError('Date/time is outside this layer\'s advertised availability. Select another date or dataset.')


def resolve_depth(layer, positive_depth):
    dimension = layer['dimensions'].get('elevation')
    if not dimension:
        return ''
    values = dimension['values'] or [dimension['default']]
    numeric = [(abs(abs(float(v)) - positive_depth), v) for v in values if '/' not in v]
    if not numeric:
        raise ValueError('This layer uses an unsupported depth range.')
    return min(numeric)[1]


def tile_plan(bounds, detail=1024):
    xmin, ymin, xmax, ymax = bounds
    if not all(math.isfinite(v) for v in bounds) or xmax <= xmin or ymax <= ymin:
        raise ValueError('Select a terrain mesh with a non-zero geographic area.')
    if min(xmin, ymin) < -HALF or max(xmax, ymax) > HALF or xmax - xmin > HALF:
        raise ValueError('This prototype supports local maps within Web Mercator, without dateline crossing.')
    zoom = max(0, min(18, int(math.ceil(math.log2(2 * HALF * detail / (256 * max(xmax-xmin, ymax-ymin)))))))
    while True:
        span = 2 * HALF / (2 ** zoom)
        c0 = max(0, int(math.floor((xmin + HALF) / span)))
        c1 = min(2 ** zoom - 1, int(math.ceil((xmax + HALF) / span) - 1))
        r0 = max(0, int(math.floor((HALF - ymax) / span)))
        r1 = min(2 ** zoom - 1, int(math.ceil((HALF - ymin) / span) - 1))
        if (c1-c0+1)*(r1-r0+1) <= 36:
            break
        zoom -= 1
    return dict(zoom=zoom, c0=c0, c1=c1, r0=r0, r1=r1,
                width=(c1-c0+1)*256, height=(r1-r0+1)*256,
                bounds=[c0*span-HALF, HALF-(r1+1)*span, (c1+1)*span-HALF, HALF-r0*span])


def uv_for(point, bounds):
    x0,y0,x1,y1 = bounds
    return ((point[0]-x0)/(x1-x0), (point[1]-y0)/(y1-y0))


def fetch_tiles(job, progress=lambda message: None):
    layer = job['layer']
    requested_time = resolve_time(layer, job['date'])
    elevation = resolve_depth(layer, job['depth'])
    palette = job['palette']
    cmap = 'gray' if palette in ('NEON', 'CYAN') else palette
    style = 'cmap:' + cmap
    lo, hi = job['range']
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        raise ValueError('Colour maximum must be greater than minimum.')
    style += ',range:' + str(lo) + '/' + str(hi)
    plan = tile_plan(job['bounds'], job['detail'])
    folder = Path(job['cache_dir']) / 'tiles'
    folder.mkdir(parents=True, exist_ok=True)
    pairs = [(col, row) for row in range(plan['r0'], plan['r1']+1)
             for col in range(plan['c0'], plan['c1']+1)]
    def fetch(pair):
        col, row = pair
        params = dict(SERVICE='WMTS', VERSION='1.0.0', REQUEST='GetTile', FORMAT='image/png',
                      LAYER=layer['id'], TILEMATRIXSET='EPSG:3857', TILEMATRIX=plan['zoom'],
                      TILECOL=col, TILEROW=row, STYLE=style)
        if requested_time:
            params['TIME'] = requested_time
        if elevation:
            params['ELEVATION'] = elevation
        url = url_for(**params)
        path = folder / (hashlib.sha256(url.encode()).hexdigest() + '.png')
        if not path.is_file():
            data = request_bytes(url, limit=2_000_000)
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Marine returned an error instead of a map tile: ' + data[:240].decode(errors='replace'))
            tmp = path.with_suffix('.part')
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return dict(col=col, row=row, path=str(path))
    tiles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=HTTP_WORKERS) as pool:
        for result in pool.map(fetch, pairs):
            tiles.append(result)
            progress('Loading Marine map: %s / %s tiles' % (len(tiles), len(pairs)))
    return dict(plan=plan, tiles=tiles, layer=layer, time=requested_time, elevation=elevation,
                palette=palette, style=style, range=[lo,hi],
                temporal=temporal_info(layer),
                period_label=time_label(layer, requested_time) if requested_time else 'Static field',
                loaded_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                map_crs='EPSG:3857', rendered_pixel_spacing_m=(plan['bounds'][2]-plan['bounds'][0])/plan['width'],
                attribution='Contains modified Copernicus Marine Service information',
                source='Copernicus Marine WMTS; rendered visualisation, not raw numeric data')


def atomic_json(path, value):
    tmp = Path(str(path) + '.part')
    tmp.write_text(json.dumps(value), encoding='utf8')
    os.replace(tmp, path)


def worker(job_path):
    global HTTP_TIMEOUT, HTTP_WORKERS
    job = json.loads(Path(job_path).read_text())
    result_path = job['result_path']
    HTTP_TIMEOUT = max(5, min(120, int(job.get('timeout', 35))))
    HTTP_WORKERS = max(1, min(8, int(job.get('parallel_downloads', 4))))
    try:
        if job['action'] in ('catalogue', 'connection'):
            result = dict(layers=catalogue(job['product'], job['cache_dir'],
                          ttl_seconds=job.get('catalogue_ttl_seconds', 3600),
                          force_refresh=job.get('force_refresh', False)),
                          checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'))
        else:
            result = fetch_tiles(job, lambda message: atomic_json(job['progress_path'], dict(message=message)))
        atomic_json(result_path, dict(ok=True, data=result))
    except Exception as exc:
        atomic_json(result_path, dict(ok=False, error=str(exc)))
        sys.exit(1)


if __name__ == '__main__':
    worker(sys.argv[1])
