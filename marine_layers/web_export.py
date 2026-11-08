# SPDX-License-Identifier: GPL-3.0-or-later
"""Export a selected terrain as a self-contained GLB plus its GIS sidecar.

Bake the existing Base Color and Alpha, not a fresh web rendering of the data.
All baking happens on an evaluated temporary copy. Source objects and materials
are never replaced, and selection/render settings are restored in finally.
"""
import json
import struct
import uuid
from pathlib import Path

import bpy


def _has_requested_overlay(material, metadata):
    if not material or not material.use_nodes:
        return False
    expected = metadata.get('layer', {}).get('id')
    for node in material.node_tree.nodes:
        if node.type != 'TEX_IMAGE' or not node.name.startswith('CMEMS_') or not node.image:
            continue
        try:
            record = json.loads(node.image.get('cmems_metadata', '{}'))
        except (TypeError, ValueError):
            continue
        if record.get('layer', {}).get('id') == expected and record.get('time') == metadata.get('time'):
            return True
    return False


def _export_materials(source, metadata):
    """Recover only exact managed-material pointers, on the export copy only."""
    current = list(source.data.materials)
    backups = list(source.cmems_state.backups)
    expected = bool(metadata.get('layer', {}).get('id'))
    recovered = []
    result = []
    for index in range(max(len(current), 1)):
        mat = current[index] if index < len(current) else None
        if expected and not _has_requested_overlay(mat, metadata):
            saved = backups[index].managed_material if index < len(backups) else None
            if not _has_requested_overlay(saved, metadata):
                raise ValueError('This terrain currently has only its base material, while its JSON describes a Marine layer. Reload the intended layer on this terrain before exporting.')
            mat = saved
            recovered.append(index)
        if mat is None:
            raise ValueError('The selected terrain has an empty material slot. Assign its map material before exporting.')
        result.append(mat)
    return result, recovered


def export_web_package(context, source, filepath, resolution=2048):
    if source.type != 'MESH' or not source.data.polygons:
        raise ValueError('Choose the actual terrain mesh, not a basemap image empty.')
    if source.mode != 'OBJECT':
        raise ValueError('Switch the terrain to Object Mode before exporting.')
    metadata = json.loads(source.cmems_state.metadata or '{}')
    export_materials, recovered = _export_materials(source, metadata)
    if not metadata.get('scene_origin'):
        metadata['scene_origin'] = [context.scene.get('crs x', 0), context.scene.get('crs y', 0)]
        metadata['scene_crs'] = context.scene.get('SRID', 'EPSG:3857')
        metadata['scene_scale'] = context.scene.get('scale', 1)
    metadata['appearance'] = {key: getattr(source.cmems_state, key) for key in
                              ('opacity', 'blend', 'saturation', 'contrast', 'interpolation')}
    path = Path(filepath).with_suffix('.glb')
    path.parent.mkdir(parents=True, exist_ok=True)
    workpath = path.with_name(path.stem + '.' + uuid.uuid4().hex[:8] + '.glb')
    scene = context.scene
    selected, active = list(context.selected_objects), context.view_layer.objects.active
    settings = (scene.render.engine, scene.cycles.device, scene.cycles.samples,
                scene.render.bake.margin, scene.render.bake.use_clear)
    temp = None
    mesh = None
    materials, images = [], []
    try:
        deps = context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(source.evaluated_get(deps), depsgraph=deps)
        temp = bpy.data.objects.new(source.name + ' · Web export', mesh)
        scene.collection.objects.link(temp)
        temp.matrix_world = source.matrix_world.copy()
        for obj in context.selected_objects:
            obj.select_set(False)
        temp.select_set(True)
        context.view_layer.objects.active = temp
        original_uv = mesh.uv_layers.active.name if mesh.uv_layers.active else None
        copied = []
        for original in export_materials:
            mat = original.copy()
            materials.append(mat)
            mat.use_nodes = True
            if source.cmems_state.applied:
                from . import update_material
                update_material(mat, source.cmems_state)
            nodes, links = mat.node_tree.nodes, mat.node_tree.links
            output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
            shader = output.inputs['Surface'].links[0].from_node if output and output.inputs['Surface'].is_linked else None
            if shader is None or shader.type not in ('BSDF_PRINCIPLED', 'BSDF_DIFFUSE', 'EMISSION'):
                raise ValueError('Web export currently supports a Principled, Diffuse or Emission surface. Simplify this material first.')
            # Freeze implicit source UVs before making a separate bake UV active.
            if original_uv:
                for n in list(nodes):
                    if n.type == 'TEX_IMAGE' and not n.inputs['Vector'].is_linked:
                        uv = nodes.new('ShaderNodeUVMap')
                        uv.uv_map = original_uv
                        links.new(uv.outputs['UV'], n.inputs['Vector'])
            colour = shader.inputs['Base Color' if shader.type == 'BSDF_PRINCIPLED' else 'Color']
            alpha = shader.inputs.get('Alpha')
            emission = nodes.new('ShaderNodeEmission')
            links.new(emission.outputs[0], output.inputs['Surface'])
            target = nodes.new('ShaderNodeTexImage')
            target.name = 'WEB_BAKE_TARGET'
            nodes.active = target
            copied.append((mat, colour, alpha, emission, target))
        mesh.materials.clear()
        for mat, *_ in copied:
            mesh.materials.append(mat)
        uv = mesh.uv_layers.new(name='Web_Export_UV')
        points = [temp.matrix_world @ v.co for v in mesh.vertices]
        x0, x1 = min(p.x for p in points), max(p.x for p in points)
        y0, y1 = min(p.y for p in points), max(p.y for p in points)
        if x1 - x0 < .001 or y1 - y0 < .001:
            raise ValueError('The selected map needs a non-zero XY extent.')
        for loop in mesh.loops:
            p = points[loop.vertex_index]
            uv.data[loop.index].uv = ((p.x-x0)/(x1-x0), (p.y-y0)/(y1-y0))
        mesh.uv_layers.active = uv
        uv.active_render = True
        width = max(256, int(resolution))
        height = max(256, round(width * (y1-y0)/(x1-x0)))
        if height > resolution:
            width = max(256, round(width * resolution/height))
            height = resolution
        baked = bpy.data.images.new('Web · baked colour', width, height, alpha=True)
        alpha_image = bpy.data.images.new('Web · baked alpha', width, height, alpha=True)
        images.extend([baked, alpha_image])
        baked.colorspace_settings.name = 'sRGB'
        alpha_image.colorspace_settings.name = 'Non-Color'
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 1
        scene.render.bake.margin = 2
        scene.render.bake.use_clear = True
        for mat, colour, alpha, emission, target in copied:
            target.image = baked
            if colour.is_linked:
                mat.node_tree.links.new(colour.links[0].from_socket, emission.inputs['Color'])
            else:
                emission.inputs['Color'].default_value = colour.default_value
        bpy.ops.object.bake(type='EMIT')
        for mat, colour, alpha, emission, target in copied:
            target.image = alpha_image
            for link in list(emission.inputs['Color'].links):
                mat.node_tree.links.remove(link)
            if alpha is not None and alpha.is_linked:
                mat.node_tree.links.new(alpha.links[0].from_socket, emission.inputs['Color'])
            else:
                value = alpha.default_value if alpha is not None else 1
                emission.inputs['Color'].default_value = (value, value, value, 1)
        bpy.ops.object.bake(type='EMIT')
        import numpy as np
        rgba = np.empty(width*height*4, dtype=np.float32)
        alphas = np.empty_like(rgba)
        baked.pixels.foreach_get(rgba)
        alpha_image.pixels.foreach_get(alphas)
        rgba[3::4] = np.clip(alphas[0::4], 0, 1)
        baked.pixels.foreach_set(rgba)
        baked.update()
        baked.pack()
        outputmat = bpy.data.materials.new(source.name + ' · Web colour')
        materials.append(outputmat)
        outputmat.use_nodes = True
        nodes, links = outputmat.node_tree.nodes, outputmat.node_tree.links
        bsdf = nodes.get('Principled BSDF')
        bsdf.inputs['Roughness'].default_value = 1
        tex = nodes.new('ShaderNodeTexImage')
        tex.image = baked
        uvnode = nodes.new('ShaderNodeUVMap')
        uvnode.uv_map = uv.name
        links.new(uvnode.outputs['UV'], tex.inputs['Vector'])
        links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
        links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
        mesh.materials.clear()
        mesh.materials.append(outputmat)
        for poly in mesh.polygons:
            poly.material_index = 0
        # Use the baked colour as unlit appearance, independent of web lighting.
        outputmat['gltf_unlit'] = True
        temp['georef'] = {'scene_crs': metadata.get('scene_crs'),
                          'scene_origin': metadata.get('scene_origin'),
                          'scene_scale': metadata.get('scene_scale', 1)}
        result = bpy.ops.export_scene.gltf(filepath=str(workpath), export_format='GLB',
                    use_selection=True, export_apply=True, export_extras=True, export_yup=True)
        if 'FINISHED' not in result or not workpath.exists():
            raise RuntimeError('Blender did not finish the GLB export.')
        with workpath.open('rb') as handle:
            handle.read(12)
            length, chunk = struct.unpack('<II', handle.read(8))
            doc = json.loads(handle.read(length))
        if not doc.get('meshes') or not doc.get('images'):
            raise RuntimeError('Export verification failed: the GLB has no mesh or embedded texture.')
        workpath.replace(path)
        package = {'schema': 'blender-marine-web/1', 'name': source.name,
                   'asset': {'file': path.name, 'format': 'glb', 'bytes': path.stat().st_size,
                             'texture_size': [width, height], 'appearance': 'baked Base Color and Alpha',
                             'environmental_layer_embedded': bool(metadata.get('layer', {}).get('id')),
                             'recovered_managed_material_slots': recovered},
                   'metadata': metadata}
        sidecar = path.with_suffix('.json')
        sidecar.write_text(json.dumps(package, indent=2), encoding='utf8')
        return {'glb': str(path), 'json': str(sidecar), 'bytes': path.stat().st_size}
    finally:
        if temp is not None:
            bpy.data.objects.remove(temp, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        for mat in materials:
            if mat.users == 0:
                bpy.data.materials.remove(mat)
        for image in images:
            if image.users == 0:
                bpy.data.images.remove(image)
        scene.render.engine, scene.cycles.device, scene.cycles.samples, scene.render.bake.margin, scene.render.bake.use_clear = settings
        for obj in selected:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        context.view_layer.objects.active = active
        if workpath.exists():
            workpath.unlink()
