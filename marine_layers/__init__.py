# SPDX-License-Identifier: GPL-3.0-or-later
"""Copernicus Marine image layers for existing BlenderGIS terrain materials."""
bl_info = {
    'name': 'BlenderGIS Marine Layers', 'author': 'Codex', 'version': (0, 1, 0),
    'blender': (4, 2, 0), 'location': '3D View > Sidebar > GIS > Marine Layers',
    'description': 'Blend georeferenced Copernicus Marine maps into existing terrain',
    'category': '3D View',
}

import datetime as dt
import importlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       PointerProperty, StringProperty)
from mathutils import Vector
from . import core

UV_NAME = 'CMEMS_Marine_UV'
PREFIX = 'CMEMS_'
_ENUM_CACHE = {}
_JOB = None


def read_layers(p):
    try:
        return json.loads(p.catalogue_json)
    except (ValueError, TypeError):
        return []


def layer_items(self, context):
    key = self.catalogue_json
    if key not in _ENUM_CACHE:
        _ENUM_CACHE[key] = [(l['id'], l['title'] + (' · ' + l['period'] if l['period'] else ''),
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


def change_product(self, context):
    self.catalogue_json = '[]'
    self.status = 'Load available layers for this product.'


def gis_modules():
    for name, module in tuple(sys.modules.items()):
        if name.endswith('.geoscene') and hasattr(module, 'GeoScene'):
            return module, importlib.import_module(name.rsplit('.', 1)[0] + '.core.proj.reproj')
    raise ValueError('Enable BlenderGIS first, and export its basemap to a terrain mesh.')


def geo_signature(scene):
    return tuple(scene.get(k) for k in ('SRID', 'crs x', 'crs y', 'scale'))


def projected_vertices(obj, scene):
    geo_module, reproj = gis_modules()
    geo = geo_module.GeoScene(scene)
    if not geo.isGeoref:
        raise ValueError('This scene needs its BlenderGIS projection and map origin.')
    world = [obj.matrix_world @ v.co for v in obj.data.vertices]
    points = [geo.view3dToProj(v.x, v.y) for v in world]
    srs = reproj.SRS(geo.crs)
    if srs.isWM:
        return points
    if srs.isWGS84:
        return [reproj.lonLatToWebMerc(*p) for p in points]
    if srs.isUTM:
        utm = reproj.UTM.init_from_epsg(srs)
        return [reproj.lonLatToWebMerc(*utm.utm_to_lonlat(*p)) for p in points]
    raise ValueError('First version supports Web Mercator, WGS84 and UTM scenes. Use one of these in BlenderGIS.')


def active_terrain(context):
    obj = context.active_object
    if obj is None or obj.type != 'MESH' or obj.mode != 'OBJECT':
        raise ValueError('Select your exported terrain mesh in Object Mode.')
    if not obj.data.vertices or not obj.data.polygons:
        raise ValueError('Select a terrain mesh with faces.')
    if obj.library or obj.data.library:
        raise ValueError('Make the terrain local before applying a layer.')
    return obj


def shader_targets(mat):
    if mat is None or not mat.use_nodes:
        raise ValueError('The terrain needs a node material with its satellite texture.')
    targets = [n.inputs['Base Color'] for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED']
    if not targets:
        raise ValueError('This first version supports Principled BSDF terrain materials.')
    return targets


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
        links.new(ramp.outputs['Color'] if palette in ('NEON', 'CYAN') else tex.outputs['Color'], mix.inputs[2])
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


def live_style(self, context):
    obj = context.active_object if context else None
    if obj and obj.type == 'MESH' and obj.cmems_state.applied:
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
    if not np.any(pixels[:,:,3] > 0):
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


def apply_layer(obj, scene, points, result, props):
    # Validate every material before making a change.
    for slot in obj.material_slots:
        shader_targets(slot.material)
    if not obj.material_slots:
        raise ValueError('The terrain needs its existing satellite material.')
    state = obj.cmems_state
    if not state.applied and obj.data.uv_layers.get(UV_NAME):
        raise ValueError('Reserved Marine UV map already exists. Rename it before adding a layer.')
    if state.applied and len(state.backups) != len(obj.material_slots):
        raise ValueError('Material slots changed. Restore them before updating this layer.')
    image = make_mosaic(result, 'Marine · ' + result['layer']['title'])
    staged_materials = []
    staged_mesh = None
    try:
        for slot in obj.material_slots:
            material = slot.material.copy()
            staged_materials.append(material)
            if not state.applied:
                material.name = slot.material.name + ' · Marine'
            add_material_nodes(material, image, result['palette'], props)
        staged_mesh = obj.data.copy()
        old_render_uv = next((u.name for u in staged_mesh.uv_layers if u.active_render), None)
        old_active_uv = staged_mesh.uv_layers.active.name if staged_mesh.uv_layers.active else None
        uv = staged_mesh.uv_layers.get(UV_NAME) or staged_mesh.uv_layers.new(name=UV_NAME)
        if old_render_uv:
            staged_mesh.uv_layers[old_render_uv].active_render = True
        if old_active_uv:
            staged_mesh.uv_layers.active = staged_mesh.uv_layers[old_active_uv]
        values = [core.uv_for(point, result['plan']['bounds']) for point in points]
        uv.data.foreach_set('uv', [value for loop in staged_mesh.loops for value in values[loop.vertex_index]])
    except Exception:
        if staged_mesh:
            bpy.data.meshes.remove(staged_mesh)
        for material in staged_materials:
            bpy.data.materials.remove(material)
        bpy.data.images.remove(image)
        raise
    # Commit only once the new image, materials and UV mapping are all ready.
    if not state.applied:
        for slot in obj.material_slots:
            backup = state.backups.add()
            backup.material = slot.material
            backup.link = slot.link
    obj.data = staged_mesh
    for slot, material in zip(obj.material_slots, staged_materials):
        slot.link = 'OBJECT'
        slot.material = material
    state.metadata = json.dumps({k:v for k,v in result.items() if k != 'tiles'})
    state.applied = True
    depth = (' · depth ' + result['elevation'] + ' m') if result['elevation'] else ''
    props.status = 'Loaded: ' + result['time'] + depth
    props.loaded_label = result['layer']['title']
    props.loaded_units = result['layer']['unit']
    # Ensure the result can be seen in the viewport without altering lighting/render settings.
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D' and area.spaces.active.shading.type == 'SOLID':
                area.spaces.active.shading.color_type = 'TEXTURE'


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


class CMEMSObjectState(bpy.types.PropertyGroup):
    applied: BoolProperty(default=False)
    backups: CollectionProperty(type=CMEMSBackup)
    metadata: StringProperty(default='')


class CMEMSSettings(bpy.types.PropertyGroup):
    product: EnumProperty(name='Region / data', items=[(k,v[0],v[1]) for k,v in core.PRODUCTS.items()]
                         + [('CUSTOM','Other Marine product','Enter a product ID')], default='BALTIC_BIO', update=change_product)
    custom_product: StringProperty(name='Product ID', default='')
    catalogue_json: StringProperty(default='[]')
    layer: EnumProperty(name='Variable / dataset', items=layer_items, update=select_layer)
    date: StringProperty(name='Date / UTC time', default='', description='Exact ISO date or UTC time; copied from layer metadata')
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
    blend: EnumProperty(name='Blend',items=[('MIX','Mix',''),('ADD','Add',''),('MULTIPLY','Multiply',''),('SCREEN','Screen','')],default='MIX',update=live_style)
    status: StringProperty(default='Select a terrain mesh, then load available layers.')
    loaded_label: StringProperty(default='')
    loaded_units: StringProperty(default='')
    busy: BoolProperty(default=False,options={'SKIP_SAVE'})


class CMEMS_OT_download(bpy.types.Operator):
    bl_idname = 'cmems.download'
    bl_label = 'Load Marine layer'
    bl_options = {'REGISTER','UNDO'}
    action: StringProperty(default='tiles')
    _timer = None
    _process = None
    _log = None

    def execute(self, context):
        global _JOB
        p = context.scene.cmems
        if _JOB is not None:
            self.report({'WARNING'}, 'A Marine request is already running.')
            return {'CANCELLED'}
        try:
            self._scene = context.scene
            self._obj = None
            job_dir = Path(tempfile.mkdtemp(prefix='blender_marine_'))
            cache = Path(bpy.utils.user_resource('DATAFILES', path='marine_layers/cache', create=True))
            job = dict(action=self.action, cache_dir=str(cache),
                       result_path=str(job_dir/'result.json'), progress_path=str(job_dir/'progress.json'))
            if self.action == 'catalogue':
                self._product = p.custom_product.strip() if p.product == 'CUSTOM' else core.PRODUCTS[p.product][1]
                job['product'] = self._product
            else:
                self._obj = active_terrain(context)
                for slot in self._obj.material_slots:
                    shader_targets(slot.material)
                layer = current_layer(p)
                if layer is None:
                    raise ValueError('Load available layers and choose a variable first.')
                self._points = projected_vertices(self._obj, self._scene)
                xs, ys = zip(*self._points)
                bounds = [min(xs),min(ys),max(xs),max(ys)]
                core.tile_plan(bounds, int(p.detail))
                core.resolve_time(layer, p.date)
                core.resolve_depth(layer, p.depth)
                self._signature = (geo_signature(self._scene), tuple(v for row in self._obj.matrix_world for v in row),
                                   self._obj.data.as_pointer(), len(self._obj.data.vertices), len(self._obj.data.loops))
                job.update(layer=layer, date=p.date, depth=p.depth, palette=p.palette,
                           range=[p.minimum,p.maximum], bounds=bounds, detail=int(p.detail))
            self._job_dir = job_dir
            (job_dir/'job.json').write_text(json.dumps(job))
            self._log = open(job_dir/'worker.log','w')
            self._process = subprocess.Popen([python_executable(), str(Path(core.__file__)), str(job_dir/'job.json')],
                                              stdout=self._log, stderr=subprocess.STDOUT)
            _JOB = self
            p.busy = True
            p.status = 'Loading available layers…' if self.action == 'catalogue' else 'Loading Marine map…'
            self._timer = context.window_manager.event_timer_add(0.3, window=context.window)
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self.finish(context)
            p.status = str(exc)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}

    def finish(self, context):
        global _JOB
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._log:
            self._log.close()
            self._log = None
        try:
            self._scene.cmems.busy = False
        except (ReferenceError, AttributeError):
            pass
        if _JOB is self:
            _JOB = None

    def modal(self, context, event):
        if event.type == 'ESC':
            self.finish(context)
            self._scene.cmems.status = 'Marine request cancelled.'
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        p = self._scene.cmems
        progress = self._job_dir/'progress.json'
        if progress.is_file():
            try:
                p.status = json.loads(progress.read_text())['message']
            except (ValueError,OSError):
                pass
        if self._process.poll() is None:
            return {'PASS_THROUGH'}
        try:
            result_path = self._job_dir/'result.json'
            if not result_path.is_file():
                raise RuntimeError('Download worker stopped. See ' + str(self._job_dir/'worker.log'))
            result = json.loads(result_path.read_text())
            if not result['ok']:
                raise RuntimeError(result['error'])
            data = result['data']
            if self.action == 'catalogue':
                product = p.custom_product.strip() if p.product == 'CUSTOM' else core.PRODUCTS[p.product][1]
                if product != self._product:
                    raise ValueError('Product changed while loading. Load its layers again.')
                p.catalogue_json = json.dumps(data['layers'])
                chosen = next((l for l in data['layers'] if l['id'].endswith('/chl') and l['period'].lower() == 'daily'), data['layers'][0])
                p.layer = chosen['id']
                select_layer(p, context)
                p.status = str(len(data['layers'])) + ' layers available. Choose a variable and load.'
            else:
                obj = self._obj
                signature = (geo_signature(self._scene), tuple(v for row in obj.matrix_world for v in row),
                             obj.data.as_pointer(),len(obj.data.vertices),len(obj.data.loops))
                if signature != self._signature or obj.mode != 'OBJECT':
                    raise ValueError('Terrain or map location changed while loading. Load the layer again.')
                # Recompute positions after download, catching vertex edits with unchanged topology.
                points = projected_vertices(obj,self._scene)
                if points != self._points:
                    raise ValueError('Terrain was edited while loading. Load the layer again.')
                apply_layer(obj,self._scene,points,data,p)
            self.finish(context)
            self.report({'INFO'},p.status)
            return {'FINISHED'}
        except Exception as exc:
            self.finish(context)
            p.status = str(exc)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


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
            if len(state.backups) != len(obj.material_slots):
                raise ValueError('Material slots changed after adding the layer. Restore them before removing it.')
            for slot,backup in zip(obj.material_slots,state.backups):
                slot.material = backup.material
                slot.link = backup.link
            uv = obj.data.uv_layers.get(UV_NAME)
            if uv:
                obj.data.uv_layers.remove(uv)
            state.backups.clear()
            state.metadata = ''
            state.applied = False
            context.scene.cmems.status = 'Original terrain materials restored.'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class CMEMS_PT_layers(bpy.types.Panel):
    bl_label = 'Marine Layers'
    bl_idname = 'CMEMS_PT_layers'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GIS'

    def draw(self, context):
        layout = self.layout
        p = context.scene.cmems
        controls = layout.column()
        controls.enabled = not p.busy
        controls.prop(p,'product')
        if p.product == 'CUSTOM':
            controls.prop(p,'custom_product')
        op = controls.operator('cmems.download',text='Load available layers',icon='FILE_REFRESH'); op.action = 'catalogue'
        if read_layers(p):
            controls.prop(p,'layer')
            controls.prop(p,'date')
            controls.prop(p,'depth')
            controls.prop(p,'palette')
            row = controls.row(align=True); row.prop(p,'minimum'); row.prop(p,'maximum')
            layer = current_layer(p)
            if layer and layer['unit']:
                controls.label(text='Values in ' + layer['unit'])
            controls.prop(p,'detail')
            controls.operator('cmems.download',text='Load / update on selected terrain',icon='IMPORT').action = 'tiles'
        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.cmems_state.applied:
            box = layout.box()
            box.enabled = not p.busy
            box.label(text='Selected terrain: Marine layer')
            box.prop(p,'opacity')
            box.prop(p,'blend')
            box.prop(p,'saturation')
            box.prop(p,'contrast')
            try:
                meta = json.loads(obj.cmems_state.metadata)
                box.label(text=meta['layer']['title'][:48])
                box.label(text=meta['time'][:25])
                if meta['elevation']:
                    box.label(text='Actual depth: ' + meta['elevation'][:8] + ' m')
                box.label(text='Range: %g–%g %s' % (*meta['range'],meta['layer']['unit']))
            except (ValueError,KeyError):
                pass
            box.operator('cmems.remove',icon='X')
        for line in [p.status[i:i+46] for i in range(0,len(p.status),46)][:4]:
            layout.label(text=line)
        if p.busy:
            layout.label(text='Esc cancels this request.')
        layout.label(text='Marine maps retain their source resolution.',icon='INFO')


CLASSES = (CMEMSBackup,CMEMSObjectState,CMEMSSettings,CMEMS_OT_download,CMEMS_OT_remove,CMEMS_PT_layers)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cmems = PointerProperty(type=CMEMSSettings)
    bpy.types.Object.cmems_state = PointerProperty(type=CMEMSObjectState)


def unregister():
    global _JOB
    if _JOB:
        _JOB.finish(bpy.context)
    del bpy.types.Object.cmems_state
    del bpy.types.Scene.cmems
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _ENUM_CACHE.clear()
