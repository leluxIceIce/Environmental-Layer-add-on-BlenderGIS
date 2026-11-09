# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable world-space map marks using Blender curves and keyframes."""

import math

import bpy
from bpy.props import CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from mathutils import Vector

from .osm_world import metres_per_unit

WINDOW = 'cmems_annotations_window'
TAG = 'cmems_annotation'


class CMEMS_AnnotationPoint(bpy.types.PropertyGroup):
    position: FloatVectorProperty(size=3)


class CMEMS_AnnotationSettings(bpy.types.PropertyGroup):
    name: StringProperty(name='Name')
    radius: FloatProperty(name='Ring radius (m)', default=250, min=0.01, soft_max=10000)
    ring_style: EnumProperty(name='Ring style', items=(
        ('SOLID', 'Solid', 'Single continuous ring'),
        ('DASHED', 'Dashed', 'Segmented ring'),
        ('DOUBLE', 'Double', 'Two concentric rings'),
    ), default='SOLID')
    width: FloatProperty(name='Stroke width (m)', default=5, min=.001, soft_max=100)
    lift: FloatProperty(name='Lift (m)', default=1, description='Offset above the captured cursor height')
    color: FloatVectorProperty(name='Stroke color', size=3, subtype='COLOR', min=0, max=1,
                               default=(.66, .80, .72))
    opacity: FloatProperty(name='Stroke opacity', default=1, min=0, max=1, subtype='FACTOR')
    reveal_frames: IntProperty(name='Reveal duration (frames)', default=24, min=1, soft_max=240)
    points: CollectionProperty(type=CMEMS_AnnotationPoint)


def ring_paths(radius, style):
    def arc(r, start, end, steps):
        return [(r * math.cos(start + (end - start) * i / steps),
                 r * math.sin(start + (end - start) * i / steps), 0) for i in range(steps + 1)]

    if style == 'DASHED':
        return [arc(radius, math.tau * i / 12, math.tau * (i + .72) / 12, 8) for i in range(12)]
    return [arc(r, 0, math.tau, 96) for r in ((radius, radius * 1.06) if style == 'DOUBLE' else (radius,))]


def annotation_collection(scene):
    group = next((c for c in scene.collection.children if c.get('cmems_annotations')), None)
    if group is None:
        group = bpy.data.collections.new('Map Annotations')
        group['cmems_annotations'] = True
        scene.collection.children.link(group)
    return group


def stroke_material(name, props):
    material = bpy.data.materials.new(name + ' · stroke')
    material.diffuse_color = (*props.color, props.opacity)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    ink = tree.nodes.new('ShaderNodeEmission')
    ink.name = 'Annotation Color'
    ink.inputs[0].default_value = (*props.color, 1)
    trans = tree.nodes.new('ShaderNodeBsdfTransparent')
    mix = tree.nodes.new('ShaderNodeMixShader')
    mix.name = 'Annotation Opacity'
    mix.inputs[0].default_value = props.opacity
    out = tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(trans.outputs[0], mix.inputs[1])
    tree.links.new(ink.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], out.inputs[0])
    return material


def create_annotation(context, name, origin, paths):
    scene = context.scene
    props = scene.cmems_annotations
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions = '3D'
    curve.bevel_depth = props.width / (2 * metres_per_unit(scene))
    curve.bevel_resolution = 2
    curve.bevel_factor_mapping_end = 'SPLINE'
    for path in paths:
        spline = curve.splines.new('POLY')
        spline.points.add(len(path) - 1)
        spline.points.foreach_set('co', [value for point in path for value in (*point, 1)])
    obj = bpy.data.objects.new(name, curve)
    annotation_collection(scene).objects.link(obj)
    obj.location = origin
    obj[TAG] = True
    curve.materials.append(stroke_material(name, props))
    for previous in getattr(context, 'selected_objects', None) or ():
        previous.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    return obj


def selected_annotation(context):
    obj = context.view_layer.objects.active
    return obj if obj and obj.type == 'CURVE' and obj.get(TAG) else None


class CMEMS_OT_annotation_ring(bpy.types.Operator):
    bl_idname = 'cmems.annotation_ring'
    bl_label = 'Ring at 3D cursor'
    bl_description = 'Create an editable map ring centered on the 3D cursor'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        scene = context.scene
        props = scene.cmems_annotations
        scale = metres_per_unit(scene)
        origin = scene.cursor.location.copy()
        origin.z += props.lift / scale
        create_annotation(context, props.name.strip() or 'Map Ring', origin,
                          ring_paths(props.radius / scale, props.ring_style))
        return {'FINISHED'}


class CMEMS_OT_annotation_polyline(bpy.types.Operator):
    bl_idname = 'cmems.annotation_polyline'
    bl_label = 'Map polyline'
    bl_description = 'Capture points with the 3D cursor and build an editable map line'
    bl_options = {'UNDO'}
    action: EnumProperty(items=(('ADD', 'Add point', ''), ('REMOVE', 'Remove last point', ''),
                               ('CLEAR', 'Clear points', ''), ('FINISH', 'Create polyline', '')))

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        scene = context.scene
        props = scene.cmems_annotations
        points = props.points
        if self.action == 'ADD':
            position = scene.cursor.location
            if len(points) and (Vector(points[-1].position) - position).length < 1e-6:
                self.report({'WARNING'}, 'Move the cursor to a new point first.')
                return {'CANCELLED'}
            points.add().position = position
        elif self.action == 'REMOVE':
            if len(points):
                points.remove(len(points) - 1)
        elif self.action == 'CLEAR':
            points.clear()
        else:
            if len(points) < 2:
                self.report({'WARNING'}, 'Capture at least two points first.')
                return {'CANCELLED'}
            first = Vector(points[0].position)
            path = [tuple(Vector(point.position) - first) for point in points]
            first.z += props.lift / metres_per_unit(scene)
            create_annotation(context, props.name.strip() or 'Map Polyline', first, [path])
            points.clear()
        return {'FINISHED'}


class CMEMS_OT_annotation_reveal(bpy.types.Operator):
    bl_idname = 'cmems.annotation_reveal'
    bl_label = 'Keyframe reveal'
    bl_description = 'Animate the selected stroke drawing on from the current Blender frame'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return selected_annotation(context) is not None

    def execute(self, context):
        scene = context.scene
        obj = selected_annotation(context)
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        curve = obj.data
        start = scene.frame_current
        end = start + scene.cmems_annotations.reveal_frames
        curve.bevel_factor_mapping_end = 'SPLINE'
        for frame, value in ((start, 0.0), (end, 1.0)):
            curve.bevel_factor_end = value
            curve.keyframe_insert(data_path='bevel_factor_end', frame=frame, group='Annotation Reveal')
        curve.bevel_factor_end = 0.0
        self.report({'INFO'}, f'Reveal keyed from frame {start} to {end}.')
        return {'FINISHED'}


class CMEMS_OT_annotation_transform(bpy.types.Operator):
    bl_idname = 'cmems.annotation_transform'
    bl_label = 'Keyframe transform'
    bl_description = 'Key the annotation position, rotation and scale on the current Blender frame'
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return selected_annotation(context) is not None

    def execute(self, context):
        obj = selected_annotation(context)
        rotation = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}.get(
            obj.rotation_mode, 'rotation_euler')
        for field in ('location', rotation, 'scale'):
            obj.keyframe_insert(data_path=field, group='Annotation Transform')
        return {'FINISHED'}


class CMEMS_OT_annotation_window(bpy.types.Operator):
    bl_idname = 'cmems.annotation_window'
    bl_label = 'Map Annotations'
    bl_description = 'Open the annotation controls in a separate window'

    def execute(self, context):
        from . import open_control_window
        try:
            open_control_window(context, WINDOW)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


def draw_annotations(layout, context):
    props = context.scene.cmems_annotations
    style = layout.box()
    style.label(text='New annotation', icon='CURVE_DATA')
    for field in ('name', 'width', 'lift', 'color', 'opacity'):
        style.prop(props, field)
    ring = layout.box()
    ring.prop(props, 'ring_style')
    ring.prop(props, 'radius')
    ring.operator('cmems.annotation_ring', icon='CURVE_NCIRCLE')
    line = layout.box()
    line.label(text=f'Polyline · {len(props.points)} cursor points')
    line.operator('cmems.annotation_polyline', text='Add cursor point', icon='ADD').action = 'ADD'
    row = line.row(align=True)
    row.enabled = bool(len(props.points))
    row.operator('cmems.annotation_polyline', text='Remove last').action = 'REMOVE'
    row.operator('cmems.annotation_polyline', text='Clear').action = 'CLEAR'
    row = line.row()
    row.enabled = len(props.points) >= 2
    row.operator('cmems.annotation_polyline', text='Create polyline').action = 'FINISH'

    selected = layout.box()
    obj = selected_annotation(context)
    if obj is None:
        selected.label(text='Select a map annotation to edit or animate it.')
        return
    selected.label(text=obj.name)
    selected.prop(obj, 'location')
    rotation = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}.get(
        obj.rotation_mode, 'rotation_euler')
    selected.prop(obj, rotation, text='Rotation')
    selected.prop(obj, 'scale')
    selected.operator('cmems.annotation_transform', icon='KEY_HLT')
    selected.prop(obj.data, 'bevel_depth', text='Stroke depth')
    material = obj.active_material
    if material and material.node_tree:
        for name, label in (('Annotation Color', 'Stroke color'), ('Annotation Opacity', 'Stroke opacity')):
            node = material.node_tree.nodes.get(name)
            if node:
                selected.prop(node.inputs[0], 'default_value', text=label)
    selected.prop(obj.data, 'bevel_factor_end', text='Reveal', slider=True)
    selected.prop(props, 'reveal_frames')
    selected.operator('cmems.annotation_reveal', icon='KEY_HLT')


class CMEMS_PT_annotations_sidebar(bpy.types.Panel):
    bl_label = 'Map Annotations'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Annotations'

    def draw(self, context):
        draw_annotations(self.layout, context)


class CMEMS_PT_annotations_window(bpy.types.Panel):
    bl_label = 'Map Annotations'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'scene'
    bl_order = -1000

    @classmethod
    def poll(cls, context):
        return bool(context.screen and context.screen.get(WINDOW))

    def draw(self, context):
        draw_annotations(self.layout, context)


CLASSES = (CMEMS_AnnotationPoint, CMEMS_AnnotationSettings, CMEMS_OT_annotation_ring,
           CMEMS_OT_annotation_polyline, CMEMS_OT_annotation_reveal, CMEMS_OT_annotation_transform,
           CMEMS_OT_annotation_window,
           CMEMS_PT_annotations_sidebar, CMEMS_PT_annotations_window)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cmems_annotations = PointerProperty(type=CMEMS_AnnotationSettings)


def unregister():
    del bpy.types.Scene.cmems_annotations
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
