# SPDX-License-Identifier: GPL-3.0-or-later
"""Copernicus Marine image layers for existing BlenderGIS terrain materials."""
bl_info = {
    'name': 'BlenderGIS Marine Layers', 'author': 'Codex', 'version': (0, 4, 1),
    'blender': (4, 2, 0), 'location': 'GIS > Web geodata > Environmental Layer',
    'description': 'Blend georeferenced Copernicus Marine maps into existing terrain',
    'category': '3D View',
}

import datetime as dt
import importlib
import json
import math
from pathlib import Path
import textwrap
import shutil
import subprocess
import sys
import tempfile
import uuid
from types import SimpleNamespace

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy_extras.io_utils import ExportHelper
from mathutils import Vector
from . import core

UV_NAME = 'CMEMS_Marine_UV'
PREFIX = 'CMEMS_'
_ENUM_CACHE = {}
_TIME_CACHE = {}
_JOB = None
_READY = None


def preferences(context):
    entry = context.preferences.addons.get(__package__)
    if entry and hasattr(entry.preferences, 'cache_folder'):
        return entry.preferences
    return SimpleNamespace(cache_folder='', timeout=35, parallel_downloads=4, catalogue_hours=1,
                           default_detail='1024', default_filter='Linear')


def cache_folder(context):
    custom = preferences(context).cache_folder
    path = Path(bpy.path.abspath(custom)).expanduser() if custom else Path(
        bpy.utils.user_resource('DATAFILES', path='marine_layers/cache', create=True))
    path.mkdir(parents=True, exist_ok=True)
    return path


def wrapped(layout, text, width=43, icon=None):
    for index, line in enumerate(textwrap.wrap(str(text), width=width) or ['']):
        if index == 0 and icon:
            layout.label(text=line, icon=icon)
        else:
            layout.label(text=line)


def time_items(self, context):
    layer = current_layer(self)
    key = (self.catalogue_json, self.layer) if layer else ('', '')
    if key not in _TIME_CACHE:
        values = core.available_times(layer) if layer else []
        _TIME_CACHE[key] = [(v, core.time_label(layer,v), v) for v in values] or [('NONE','Use manual UTC time','')]
    return _TIME_CACHE[key]


def select_time(self, context):
    if self.date_pick != 'NONE':
        self.date = self.date_pick


def read_layers(p):
    try:
        return json.loads(p.catalogue_json)
    except (ValueError, TypeError):
        return []


def layer_items(self, context):
    key = self.catalogue_json
    if key not in _ENUM_CACHE:
        _ENUM_CACHE[key] = [(l['id'], l['title'][:64] + ' · ' + core.temporal_info(l)['cadence'],
                             l['id']) for l in read_layers(self)] or [('NONE', 'Load available layers first', '')]
    return _ENUM_CACHE[key]


def current_layer(p):
    return next((l for l in read_layers(p) if l['id'] == p.layer), None)


def select_layer(self, context):
    layer = current_layer(self)
    if not layer:
        return
    try:
        self.minimum = float(layer['minimum'])
        self.maximum = float(layer['maximum'])
    except (ValueError, TypeError):
        self.minimum, self.maximum = 0, 1
    if layer['id'].startswith('OCEANCOLOUR_NWS_BGC_HR_'):
        # Coastal display defaults for the Nykøbing Mors workflow; still editable.
        coastal_ranges = {'CHL': (0, 40), 'TUR': (0, 15), 'SPM': (0, 10)}
        variable = layer['id'].rsplit('/', 1)[-1]
        if variable in coastal_ranges:
            self.minimum, self.maximum = coastal_ranges[variable]
    dimension = layer['dimensions'].get('time', {})
    value = dimension.get('default', '')
    # Do not silently start on tomorrow's forecast when today's data is available.
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if value and value[:10] > today:
        candidate = today + 'T00:00:00Z'
        try:
            core.resolve_time(layer, candidate)
            value = candidate
        except ValueError:
            pass
    self.date = value
    choices = core.available_times(layer)
    matching = next((v for v in choices if core.iso(v) == core.iso(value)), None) if value else None
    if matching:
        self.date_pick = matching
        self.time_mode = 'PICK'
    else:
        self.time_mode = 'MANUAL'


def change_product(self, context):
    self.catalogue_json = '[]'
    self.status = 'Load available layers for this product.'


def gis_modules():
    for name, module in tuple(sys.modules.items()):
        if name.endswith('.geoscene') and hasattr(module, 'GeoScene'):
            return module, importlib.import_module(name.rsplit('.', 1)[0] + '.core.proj.reproj')
    raise ValueError('Enable BlenderGIS for geographic scene coordinates.')


def geo_signature(scene):
    return tuple(scene.get(k) for k in ('SRID', 'crs x', 'crs y', 'scale'))


def projected_vertices(obj, scene):
    geo_module, reproj = gis_modules()
    geo = geo_module.GeoScene(scene)
    if not geo.isGeoref:
        raise ValueError('This scene needs its BlenderGIS projection and map origin.')
    world = [obj.matrix_world @ v.co for v in obj.data.vertices]
    points = [geo.view3dToProj(v.x, v.y) for v in world]
    return crs_to_mercator(points,geo.crs,reproj)


def crs_to_mercator(points,crs,reproj):
    srs = reproj.SRS(crs)
    if srs.isWM:
        return points
    if srs.isWGS84:
        return [reproj.lonLatToWebMerc(*p) for p in points]
    if srs.isUTM:
        utm = reproj.UTM.init_from_epsg(srs)
        return [reproj.lonLatToWebMerc(*utm.utm_to_lonlat(*p)) for p in points]
    raise ValueError('First version supports Web Mercator, WGS84 and UTM scenes. Use one of these in BlenderGIS.')


def standalone_bounds(context):
    p=context.scene.cmems
    if p.area_mode=='MAP':
        active=context.active_object
        obj=active if active and active.type=='EMPTY' and active.empty_display_type=='IMAGE' else p.target or active
        if obj and obj.type=='MESH':
            points=projected_vertices(obj,context.scene)
        elif obj and obj.type=='EMPTY' and obj.empty_display_type=='IMAGE' and obj.data:
            geo_module,reproj=gis_modules()
            root=geo_module.__package__
            raster_class=importlib.import_module(root+'.core.georaster').GeoRaster
            raster=raster_class(bpy.path.abspath(obj.data.filepath))
            box=raster.bbox
            corners=[(box.xmin,box.ymin),(box.xmax,box.ymin),(box.xmax,box.ymax),(box.xmin,box.ymax)]
            points=crs_to_mercator(corners,geo_module.GeoScene(context.scene).crs,reproj)
        else:
            raise ValueError('Select the BlenderGIS map or choose Custom geographic bounds.')
        xs,ys=zip(*points)
        return [min(xs),min(ys),max(xs),max(ys)]
    if p.west>=p.east or p.south>=p.north:
        raise ValueError('West must be smaller than east, and south smaller than north.')
    def xy(lon,lat):
        return (lon*core.HALF/180,math.log(math.tan(math.pi/4+math.radians(lat)/2))*core.HALF/math.pi)
    return [*xy(p.west,p.south),*xy(p.east,p.north)]


def create_standalone_surface(scene,bounds,result,props):
    """Create a flat, georeferenced field surface; scientific values never become height."""
    keys=('SRID','crs x','crs y','latitude','longitude','scale')
    before={key:scene[key] for key in keys if key in scene}
    obj=None;mesh=None
    try:
        geo_module,reproj=gis_modules()
        geo=geo_module.GeoScene(scene)
        if not geo.isGeoref:
            if geo.isPartiallyGeoref:
                raise ValueError('Finish the existing BlenderGIS scene georeference before creating a surface.')
            geo.crs='EPSG:3857';geo.setOriginPrj((bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2)
            geo.scale=1
        srs=reproj.SRS(geo.crs)
        utm=reproj.UTM.init_from_epsg(srs) if srs.isUTM else None
        def local(x,y):
            if not srs.isWM:
                lon,lat=reproj.webMercToLonLat(x,y)
                if srs.isWGS84:x,y=lon,lat
                elif utm:x,y=utm.lonlat_to_utm(lon,lat)
                else:raise ValueError('Standalone surfaces support Web Mercator, WGS84 and UTM scenes.')
            return ((x-geo.crsx)/geo.scale,(y-geo.crsy)/geo.scale,props.surface_z)
        divisions=16
        points=[(bounds[0]+(bounds[2]-bounds[0])*i/divisions,
                 bounds[1]+(bounds[3]-bounds[1])*j/divisions)
                for j in range(divisions+1) for i in range(divisions+1)]
        vertices=[local(x,y) for x,y in points]
        faces=[(j*(divisions+1)+i,j*(divisions+1)+i+1,(j+1)*(divisions+1)+i+1,(j+1)*(divisions+1)+i)
               for j in range(divisions) for i in range(divisions)]
        mesh=bpy.data.meshes.new('Environmental surface')
        mesh.from_pydata(vertices,[],faces);mesh.update()
        name='Environment · '+result['layer']['id'].rsplit('/',1)[-1]+' · '+result['time'][:10]
        obj=bpy.data.objects.new(name,mesh);scene.collection.objects.link(obj)
        payload=dict(result,geometry='Flat georeferenced environmental surface; not DEM or bathymetry')
        apply_layer(obj,scene,points,payload,props)
        obj.cmems_state.standalone=True;obj.cmems_state.opacity=1
        for slot in obj.material_slots:
            tree=slot.material.node_tree
            alpha=tree.nodes.get('CMEMS_0_Opacity')
            if alpha:
                for node in tree.nodes:
                    if node.type=='BSDF_PRINCIPLED':tree.links.new(alpha.outputs[0],node.inputs['Alpha'])
            if hasattr(slot.material,'surface_render_method'):slot.material.surface_render_method='DITHERED'
        props.target=obj
        props.placement='EXISTING'  # Later loads update this surface instead of stacking duplicates.
        if mesh.users==0:bpy.data.meshes.remove(mesh)
        return obj
    except Exception:
        original_name=mesh.name if mesh else None
        if obj:
            current=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if current.users==0:bpy.data.meshes.remove(current)
        remaining=bpy.data.meshes.get(original_name) if original_name else None
        if remaining and remaining.users==0:bpy.data.meshes.remove(remaining)
        for key in keys:
            if key in before:scene[key]=before[key]
            elif key in scene:del scene[key]
        raise


def active_terrain(context):
    p=context.scene.cmems
    obj=p.target
    if obj is None:
        candidates=[o for o in context.scene.objects if o.type=='MESH' and o.data.polygons]
        exports=[o for o in candidates if o.name.lower().startswith(('export_','srtm','gmrt','dem')) or
                 any(modifier.type=='DISPLACE' for modifier in o.modifiers)]
        if len(exports)==1:obj=exports[0]
        elif context.active_object in candidates:obj=context.active_object
        elif len(candidates)==1:obj=candidates[0]
    if obj is None or obj.type!='MESH':
        raise ValueError('Choose your existing map in Target terrain. For a live BlenderGIS basemap, first press E in its map view to export it as a mesh.')
    if obj.mode!='OBJECT':
        raise ValueError('Target terrain is in Edit Mode. Switch it to Object Mode and load again.')
    p.target=obj
    if not obj.data.vertices or not obj.data.polygons:
        raise ValueError('Select a terrain mesh with faces.')
    if obj.library or obj.data.library:
        raise ValueError('Make the terrain local before applying a layer.')
    return obj


def terrain_poll(self,obj):
    return obj.type=='MESH' and bool(obj.data.polygons)


def existing_basemap(scene):
    """Read BlenderGIS's own image footprint, without creating a second map."""
    geo_module,_=gis_modules()
    raster_class=importlib.import_module(geo_module.__package__+'.core.georaster').GeoRaster
    images=[o for o in scene.objects if o.type=='EMPTY' and o.empty_display_type=='IMAGE' and o.data]
    images.sort(key=lambda o:(o.hide_viewport,not o.name.startswith(('GOOGLE','ESRI','OSM'))))
    for obj in images:
        try:
            raster=raster_class(bpy.path.abspath(obj.data.filepath))
            box=raster.bbox
            return dict(image=obj.data,bounds=[box.xmin,box.ymin,box.xmax,box.ymax],name=obj.name)
        except Exception:continue
    return None


def shader_targets(mat):
    if mat is None or not mat.use_nodes:
        return []
    return [n.inputs['Base Color'] if n.type=='BSDF_PRINCIPLED' else n.inputs['Color']
            for n in mat.node_tree.nodes if n.type in ('BSDF_PRINCIPLED','BSDF_DIFFUSE','EMISSION')]


def prepare_material(original):
    mat=original.copy() if original else bpy.data.materials.new('Environmental layer')
    colour=tuple(mat.diffuse_color)
    if not mat.use_nodes:
        mat.use_nodes=True
        mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value=colour
    if not shader_targets(mat):
        tree=mat.node_tree
        output=next((n for n in tree.nodes if n.type=='OUTPUT_MATERIAL' and n.is_active_output),None)
        if output is None:output=tree.nodes.new('ShaderNodeOutputMaterial')
        previous=output.inputs['Surface'].links[0].from_socket if output.inputs['Surface'].is_linked else None
        marine=tree.nodes.new('ShaderNodeBsdfPrincipled')
        marine.inputs['Base Color'].default_value=colour
        if previous:
            mix=tree.nodes.new('ShaderNodeMixShader');mix.name='CMEMS_SurfaceOverlay'
            tree.links.new(previous,mix.inputs[1]);tree.links.new(marine.outputs[0],mix.inputs[2])
            tree.links.new(mix.outputs[0],output.inputs['Surface'])
            mat['cmems_surface_overlay']=True
        else:tree.links.new(marine.outputs[0],output.inputs['Surface'])
    return mat


def ramp_colours(node, palette):
    ramp = node.color_ramp
    while len(ramp.elements) > 2:
        ramp.elements.remove(ramp.elements[-1])
    ramp.elements[0].position = 0
    ramp.elements[1].position = 1
    if palette == 'NEON':
        colours = [(0.005, 0.012, 0.008, 1), (0.48, 1.0, 0.005, 1)]
    else:
        colours = [(0.002, 0.014, 0.022, 1), (0.35, 0.9, 1.0, 1)]
    for element, colour in zip(ramp.elements, colours):
        element.color = colour


def add_material_nodes(material, image, palette, props):
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    for index, target in enumerate(shader_targets(material)):
        prefix = PREFIX + str(index) + '_'
        mix = nodes.get(prefix + 'Mix')
        if mix is None:
            old_socket = target.links[0].from_socket if target.is_linked else None
            old_value = tuple(target.default_value)
            saturation = nodes.new('ShaderNodeHueSaturation'); saturation.name = prefix + 'Satellite'
            saturation.label = 'Satellite saturation'
            if old_socket:
                links.new(old_socket, saturation.inputs['Color'])
            else:
                saturation.inputs['Color'].default_value = old_value
            contrast = nodes.new('ShaderNodeBrightContrast'); contrast.name = prefix + 'Contrast'
            links.new(saturation.outputs['Color'], contrast.inputs['Color'])
            tex = nodes.new('ShaderNodeTexImage'); tex.name = prefix + 'Image'; tex.label = 'Copernicus Marine map'
            tex.extension = 'CLIP'; tex.interpolation = 'Linear'
            uv = nodes.new('ShaderNodeUVMap'); uv.name = prefix + 'UV'; uv.uv_map = UV_NAME
            links.new(uv.outputs['UV'], tex.inputs['Vector'])
            ramp = nodes.new('ShaderNodeValToRGB'); ramp.name = prefix + 'Ramp'; ramp.label = 'Artistic Marine colours'
            links.new(tex.outputs['Color'], ramp.inputs['Fac'])
            alpha = nodes.new('ShaderNodeMath'); alpha.name = prefix + 'Opacity'; alpha.operation = 'MULTIPLY'
            links.new(tex.outputs['Alpha'], alpha.inputs[0])
            mix = nodes.new('ShaderNodeMixRGB'); mix.name = prefix + 'Mix'; mix.label = 'Marine over satellite'
            links.new(alpha.outputs[0], mix.inputs[0])
            links.new(contrast.outputs['Color'], mix.inputs[1])
            links.new(mix.outputs[0], target)
            for n, loc in [(uv,(-1000,-400)),(tex,(-800,-400)),(ramp,(-500,-400)),
                           (saturation,(-800,100)),(contrast,(-500,100)),(alpha,(-300,-400)),(mix,(-100,100))]:
                n.location = loc[0], loc[1] - index*800
        tex = nodes[prefix + 'Image']
        tex.image = image
        ramp = nodes[prefix + 'Ramp']
        ramp_colours(ramp, palette)
        colour=ramp.outputs['Color'] if palette in ('NEON','CYAN') else tex.outputs['Color']
        links.new(colour,mix.inputs[2])
        if material.get('cmems_surface_overlay'):
            links.new(colour,target)
            links.new(nodes[prefix+'Opacity'].outputs[0],nodes['CMEMS_SurfaceOverlay'].inputs[0])
    material['cmems_palette'] = palette
    update_material(material, props)


def update_material(mat, props):
    if not mat or not mat.use_nodes:
        return
    for node in mat.node_tree.nodes:
        if not node.name.startswith(PREFIX):
            continue
        if node.name.endswith('_Opacity'):
            node.inputs[1].default_value = props.opacity
        elif node.name.endswith('_Satellite'):
            node.inputs['Saturation'].default_value = props.saturation
        elif node.name.endswith('_Contrast'):
            node.inputs['Contrast'].default_value = props.contrast
        elif node.name.endswith('_Mix'):
            node.blend_type = props.blend
        elif node.name.endswith('_Image'):
            node.interpolation = getattr(props, 'interpolation', 'Linear')


def live_style(self, context):
    owner = self.id_data
    obj = owner if isinstance(owner,bpy.types.Object) else context.active_object if context else None
    if obj and obj.type == 'MESH' and obj.cmems_state.applied:
        if isinstance(owner,bpy.types.Scene):
            for key in ('opacity','blend','saturation','contrast','interpolation'):
                setattr(obj.cmems_state,key,getattr(self,key))
            return
        for slot in obj.material_slots:
            update_material(slot.material, self)


def make_mosaic(result, name):
    import numpy as np
    plan = result['plan']
    pixels = np.zeros((plan['height'], plan['width'], 4), dtype=np.float32)
    for tile in result['tiles']:
        img = bpy.data.images.load(tile['path'], check_existing=False)
        try:
            if tuple(img.size) != (256,256):
                raise ValueError('Unexpected map tile dimensions; expected 256 by 256.')
            values = np.empty(256*256*4, dtype=np.float32)
            img.pixels.foreach_get(values)
            values = values.reshape((256,256,4))
            x = (tile['col']-plan['c0'])*256
            y = (plan['r1']-tile['row'])*256  # Blender pixel rows start at the bottom.
            pixels[y:y+256,x:x+256] = values
        finally:
            bpy.data.images.remove(img)
    relevant=pixels[:,:,3]
    bounds=result.get('requested_bounds')
    if bounds:
        x0,y0,x1,y1=plan['bounds'];rx=(x1-x0)/plan['width'];ry=(y1-y0)/plan['height']
        left=max(0,int(math.floor((bounds[0]-x0)/rx)));right=min(plan['width'],int(math.ceil((bounds[2]-x0)/rx)))
        bottom=max(0,int(math.floor((bounds[1]-y0)/ry)));top=min(plan['height'],int(math.ceil((bounds[3]-y0)/ry)))
        relevant=relevant[bottom:top,left:right]
    if not np.any(relevant > 0):
        raise ValueError('No Marine data in this map area at that date/depth. Try a larger area or another layer.')
    img = bpy.data.images.new(name, plan['width'], plan['height'], alpha=True)
    img.alpha_mode = 'STRAIGHT'
    img.colorspace_settings.name = 'Non-Color' if result['palette'] in ('NEON', 'CYAN') else 'sRGB'
    img.pixels.foreach_set(pixels.ravel())
    img.update()
    # Store as packed PNG: remains available when the blend file is reopened.
    img.pack()
    img['cmems_metadata'] = json.dumps({k:v for k,v in result.items() if k != 'tiles'})
    return img


def is_environment_material(material):
    return bool(material and material.use_nodes and (
        material.get('cmems_palette') or material.node_tree.nodes.get('CMEMS_0_Image')))


def material_records(obj):
    """Reconcile current slots with owned materials; keep user edits as the new baseline."""
    state=obj.cmems_state
    previous=[dict(material=b.material,link=b.link,managed=b.managed_material,
                   created=b.created,key=b.key) for b in state.backups]
    used=set();records=[];legacy_created=False
    for index,slot in enumerate(obj.material_slots):
        current=slot.material
        key=current.get('cmems_backup_key','') if current else ''
        match=next((i for i,record in enumerate(previous) if i not in used and (
            (record['managed'] is not None and record['managed']==current) or
            (key and record['key']==key))),None)
        if match is None and state.applied and is_environment_material(current) and index<len(previous):
            # Older files only stored original materials by slot index.
            if previous[index]['managed'] is None and index not in used:match=index
        if match is not None:
            used.add(match);record=dict(previous[match])
        elif (state.applied and not previous and state.original_slot_count in (-1,0)
              and is_environment_material(current) and not legacy_created):
            record=dict(material=None,link='DATA',managed=current,created=True,key='')
            legacy_created=True
        else:
            # A new/replaced material is the user's current baseline, not an error.
            record=dict(material=current,link=slot.link,managed=None,created=False,key='')
        records.append(record)
    return records


def apply_layer(obj, scene, points, result, props):
    state = obj.cmems_state
    style = state if state.applied and state.style_initialized else props
    result = dict(result)
    if state.applied and state.metadata:
        previous=json.loads(state.metadata)
        if previous.get('basemap_source'):result['basemap_source']=previous['basemap_source']
    if state.standalone:result['geometry']='Flat georeferenced environmental surface; not DEM or bathymetry'
    result['scene_crs'] = str(scene.get('SRID',''))
    result['scene_origin'] = [scene.get('crs x'),scene.get('crs y')]
    result['scene_scale'] = scene.get('scale',1)
    if not state.applied and obj.data.uv_layers.get(UV_NAME):
        raise ValueError('Reserved Marine UV map already exists. Rename it before adding a layer.')
    records=material_records(obj)
    if not records:records=[dict(material=None,link='DATA',managed=None,created=True,key='')]
    image = make_mosaic(result, 'Marine · ' + result['layer']['title'])
    staged_materials = []
    staged_mesh = None
    pending_basemap_uv=''
    basemap=None
    if not state.applied and props.placement=='EXISTING' and props.use_basemap and (
            not obj.material_slots or any(not slot.material or not slot.material.use_nodes for slot in obj.material_slots)):
        basemap=existing_basemap(scene)
    try:
        originals=[slot.material for slot in obj.material_slots] or [None]
        for original in originals:
            material = prepare_material(original)
            staged_materials.append(material)
            if not state.applied:
                material.name = (original.name if original else obj.name) + ' · Environment'
            if basemap and (original is None or not original.use_nodes):
                tree=material.node_tree
                uvnode=tree.nodes.new('ShaderNodeUVMap');uvnode.name='CMEMS_BasemapCoords';uvnode.uv_map='CMEMS_Basemap_UV'
                texture=tree.nodes.new('ShaderNodeTexImage');texture.name='CMEMS_Basemap';texture.image=basemap['image']
                texture.extension='CLIP'
                tree.links.new(uvnode.outputs['UV'],texture.inputs['Vector'])
                for target in shader_targets(material):tree.links.new(texture.outputs['Color'],target)
            add_material_nodes(material, image, result['palette'], style)
            if state.standalone:
                tree=material.node_tree
                alpha=tree.nodes.get('CMEMS_0_Opacity')
                if alpha:
                    for node in tree.nodes:
                        if node.type=='BSDF_PRINCIPLED':tree.links.new(alpha.outputs[0],node.inputs['Alpha'])
                if hasattr(material,'surface_render_method'):material.surface_render_method='DITHERED'
        staged_mesh = obj.data.copy()
        if not staged_mesh.materials:staged_mesh.materials.append(None)
        old_render_uv = next((u.name for u in staged_mesh.uv_layers if u.active_render), None)
        old_active_uv = staged_mesh.uv_layers.active.name if staged_mesh.uv_layers.active else None
        uv = staged_mesh.uv_layers.get(UV_NAME) or staged_mesh.uv_layers.new(name=UV_NAME)
        if old_render_uv:
            staged_mesh.uv_layers[old_render_uv].active_render = True
        if old_active_uv:
            staged_mesh.uv_layers.active = staged_mesh.uv_layers[old_active_uv]
        values = [core.uv_for(point, result['plan']['bounds']) for point in points]
        uv.data.foreach_set('uv', [value for loop in staged_mesh.loops for value in values[loop.vertex_index]])
        if basemap:
            geo_module,_=gis_modules();geo=geo_module.GeoScene(scene)
            bbox=basemap['bounds']
            baseuv=staged_mesh.uv_layers.new(name='CMEMS_Basemap_UV')
            xy=[obj.matrix_world@v.co for v in obj.data.vertices]
            values=[core.uv_for(geo.view3dToProj(v.x,v.y),bbox) for v in xy]
            baseuv.data.foreach_set('uv',[value for loop in staged_mesh.loops for value in values[loop.vertex_index]])
            for material in staged_materials:
                node=material.node_tree.nodes.get('CMEMS_BasemapCoords')
                if node:node.uv_map=baseuv.name
            result['basemap_source']=basemap['name']
            pending_basemap_uv=baseuv.name
            if old_active_uv:staged_mesh.uv_layers.active=staged_mesh.uv_layers[old_active_uv]
            if old_render_uv:staged_mesh.uv_layers[old_render_uv].active_render=True
    except Exception:
        if staged_mesh:
            bpy.data.meshes.remove(staged_mesh)
        for material in staged_materials:
            bpy.data.materials.remove(material)
        bpy.data.images.remove(image)
        raise
    # Commit only once the new image, materials and UV mapping are all ready.
    if not state.applied:state.original_slot_count=len(obj.material_slots)
    obj.data = staged_mesh
    if pending_basemap_uv:state.basemap_uv=pending_basemap_uv
    for slot, material in zip(obj.material_slots, staged_materials):
        slot.link = 'OBJECT'
        slot.material = material
    state.backups.clear()
    for record,material in zip(records,staged_materials):
        backup=state.backups.add()
        backup.material=record['material'];backup.link=record['link']
        backup.created=record['created'];backup.managed_material=material
        backup.key=record['key'] or uuid.uuid4().hex
        material['cmems_backup_key']=backup.key
    state.metadata = json.dumps({k:v for k,v in result.items() if k != 'tiles'})
    state.applied = True
    if not state.style_initialized:
        for key in ('opacity','blend','saturation','contrast','interpolation'):
            setattr(state,key,getattr(props,key))
        state.style_initialized=True
    depth = (' · depth ' + result['elevation'] + ' m') if result['elevation'] else ''
    props.status = 'Loaded: ' + result['time'] + depth
    props.loaded_label = result['layer']['title']
    props.loaded_units = result['layer']['unit']
    # Ensure the result can be seen in the viewport without altering lighting/render settings.
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D' and area.spaces.active.shading.type == 'SOLID':
                area.spaces.active.shading.color_type = 'TEXTURE'


def copy_environment_target(source,scene,points,result,props,hide_previous=True):
    """Create one private version; commit visibility/selection only after success."""
    duplicate=source.copy()
    base=source.get('cmems_base_name',source.name)
    variable=result['layer']['id'].rsplit('/',1)[-1]
    duplicate.name=base+' · '+variable+' · '+result['time'][:10]
    duplicate['cmems_base_name']=base
    group=next((c for c in scene.collection.children if c.get('cmems_environment_versions')),None)
    created_group=group is None
    if group is None:
        group=bpy.data.collections.new('Environmental Layers')
        group['cmems_environment_versions']=True
        scene.collection.children.link(group)
    group.objects.link(duplicate)
    try:
        payload=dict(result,update_policy='COPY',source_object=source.name)
        apply_layer(duplicate,scene,points,payload,props)
    except Exception:
        mesh=duplicate.data
        bpy.data.objects.remove(duplicate,do_unlink=True)
        if mesh.users==0:bpy.data.meshes.remove(mesh)
        if created_group and not group.objects:bpy.data.collections.remove(group)
        raise
    duplicate.hide_render=False
    layer=bpy.context.view_layer if bpy.context.scene==scene else None
    if layer:
        layer.update()
        if duplicate.name in layer.objects:
            duplicate.hide_set(False,view_layer=layer)
            for obj in list(bpy.context.selected_objects):obj.select_set(False)
            duplicate.select_set(True);layer.objects.active=duplicate
        if hide_previous and source.name in layer.objects:source.hide_set(True,view_layer=layer)
    if hide_previous:source.hide_render=True
    props.target=duplicate
    props.status='Created copy: '+duplicate.name
    return duplicate


def python_executable():
    base = Path(bpy.utils.system_resource('PYTHON'))
    names = ('python.exe',) if sys.platform == 'win32' else ('python3.' + str(sys.version_info.minor), 'python3')
    for name in names:
        for folder in (base / 'bin', base):
            if (folder / name).is_file():
                return str(folder / name)
    path = shutil.which('python3')
    if path:
        return path
    raise ValueError('Could not find Blender\'s bundled Python executable.')


class CMEMSBackup(bpy.types.PropertyGroup):
    material: PointerProperty(type=bpy.types.Material)
    link: StringProperty(default='DATA')
    managed_material: PointerProperty(type=bpy.types.Material)
    created: BoolProperty(default=False)
    key: StringProperty(default='')


class CMEMSObjectState(bpy.types.PropertyGroup):
    applied: BoolProperty(default=False)
    backups: CollectionProperty(type=CMEMSBackup)
    metadata: StringProperty(default='')
    original_slot_count: IntProperty(default=-1)
    standalone: BoolProperty(default=False)
    basemap_uv: StringProperty(default='')
    style_initialized: BoolProperty(default=False)
    opacity: FloatProperty(name='Marine opacity',default=.65,min=0,max=1,update=live_style)
    saturation: FloatProperty(name='Satellite saturation',default=1,min=0,max=2,update=live_style)
    contrast: FloatProperty(name='Satellite contrast',default=0,min=-100,max=100,update=live_style)
    blend: EnumProperty(name='Blend',items=[('MIX','Mix',''),('ADD','Add',''),('MULTIPLY','Multiply',''),('SCREEN','Screen','')],default='MIX',update=live_style)
    interpolation: EnumProperty(name='Image filtering',items=[('Closest','Nearest cells',''),('Linear','Linear',''),('Cubic','Cubic','')],default='Linear',update=live_style)


class CMEMSPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    cache_folder: StringProperty(name='Cache folder', subtype='DIR_PATH', default='',
                                 description='Leave empty to use Blender user data files')
    timeout: IntProperty(name='Request timeout (seconds)',default=35,min=5,max=120)
    parallel_downloads: IntProperty(name='Parallel downloads',default=4,min=1,max=8)
    catalogue_hours: IntProperty(name='Catalogue cache (hours)',default=1,min=0,max=168,
                                description='0 always checks for updated catalogue metadata')
    default_detail: EnumProperty(name='Default texture size',items=[('512','512 px',''),('1024','1024 px',''),('2048','2048 px','')],default='1024')
    default_filter: EnumProperty(name='Default image filtering',items=[('Closest','Nearest cells',''),('Linear','Linear',''),('Cubic','Cubic','')],default='Linear')

    def draw(self, context):
        layout = self.layout
        access=layout.box();access.label(text='Copernicus Marine · Data access',icon='WORLD')
        access.label(text='Current mode: public WMTS map imagery')
        access.label(text='No API key or login required for this mode.',icon='CHECKMARK')
        wrapped(access,'Raw-data access via the Marine Toolbox uses a Copernicus Marine account. This add-on currently loads map images.',70)
        access.operator('cmems.download',text='Test public connection',icon='URL').action='connection'
        if context.scene:
            wrapped(access,context.scene.cmems.status,70)
        download=layout.box();download.label(text='Downloads & cache',icon='FILE_FOLDER')
        download.prop(self,'cache_folder')
        download.operator('cmems.open_cache',text='Open cache folder',icon='FILE_FOLDER')
        row=download.row();row.prop(self,'timeout');row.prop(self,'parallel_downloads')
        download.prop(self,'catalogue_hours')
        defaults=layout.box();defaults.label(text='Map defaults',icon='PREFERENCES')
        defaults.prop(self,'default_detail');defaults.prop(self,'default_filter')
        defaults.operator('cmems.defaults',text='Apply defaults to this scene')
        system=layout.box();system.label(text='System',icon='INFO')
        system.label(text='Blender '+bpy.app.version_string+' · Marine Layers 0.4.0')
        try:
            module,_=gis_modules()
            parent=sys.modules.get(module.__package__)
            version=getattr(parent,'bl_info',{}).get('version',())
            system.label(text='BlenderGIS detected'+(' · '+'.'.join(map(str,version)) if version else ''))
        except ValueError:
            system.label(text='Enable BlenderGIS to use a georeferenced terrain.',icon='ERROR')
        system.label(text='Map controls: GIS → Web geodata → Environmental Layer')
        system.label(text='Render engine and samples: scene settings in the map panel.')


class CMEMSSettings(bpy.types.PropertyGroup):
    placement: EnumProperty(name='Destination',items=[('EXISTING','Existing BlenderGIS terrain','Apply to its material; keep its mesh and elevation'),
                            ('NEW','New environmental surface','Create a georeferenced flat surface with colour and transparent NoData')],default='EXISTING')
    area_mode: EnumProperty(name='Area',items=[('MAP','Selected BlenderGIS map','Use the selected mesh or georeferenced basemap image extent'),
                            ('CUSTOM','Custom geographic bounds','Enter WGS84 longitude and latitude')],default='CUSTOM')
    west: FloatProperty(name='West',default=8.72,min=-179.9,max=179.9,precision=5)
    east: FloatProperty(name='East',default=9.02,min=-179.9,max=179.9,precision=5)
    south: FloatProperty(name='South',default=56.70,min=-85,max=85,precision=5)
    north: FloatProperty(name='North',default=56.90,min=-85,max=85,precision=5)
    surface_z: FloatProperty(name='Surface height (scene units)',default=0)
    target: PointerProperty(name='Target terrain',type=bpy.types.Object,poll=terrain_poll,
                            description='Existing BlenderGIS map mesh; the environmental layer changes its material, without adding a second map object')
    use_basemap: BoolProperty(name='Use BlenderGIS basemap when material is missing',default=True,
                              description='Map the existing Google/ESRI/OSM image onto a bare terrain before blending the environmental field')
    update_policy: EnumProperty(name='When loading',items=[('COPY','Create new copy','Keep the source geometry and materials; create a private environmental version'),
                                  ('REPLACE','Replace target layer','Update only the selected target')],default='COPY')
    hide_previous: BoolProperty(name='Hide source after creating copy',default=True,
                                description='Keep it in the Outliner, hidden in the current view and render to avoid coincident surfaces')
    product: EnumProperty(name='Region / data', items=[(k,v[0],v[1]) for k,v in core.PRODUCTS.items()]
                         + [('CUSTOM','Other Marine product','Enter a product ID')], default='NORTH_COAST_MONTHLY', update=change_product)
    custom_product: StringProperty(name='Product ID', default='')
    catalogue_json: StringProperty(default='[]')
    layer: EnumProperty(name='Variable / dataset', items=layer_items, update=select_layer)
    date: StringProperty(name='Date / UTC time', default='', description='Exact ISO date or UTC time; copied from layer metadata')
    date_pick: EnumProperty(name='Available period',items=time_items,update=select_time)
    time_mode: EnumProperty(name='Time selection',items=[('PICK','Available periods','Most recent 512 times; use manual entry for older dates'),('MANUAL','Manual UTC time','Enter an exact time within advertised availability')],default='PICK')
    depth: FloatProperty(name='Depth (m)', default=0, min=0, description='Positive depth; nearest available model depth is used and recorded')
    palette: EnumProperty(name='Marine palette', items=[('NEON','Neon green','Artistic ramp from a grey Marine map'),
        ('CYAN','Cyan','Artistic ramp from a grey Marine map'),('algae','Algae',''),('thermal','Thermal',''),
        ('viridis','Viridis',''),('gray','Greyscale','')], default='NEON')
    minimum: FloatProperty(name='Minimum',default=0)
    maximum: FloatProperty(name='Maximum',default=5)
    detail: EnumProperty(name='Texture size target', items=[('512','512 px',''),('1024','1024 px',''),('2048','2048 px','')],default='1024')
    opacity: FloatProperty(name='Marine opacity', default=0.65,min=0,max=1,update=live_style)
    saturation: FloatProperty(name='Satellite saturation',default=1,min=0,max=2,update=live_style)
    contrast: FloatProperty(name='Satellite contrast',default=0,min=-100,max=100,update=live_style)
    interpolation: EnumProperty(name='Image filtering',items=[('Closest','Nearest cells','Preserve visible source cells'),('Linear','Linear','Smooth displayed image'),('Cubic','Cubic','Smooth displayed image')],default='Linear',update=live_style)
    blend: EnumProperty(name='Blend',items=[('MIX','Mix',''),('ADD','Add',''),('MULTIPLY','Multiply',''),('SCREEN','Screen','')],default='MIX',update=live_style)
    status: StringProperty(default='Select a terrain mesh, then load available layers.')
    loaded_label: StringProperty(default='')
    loaded_units: StringProperty(default='')
    busy: BoolProperty(default=False,options={'SKIP_SAVE'})
    show_details: BoolProperty(name='Source details',default=False)
    show_render: BoolProperty(name='Render & system',default=False)


class MarineRequest:
    """Network job owned by an app timer, never a retained Blender Operator."""
    def __init__(self,context,action,force_refresh=False):
        self.action=action;self.force_refresh=force_refresh
        self._scene=context.scene;self._obj=None;self._process=None;self._log=None
        self._callback=self.tick;self._inside_timer=False
        self.create_new=False
        self.copy_target=False;self.hide_previous=True

    def start(self,context):
        global _JOB
        p=self._scene.cmems
        self._job_dir=Path(tempfile.mkdtemp(prefix='blender_marine_'))
        prefs=preferences(context)
        job=dict(action=self.action,cache_dir=str(cache_folder(context)),
                 result_path=str(self._job_dir/'result.json'),progress_path=str(self._job_dir/'progress.json'),
                 timeout=prefs.timeout,parallel_downloads=prefs.parallel_downloads,
                 catalogue_ttl_seconds=prefs.catalogue_hours*3600,
                 force_refresh=self.force_refresh or self.action=='connection')
        if self.action in ('catalogue','connection'):
            self._product=p.custom_product.strip() if p.product=='CUSTOM' else core.PRODUCTS[p.product][1]
            job['product']=self._product
        else:
            layer=current_layer(p)
            if layer is None:raise ValueError('Load available layers and choose a variable first.')
            self.create_new=p.placement=='NEW'
            self.copy_target=not self.create_new and p.update_policy=='COPY'
            self.hide_previous=p.hide_previous
            if self.create_new:
                bounds=standalone_bounds(context);self._bounds=bounds
                self._geo_signature=geo_signature(self._scene)
            else:
                self._obj=active_terrain(context)
                self._points=projected_vertices(self._obj,self._scene)
                xs,ys=zip(*self._points);bounds=[min(xs),min(ys),max(xs),max(ys)]
                self._signature=self.signature()
            core.tile_plan(bounds,int(p.detail));core.resolve_time(layer,p.date);core.resolve_depth(layer,p.depth)
            core.validate_coverage(layer,bounds)
            job.update(layer=layer,date=p.date,depth=p.depth,palette=p.palette,
                       range=[p.minimum,p.maximum],bounds=bounds,detail=int(p.detail))
        (self._job_dir/'job.json').write_text(json.dumps(job))
        self._log=open(self._job_dir/'worker.log','w')
        self._process=subprocess.Popen([python_executable(),str(Path(core.__file__)),str(self._job_dir/'job.json')],
                                       stdout=self._log,stderr=subprocess.STDOUT)
        _JOB=self;p.busy=True
        p.status='Checking public connection…' if self.action=='connection' else 'Loading available layers…' if self.action=='catalogue' else 'Loading environmental data…'
        bpy.app.timers.register(self._callback,first_interval=.2)

    def signature(self):
        obj=self._obj
        return (geo_signature(self._scene),tuple(v for row in obj.matrix_world for v in row),
                obj.data.as_pointer(),len(obj.data.vertices),len(obj.data.loops))

    def finish(self,context=None):
        global _JOB
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:self._process.kill()
        if self._log:self._log.close();self._log=None
        if not self._inside_timer and bpy.app.timers.is_registered(self._callback):
            bpy.app.timers.unregister(self._callback)
        try:self._scene.cmems.busy=False
        except (ReferenceError,AttributeError):pass
        if _JOB is self:_JOB=None

    def tick(self):
        global _READY
        self._inside_timer=True
        try:
            p=self._scene.cmems
            progress=self._job_dir/'progress.json'
            if progress.is_file():
                try:p.status=json.loads(progress.read_text())['message']
                except (ValueError,OSError):pass
            if self._process.poll() is None:return .25
            result_path=self._job_dir/'result.json'
            if not result_path.is_file():raise RuntimeError('Download worker stopped. See '+str(self._job_dir/'worker.log'))
            result=json.loads(result_path.read_text())
            if not result['ok']:raise RuntimeError(result['error'])
            data=result['data']
            if self.action=='connection':
                p.status='Public WMTS connected · '+data['checked_at_utc'][:19]+' UTC · no login'
            elif self.action=='catalogue':
                product=p.custom_product.strip() if p.product=='CUSTOM' else core.PRODUCTS[p.product][1]
                if product!=self._product:raise ValueError('Product changed. Load its layers again.')
                p.catalogue_json=json.dumps(data['layers'])
                chlorophyll=[l for l in data['layers'] if l['id'].lower().endswith('/chl')]
                period='P1M' if p.product=='NORTH_COAST_MONTHLY' else 'P1D'
                chosen=next((l for l in chlorophyll if period in l['id']),data['layers'][0])
                p.layer=chosen['id'];select_layer(p,bpy.context)
                p.status=str(len(data['layers']))+' layers available. Choose a period and load.'
            else:
                if self.create_new:
                    if geo_signature(self._scene)!=self._geo_signature:
                        raise ValueError('Scene georeference changed during download. Load again.')
                else:
                    if self.signature()!=self._signature or self._obj.mode!='OBJECT':
                        raise ValueError('Target terrain or geographic location changed. Load again.')
                    points=projected_vertices(self._obj,self._scene)
                    if points!=self._points:raise ValueError('Terrain changed during download. Load again.')
                self.result=data;_READY=self
                window=next((w for w in bpy.context.window_manager.windows if w.scene==self._scene and any(a.type=='VIEW_3D' for a in w.screen.areas)),None)
                if window:
                    area=next(a for a in window.screen.areas if a.type=='VIEW_3D')
                    region=next(r for r in area.regions if r.type=='WINDOW')
                    with bpy.context.temp_override(window=window,area=area,region=region):
                        outcome=bpy.ops.cmems.apply_download('EXEC_DEFAULT')
                else:
                    with bpy.context.temp_override(scene=self._scene):outcome=bpy.ops.cmems.apply_download('EXEC_DEFAULT')
                if outcome!={'FINISHED'}:raise ValueError(p.status)
            self.finish();return None
        except Exception as exc:
            try:self._scene.cmems.status=str(exc)
            except ReferenceError:pass
            print('Marine Layers:',exc)
            self.finish();return None
        finally:
            _READY=None;self._inside_timer=False
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type in ('VIEW_3D','PREFERENCES'):area.tag_redraw()


class CMEMS_OT_download(bpy.types.Operator):
    bl_idname='cmems.download'
    bl_label='Load environmental data'
    # Request returns immediately; it never enters Blender's modal/redo lifetime.
    bl_options={'INTERNAL'}
    action: StringProperty(default='tiles')
    force_refresh: BoolProperty(default=False,options={'HIDDEN'})
    def execute(self,context):
        if _JOB is not None:
            self.report({'WARNING'},'An environmental request is already running.')
            return {'CANCELLED'}
        request=MarineRequest(context,self.action,self.force_refresh)
        try:
            request.start(context)
            return {'FINISHED'}
        except Exception as exc:
            request.finish();context.scene.cmems.status=str(exc)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


class CMEMS_OT_apply_download(bpy.types.Operator):
    bl_idname='cmems.apply_download'
    bl_label='Apply environmental layer to terrain'
    bl_options={'UNDO','INTERNAL'}
    def execute(self,context):
        if _READY is None:return {'CANCELLED'}
        request=_READY
        try:
            if request.create_new:
                request._obj=create_standalone_surface(request._scene,request._bounds,request.result,request._scene.cmems)
            elif request.copy_target:
                request._obj=copy_environment_target(request._obj,request._scene,request._points,
                            request.result,request._scene.cmems,request.hide_previous)
            else:apply_layer(request._obj,request._scene,request._points,request.result,request._scene.cmems)
            return {'FINISHED'}
        except Exception as exc:
            request._scene.cmems.status=str(exc)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


class CMEMS_OT_cancel_request(bpy.types.Operator):
    bl_idname='cmems.cancel_request'
    bl_label='Cancel environmental download'
    def execute(self,context):
        if _JOB:
            scene=_JOB._scene;_JOB.finish();scene.cmems.status='Environmental request cancelled.'
        return {'FINISHED'}


class CMEMS_OT_remove(bpy.types.Operator):
    bl_idname = 'cmems.remove'
    bl_label = 'Remove Marine layer'
    bl_options = {'REGISTER','UNDO'}

    def execute(self, context):
        try:
            obj = active_terrain(context)
            state = obj.cmems_state
            if not state.applied:
                raise ValueError('The selected terrain has no Marine layer.')
            if obj.data.users>1:obj.data=obj.data.copy()
            records=material_records(obj)
            remove=[index for index,record in enumerate(records) if record['created']]
            survivors=[record for record in records if not record['created']]
            if not survivors:obj.data.materials.clear()
            else:
                for index in reversed(remove):obj.data.materials.pop(index=index)
            # Blender keeps object-level overrides while reindexing data slots.
            # Restore after reindexing so a removed override cannot mask a survivor.
            for slot,record in zip(obj.material_slots,survivors):
                slot.link=record['link'];slot.material=record['material']
            uv = obj.data.uv_layers.get(UV_NAME)
            if uv:
                obj.data.uv_layers.remove(uv)
            baseuv=obj.data.uv_layers.get(state.basemap_uv) if state.basemap_uv else None
            if baseuv:obj.data.uv_layers.remove(baseuv)
            state.basemap_uv=''
            state.backups.clear()
            state.metadata = ''
            state.applied = False
            state.original_slot_count=-1
            state.style_initialized=False
            context.scene.cmems.status = 'Original terrain materials restored.'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class CMEMS_OT_open_cache(bpy.types.Operator):
    bl_idname='cmems.open_cache'
    bl_label='Open Marine cache'
    def execute(self,context):
        bpy.ops.wm.path_open(filepath=str(cache_folder(context)))
        return {'FINISHED'}


class CMEMS_OT_defaults(bpy.types.Operator):
    bl_idname='cmems.defaults'
    bl_label='Apply Marine defaults'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        prefs=preferences(context)
        context.scene.cmems.detail=prefs.default_detail
        context.scene.cmems.interpolation=prefs.default_filter
        return {'FINISHED'}


class CMEMS_OT_step_time(bpy.types.Operator):
    bl_idname='cmems.step_time'
    bl_label='Previous / next available period'
    direction: IntProperty(default=1)
    def execute(self,context):
        p=context.scene.cmems
        layer=current_layer(p)
        if not layer:
            return {'CANCELLED'}
        values=core.available_times(layer)
        before=[v for v in values if core.iso(v)<core.iso(p.date)]
        after=[v for v in values if core.iso(v)>core.iso(p.date)]
        target=before[-1] if self.direction<0 and before else after[0] if self.direction>0 and after else None
        if not target:
            self.report({'INFO'},'No further time in this selector. Use manual UTC time for older dates.')
            return {'CANCELLED'}
        p.date_pick=target
        return {'FINISHED'}


class CMEMS_OT_preview(bpy.types.Operator):
    bl_idname='cmems.material_preview'
    bl_label='Show Material Preview'
    def execute(self,context):
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type=='VIEW_3D':
                    area.spaces.active.shading.type='MATERIAL'
        return {'FINISHED'}


class CMEMS_OT_export_web(bpy.types.Operator,ExportHelper):
    bl_idname='cmems.export_web'
    bl_label='Export web package · GLB + JSON'
    bl_description='Export the selected terrain with baked map colours, alpha and geographic metadata'
    filename_ext='.glb'
    filter_glob: StringProperty(default='*.glb',options={'HIDDEN'})
    resolution: EnumProperty(name='Texture size',items=[('1024','1024 px',''),('2048','2048 px',''),('4096','4096 px','')],default='2048')
    def execute(self,context):
        try:
            from .web_export import export_web_package
            obj=context.active_object
            if obj is None or obj.type!='MESH':obj=active_terrain(context)
            result=export_web_package(context,obj,self.filepath,int(self.resolution))
            self.report({'INFO'},'GLB with embedded map texture and matching JSON exported.')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class CMEMS_OT_export_metadata(bpy.types.Operator,ExportHelper):
    bl_idname='cmems.export_metadata'
    bl_label='Export layer metadata'
    filename_ext='.json'
    filter_glob: StringProperty(default='*.json',options={'HIDDEN'})
    def execute(self,context):
        try:
            obj=active_terrain(context)
            if not obj.cmems_state.applied:
                raise ValueError('Select a terrain with a loaded Marine layer.')
            metadata=json.loads(obj.cmems_state.metadata)
            metadata['appearance']={key:getattr(obj.cmems_state,key) for key in
                                    ('opacity','blend','saturation','contrast','interpolation')}
            metadata['render_engine']=context.scene.render.engine
            Path(self.filepath).write_text(json.dumps(metadata,indent=2),encoding='utf8')
            self.report({'INFO'},'Layer source and time exported.')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class CMEMS_OT_environment(bpy.types.Operator):
    bl_idname='cmems.environment'
    bl_label='Environmental Layer · Copernicus Marine'
    bl_description='Add environmental data to the same georeferenced BlenderGIS terrain'
    destination: StringProperty(default='',options={'HIDDEN'})
    def invoke(self,context,event):
        if self.destination in ('EXISTING','NEW'):context.scene.cmems.placement=self.destination
        try:active_terrain(context)
        except ValueError:pass
        return context.window_manager.invoke_popup(self,width=520)
    def execute(self,context):return {'FINISHED'}
    def draw(self,context):CMEMS_PT_layers.draw(self,context)


class CMEMS_MT_environment(bpy.types.Menu):
    bl_label='Environmental Layer'
    bl_idname='CMEMS_MT_environment'
    def draw(self,context):
        layout=self.layout
        layout.operator('cmems.environment',text='On existing BlenderGIS terrain…',icon='MESH_DATA').destination='EXISTING'
        layout.operator('cmems.environment',text='Create new environmental surface…',icon='WORLD').destination='NEW'
        layout.operator('cmems.download',text='Load available variables',icon='FILE_REFRESH').action='catalogue'
        row=layout.row();row.enabled=_JOB is None
        row.operator('cmems.download',text='Load current environmental request',icon='IMPORT').action='tiles'
        layout.separator()
        layout.operator('cmems.material_preview',icon='SHADING_RENDERED')
        layout.operator('cmems.export_metadata',icon='EXPORT')
        layout.operator('cmems.export_web',icon='EXPORT')
        layout.operator('cmems.remove',icon='X')


def draw_gis_environment(self,context):
    self.layout.separator()
    self.layout.menu('CMEMS_MT_environment',icon='WORLD')


def draw_web_environment(self,context):
    self.layout.operator('cmems.environment',text='Environmental Layer',icon='WORLD')


_MENU_HOOKS=[]

def attach_gis_menus():
    for name,callback in (('VIEW3D_MT_menu_gis',draw_gis_environment),
                          ('VIEW3D_MT_menu_gis_webgeodata',draw_web_environment)):
        menu=getattr(bpy.types,name,None)
        if menu and not any(item[0] is menu for item in _MENU_HOOKS):
            menu.append(callback);_MENU_HOOKS.append((menu,callback))
    return len(_MENU_HOOKS)


def maintain_gis_menus():
    attach_gis_menus()
    return 2.0


def initialise_loaded_data():
    restore_after_load(None)
    return None


@bpy.app.handlers.persistent
def stop_on_file_load(_):
    if _JOB:_JOB.finish()


@bpy.app.handlers.persistent
def restore_after_load(_):
    attach_gis_menus()
    if _JOB is None:
        for scene in bpy.data.scenes:
            scene.cmems.busy=False
            if scene.cmems.status.startswith('Loading'):
                scene.cmems.status='Previous request ended. Choose a layer and load again.'
    for obj in bpy.data.objects:
        if obj.type!='MESH' or not obj.cmems_state.applied or obj.cmems_state.style_initialized:continue
        mat=obj.material_slots[0].material if obj.material_slots else None
        if not mat or not mat.use_nodes:continue
        nodes=mat.node_tree.nodes
        state=obj.cmems_state
        for key,node,socket in [('opacity','CMEMS_0_Opacity',1),('saturation','CMEMS_0_Satellite','Saturation'),('contrast','CMEMS_0_Contrast','Contrast')]:
            if nodes.get(node):setattr(state,key,nodes[node].inputs[socket].default_value)
        if nodes.get('CMEMS_0_Mix'):state.blend=nodes['CMEMS_0_Mix'].blend_type
        if nodes.get('CMEMS_0_Image'):state.interpolation=nodes['CMEMS_0_Image'].interpolation
        state.style_initialized=True


class CMEMS_PT_layers(bpy.types.Panel):
    bl_label = 'Marine Layers'
    bl_idname = 'CMEMS_PT_layers'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GIS'
    bl_options={'DEFAULT_CLOSED'}

    def draw(self, context):
        layout=self.layout
        p=context.scene.cmems
        obj=p.target or context.active_object
        valid=obj and obj.type=='MESH'
        top=layout.box()
        top.enabled=not p.busy
        top.label(text='Environmental Layer · 0.4.0',icon='WORLD')
        top.prop(p,'placement')
        if p.placement=='EXISTING':
            top.prop(p,'target')
            top.prop(p,'update_policy')
            if p.update_policy=='COPY':top.prop(p,'hide_previous')
            top.prop(p,'use_basemap')
            wrapped(top,'Uses this terrain and its existing elevation; a satellite material is optional.')
        else:
            top.prop(p,'area_mode')
            if p.area_mode=='MAP':
                top.prop(p,'target',text='Map mesh (or select basemap)')
            else:
                row=top.row(align=True);row.prop(p,'west');row.prop(p,'east')
                row=top.row(align=True);row.prop(p,'south');row.prop(p,'north')
            top.prop(p,'surface_z')
            wrapped(top,'Creates a flat surface, colour and transparent NoData. Real terrain relief requires elevation data.')
        top.label(text='Public map access · no API key')
        if p.placement=='EXISTING':wrapped(top,'Terrain: '+obj.name if valid else 'Choose Target terrain above.',icon='MESH_DATA' if valid else 'INFO')
        source=layout.box();source.enabled=not p.busy
        source.label(text='1 · Data source',icon='OUTLINER_OB_IMAGE')
        source.prop(p,'product')
        if p.product=='CUSTOM': source.prop(p,'custom_product')
        row=source.row(align=True)
        row.operator('cmems.download',text='Load available layers',icon='IMPORT').action='catalogue'
        refresh=row.operator('cmems.download',text='',icon='FILE_REFRESH');refresh.action='catalogue';refresh.force_refresh=True
        layer=current_layer(p) if read_layers(p) else None
        if layer:
            source.prop(p,'layer')
            timebox=layout.box();timebox.enabled=not p.busy
            timebox.label(text='2 · Time & depth',icon='TIME')
            info=core.temporal_info(layer)
            timebox.label(text=info['kind']+' · UTC')
            timebox.prop(p,'time_mode',text='Choose')
            if p.time_mode=='PICK':
                row=timebox.row(align=True)
                row.operator('cmems.step_time',text='',icon='TRIA_LEFT').direction=-1
                row.prop(p,'date_pick',text='')
                row.operator('cmems.step_time',text='',icon='TRIA_RIGHT').direction=1
            else:
                timebox.prop(p,'date')
            if info['start']:
                timebox.label(text='Available: '+info['start'][:10]+' → '+info['end'][:10])
            if 'elevation' in layer['dimensions']:
                timebox.prop(p,'depth')
                try: timebox.label(text='Nearest model depth: '+core.resolve_depth(layer,p.depth)+' m')
                except ValueError: pass
            else:
                timebox.label(text='Surface product · no depth selection')
            appearance=layout.box();appearance.enabled=not p.busy
            appearance.label(text='3 · Colour & texture',icon='COLOR')
            appearance.prop(p,'palette')
            row=appearance.row(align=True);row.prop(p,'minimum');row.prop(p,'maximum')
            appearance.label(text='Values: '+(layer['unit'] or 'see source'))
            appearance.prop(p,'detail')
            appearance.prop(p,'interpolation')
            wrapped(appearance,'Texture pixels control display quality; source resolution stays unchanged.')
            text=('Create environmental surface' if p.placement=='NEW' else
                  'Load as new copy' if p.update_policy=='COPY' else 'Replace target layer')
            appearance.operator('cmems.download',text=text,icon='IMPORT').action='tiles'
        if valid and obj.cmems_state.applied:
            box=layout.box();box.enabled=not p.busy
            box.label(text='Loaded layer · '+obj.name,icon='CHECKMARK')
            box.prop(obj.cmems_state,'opacity');box.prop(obj.cmems_state,'blend')
            box.prop(obj.cmems_state,'saturation');box.prop(obj.cmems_state,'contrast')
            box.prop(obj.cmems_state,'interpolation')
            box.operator('cmems.material_preview',icon='SHADING_RENDERED')
            try:
                meta=json.loads(obj.cmems_state.metadata)
                loaded=meta['layer']
                wrapped(box,loaded['title'])
                wrapped(box,meta.get('period_label') or core.time_label(loaded,meta['time']))
                if core.temporal_info(loaded)['cadence']=='Monthly':
                    wrapped(box,'The timestamp labels a monthly period, not an instantaneous observation.')
                box.label(text='Source time: '+meta['time'][:19]+' UTC')
                box.label(text='Depth: '+meta['elevation']+' m' if meta['elevation'] else 'Surface product')
                box.label(text='Range: %g–%g %s' % (*meta['range'],loaded['unit']))
                box.prop(p,'show_details',icon='TRIA_DOWN' if p.show_details else 'TRIA_RIGHT',emboss=False)
                if p.show_details:
                    wrapped(box,'Product: '+loaded['id'].split('/')[0])
                    wrapped(box,'Dataset: '+loaded['id'].split('/')[1])
                    box.label(text='Variable: '+loaded['id'].split('/')[-1])
                    if 'BGC_HR_' in loaded['id']:
                        box.label(text='Coastal product grid: 100 m')
                    box.label(text='Map CRS: '+meta.get('map_crs','EPSG:3857'))
                    box.label(text='Scene CRS: '+str(meta.get('scene_crs','see BlenderGIS')))
                    wrapped(box,'Loaded at: '+meta.get('loaded_at_utc','not recorded by older version'))
                    if loaded.get('data_updated_utc'):
                        wrapped(box,'Provider metadata update: '+loaded['data_updated_utc'])
                    box.label(text='Image packed in blend file')
                    box.operator('cmems.export_metadata',icon='EXPORT')
                    box.operator('cmems.export_web',icon='EXPORT')
            except (ValueError,KeyError):
                wrapped(box,'Reload this layer to record full source metadata.')
            box.operator('cmems.remove',icon='X')
        system=layout.box()
        system.prop(p,'show_render',icon='TRIA_DOWN' if p.show_render else 'TRIA_RIGHT',emboss=False)
        if p.show_render:
            scene=context.scene
            system.prop(scene.render,'engine',text='Engine')
            if scene.render.engine=='CYCLES':
                system.prop(scene.cycles,'samples',text='Render samples')
                system.prop(scene.cycles,'preview_samples',text='Viewport samples')
                system.prop(scene.cycles,'device',text='Device')
            system.label(text='Scene CRS: '+str(scene.get('SRID','not set')))
            system.label(text='Blender '+bpy.app.version_string+' · Add-on 0.4.0')
            wrapped(system,'Connection and cache settings are in Preferences → Add-ons → BlenderGIS Marine Layers.')
        for line in textwrap.wrap(p.status,width=43)[:5]: layout.label(text=line)
        if p.busy: layout.operator('cmems.cancel_request',text='Cancel request',icon='CANCEL')


CLASSES = (CMEMSBackup,CMEMSObjectState,CMEMSPreferences,CMEMSSettings,CMEMS_OT_download,CMEMS_OT_apply_download,CMEMS_OT_cancel_request,CMEMS_OT_remove,
           CMEMS_OT_open_cache,CMEMS_OT_defaults,CMEMS_OT_step_time,CMEMS_OT_preview,
           CMEMS_OT_export_metadata,CMEMS_OT_export_web,CMEMS_OT_environment,CMEMS_MT_environment,CMEMS_PT_layers)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cmems = PointerProperty(type=CMEMSSettings)
    bpy.types.Object.cmems_state = PointerProperty(type=CMEMSObjectState)
    attach_gis_menus()
    if not bpy.app.timers.is_registered(maintain_gis_menus):
        bpy.app.timers.register(maintain_gis_menus,first_interval=1,persistent=True)
    bpy.app.handlers.load_pre.append(stop_on_file_load)
    bpy.app.handlers.load_post.append(restore_after_load)
    bpy.app.timers.register(initialise_loaded_data,first_interval=.1)


def unregister():
    global _JOB
    if _JOB:
        _JOB.finish(bpy.context)
    if bpy.app.timers.is_registered(maintain_gis_menus):bpy.app.timers.unregister(maintain_gis_menus)
    if bpy.app.timers.is_registered(initialise_loaded_data):bpy.app.timers.unregister(initialise_loaded_data)
    for menu,callback in _MENU_HOOKS:
        try:menu.remove(callback)
        except (ValueError,ReferenceError,RuntimeError):pass
    _MENU_HOOKS.clear()
    if stop_on_file_load in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(stop_on_file_load)
    if restore_after_load in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(restore_after_load)
    del bpy.types.Object.cmems_state
    del bpy.types.Scene.cmems
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _ENUM_CACHE.clear()
    _TIME_CACHE.clear()
