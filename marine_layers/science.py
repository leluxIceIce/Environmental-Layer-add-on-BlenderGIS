# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded native Copernicus ARCO raster preparation, outside Blender's UI.

No RGB inversion, interpolated preview sampling or HTTP-per-cell. Reads the
catalogue's native-resolution timeChunked Zarr asset and only intersecting
chunks. The small Blosc/LZ4 decoder intentionally rejects other encodings.
"""
import bisect
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_CELLS = 250_000
MAX_CHUNKS = 16
MAX_BYTES = 12_000_000
BASE = 'https://stac.marine.copernicus.eu/metadata/'
WMTS = 'https://wmts.marine.copernicus.eu/teroWmts'


def _lz4(data, expected):
    """Decode an independent raw LZ4 block with strict output bounds."""
    src = memoryview(data); out = bytearray(); i = 0
    while i < len(src):
        token = src[i]; i += 1; length = token >> 4
        if length == 15:
            while True:
                if i >= len(src): raise ValueError('Truncated LZ4 literal length.')
                n = src[i]; i += 1; length += n
                if n != 255: break
        if i + length > len(src) or len(out) + length > expected: raise ValueError('Invalid LZ4 literal length.')
        out.extend(src[i:i+length]); i += length
        if i == len(src): break
        if i + 2 > len(src): raise ValueError('Truncated LZ4 offset.')
        offset = src[i] | src[i+1] << 8; i += 2
        if offset == 0 or offset > len(out): raise ValueError('Invalid LZ4 match offset.')
        length = (token & 15) + 4
        if token & 15 == 15:
            while True:
                if i >= len(src): raise ValueError('Truncated LZ4 match length.')
                n = src[i]; i += 1; length += n
                if n != 255: break
        if len(out) + length > expected: raise ValueError('LZ4 output exceeds chunk bounds.')
        # Repeated slices handle overlapping matches without per-byte Python work.
        while length:
            n = min(length, offset); out.extend(out[-offset:][:n]); length -= n
    if len(out) != expected: raise ValueError('LZ4 output size mismatch.')
    return out


def decode_blosc(data):
    if len(data) < 16: raise ValueError('Truncated Blosc header.')
    version, codec_version, flags, size = data[:4]
    count, block, compressed = struct.unpack_from('<III', data, 4)
    if version not in (1, 2) or not size or size > 16 or count > 8_388_608 or not block or compressed != len(data):
        raise ValueError('Unsupported or oversized Blosc chunk.')
    if flags & 2:
        if len(data) != count + 16: raise ValueError('Invalid Blosc memcpy size.')
        return data[16:]
    if flags & 4 or flags >> 5 != 1: raise ValueError('Only Blosc byte-shuffled LZ4 chunks are supported.')
    nblocks = (count + block - 1) // block
    if len(data) < 16 + nblocks * 4: raise ValueError('Truncated Blosc block table.')
    offsets = struct.unpack_from('<' + 'I' * nblocks, data, 16)
    result = bytearray()
    for b, start in enumerate(offsets):
        n = min(block, count - b * block)
        split = size if not flags & 16 and n // size >= 128 else 1
        if n % split: raise ValueError('Invalid Blosc split size.')
        stream = bytearray(); p = start
        for _ in range(split):
            if p < 16 + nblocks * 4 or p + 4 > len(data): raise ValueError('Invalid Blosc block offset.')
            length = struct.unpack_from('<I', data, p)[0]; p += 4
            if p + length > len(data): raise ValueError('Truncated Blosc stream.')
            raw = data[p:p+length]; p += length
            stream.extend(raw if length == n // split else _lz4(raw, n // split))
        if len(stream) != n: raise ValueError('Invalid decoded Blosc block.')
        if flags & 1:
            elements = n // size; dest = bytearray(n)
            for byte in range(size): dest[byte:elements*size:size] = stream[byte*elements:(byte+1)*elements]
            result.extend(dest)
        else: result.extend(stream)
    if len(result) != count: raise ValueError('Blosc output size mismatch.')
    return bytes(result)


def request(url, cache, limit=MAX_BYTES):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'stac.marine.copernicus.eu','s3.waw3-1.cloudferro.com','s3.waw4-1.cloudferro.com','wmts.marine.copernicus.eu'}:
        raise ValueError('Unsupported numerical source host.')
    key = hashlib.sha256(url.encode()).hexdigest()
    path = Path(cache) / (key + '.bin'); path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file() and path.stat().st_size <= limit and time.time()-path.stat().st_mtime < 86400: return path.read_bytes()
    ctx = ssl.create_default_context()
    if sys.platform == 'darwin' and Path('/etc/ssl/cert.pem').is_file(): ctx.load_verify_locations(cafile='/etc/ssl/cert.pem')
    req = urllib.request.Request(url, headers={'User-Agent':'BlenderGIS-Marine-Companion/0.4.3'})
    try:
        with urllib.request.urlopen(req, timeout=35, context=ctx) as r: data = r.read(limit+1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403): raise ValueError('Native numeric source requires authenticated access. No raster was exported.') from exc
        raise
    if len(data) > limit: raise ValueError('Native data response exceeds its size bound.')
    tmp = path.with_suffix('.tmp'); tmp.write_bytes(data); tmp.replace(path)
    return data


def key_for(metadata):
    identity = {k:metadata.get(k) for k in ('time','elevation','requested_bounds','map_crs','style')}
    identity['layer'] = metadata.get('layer',{}).get('id')
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def _array(url, name, meta, cache):
    config = meta[name+'/.zarray']; attrs = meta[name+'/.zattrs']
    if len(config['shape']) != 1 or config['shape'][0] > 200_000 or config.get('filters'):
        raise ValueError('Unsupported native coordinate array.')
    if config['dtype'] not in ('<f8','<f4'): raise ValueError('Unsupported native coordinate type.')
    fmt = 'd' if config['dtype'] == '<f8' else 'f'; output=[]
    for index in range((config['shape'][0]+config['chunks'][0]-1)//config['chunks'][0]):
        data=request(url+'/'+name+'/'+str(index),cache)
        raw=decode_blosc(data) if config.get('compressor') else data
        output.extend(v[0] for v in struct.iter_unpack('<'+fmt,raw))
    return output[:config['shape'][0]],attrs


def _iso(value):
    return dt.datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(dt.timezone.utc)


def legend(metadata,cache):
    q={'SERVICE':'WMTS','VERSION':'1.0.0','REQUEST':'GetLegend','LAYER':metadata['layer']['id'],
       'STYLE':metadata.get('style',''),'FORMAT':'application/json','TIME':metadata.get('time','')}
    if metadata.get('elevation'): q['ELEVATION']=metadata['elevation']
    result=json.loads(request(WMTS+'?'+urllib.parse.urlencode(q),cache));result['style']=metadata.get('style','');return result


def prepare(metadata,cache):
    layer_id=metadata.get('layer',{}).get('id',''); parts=layer_id.split('/')
    if len(parts)!=3: raise ValueError('Missing exact Copernicus product/dataset/variable identity.')
    product,dataset,variable=parts
    if variable!='CHL': raise ValueError('Native numerical web export currently supports CHL. The original map remains available for other variables.')
    for part in parts:
        if not part or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:' for c in part): raise ValueError('Invalid catalogue identifier.')
    catalogue_url=BASE+product+'/'+dataset+'/dataset.stac.json'
    stac=json.loads(request(catalogue_url,cache))
    asset=stac.get('assets',{}).get('timeChunked')
    if not asset: raise ValueError('This dataset has no native-resolution ARCO timeChunked asset.')
    url=asset['href']; z=json.loads(request(url+'/.zmetadata',cache))['metadata']
    a=z.get(variable+'/.zarray'); attrs=z.get(variable+'/.zattrs',{})
    if not a or attrs.get('_ARRAY_DIMENSIONS')!=['time','latitude','longitude'] or a.get('dtype')!='<f4' or a.get('order')!='C' or a.get('filters'):
        raise ValueError('Unsupported native CHL grid layout. No interpolated substitute was used.')
    compressor=a.get('compressor',{})
    if compressor.get('id')!='blosc' or compressor.get('cname')!='lz4' or compressor.get('shuffle')!=1:
        raise ValueError('Unsupported native CHL compression.')
    lons,_=_array(url,'longitude',z,cache);lats,_=_array(url,'latitude',z,cache);times,tattrs=_array(url,'time',z,cache)
    if not all(x<y for x,y in zip(lons,lons[1:])) or not all(x<y for x,y in zip(lats,lats[1:])):raise ValueError('Native coordinate arrays must be strictly ascending.')
    units=tattrs.get('units','')
    if not units.startswith('seconds since '):raise ValueError('Unsupported native time encoding.')
    base=_iso(units.split('since ',1)[1]+'T00:00:00Z')
    exact=_iso(metadata['time']);matches=[i for i,t in enumerate(times) if abs((base+dt.timedelta(seconds=t)-exact).total_seconds())<.001]
    if len(matches)!=1:raise ValueError('The exact displayed month is not present in the native numerical dataset.')
    ti=matches[0];bounds=metadata.get('requested_bounds') or metadata.get('plan',{}).get('bounds')
    if len(bounds or [])!=4 or '3857' not in metadata.get('map_crs','EPSG:3857'):raise ValueError('Numeric extraction needs the layer bounds in EPSG:3857.')
    R=6378137;west=bounds[0]/R*180/math.pi;east=bounds[2]/R*180/math.pi
    south=(2*math.atan(math.exp(bounds[1]/R))-math.pi/2)*180/math.pi;north=(2*math.atan(math.exp(bounds[3]/R))-math.pi/2)*180/math.pi
    # Include native cells intersecting the footprint, using their midpoint edges.
    dx=(lons[-1]-lons[0])/(len(lons)-1);dy=(lats[-1]-lats[0])/(len(lats)-1)
    x0=max(0,bisect.bisect_left(lons,west-dx/2));x1=min(len(lons),bisect.bisect_right(lons,east+dx/2))
    y0=max(0,bisect.bisect_left(lats,south-dy/2));y1=min(len(lats),bisect.bisect_right(lats,north+dy/2))
    if x0>=x1 or y0>=y1:raise ValueError('The native numeric raster does not cover this map.')
    cells=(x1-x0)*(y1-y0)
    if cells>MAX_CELLS:raise ValueError('Select a smaller map: native raster exceeds 250,000 cells.')
    ct,cy,cx=a['chunks'];chunk_ids=[(ti//ct,y,x) for y in range(y0//cy,(y1-1)//cy+1) for x in range(x0//cx,(x1-1)//cx+1)]
    if len(chunk_ids)>MAX_CHUNKS or ct*cy*cx*4>8_388_608:raise ValueError('Native subset exceeds the bounded chunk limit.')
    rows=[[None]*(x1-x0) for _ in range(y1-y0)];valid=0
    for t,y,x in chunk_ids:
        raw=decode_blosc(request(url+'/'+variable+'/%d.%d.%d'%(t,y,x),cache))
        if len(raw)!=ct*cy*cx*4:raise ValueError('Native CHL chunk size mismatch.')
        for j in range(max(y0,y*cy),min(y1,(y+1)*cy)):
            for i in range(max(x0,x*cx),min(x1,(x+1)*cx)):
                offset=(((ti%ct)*cy+j-y*cy)*cx+i-x*cx)*4;v=struct.unpack_from('<f',raw,offset)[0]
                if math.isfinite(v) and v>=attrs.get('valid_min',-float('inf')) and v<=attrs.get('valid_max',float('inf')):
                    rows[j-y0][i-x0]=v;valid+=1
    raster={'schema':'marine-science-raster/1','crs':'EPSG:4326','longitude':lons[x0:x1],'latitude':lats[y0:y1],
            'values':rows,'variable':variable,'title':attrs.get('long_name',variable),'unit':attrs.get('units',''),
            'time':exact.isoformat().replace('+00:00','Z'),'depth':None,'no_data':None,
            'vertical_support':'surface ocean-colour retrieval; no depth dimension',
            'source':{'dataset_id':dataset,'product_id':product,'service':'Copernicus Marine native ARCO Zarr','url':url,'catalogue':catalogue_url,'resolution':'native','interpolation':'none'},
            'coverage':{'valid_cells':valid,'total_cells':cells,'fraction':valid/cells},
            'requested_bounds_wgs84':[west,south,east,north],'resolution_degrees':[dx,dy],
            'prepared_at_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
    output={'science_raster':raster,'legend':legend(metadata,cache),'identity':key_for(metadata)}
    target=Path(cache)/('science_'+key_for(metadata)+'.json');tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(output,allow_nan=False),encoding='utf8');tmp.replace(target)
    return {'cache_file':str(target),'valid_cells':valid,'total_cells':cells,'time':raster['time']}


def cached(metadata,cache):
    path=Path(cache)/('science_'+key_for(metadata)+'.json')
    if not path.is_file():raise ValueError('Prepare the numerical CHL raster first (GIS > Environmental Layer > Prepare numeric raster). No web package was changed.')
    data=json.loads(path.read_text(encoding='utf8'))
    if data.get('identity')!=key_for(metadata):raise ValueError('Numeric cache does not match the selected layer/time/extent. Prepare it again.')
    return data


def worker(job_path):
    job=json.loads(Path(job_path).read_text());target=Path(job['result_path'])
    try:result={'ok':True,'data':prepare(job['metadata'],job['cache_dir'])}
    except Exception as exc:result={'ok':False,'error':str(exc)}
    tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(result));tmp.replace(target)

if __name__=='__main__':worker(sys.argv[1])
