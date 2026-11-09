# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable world-space map marks using Blender curves and keyframes."""

import math

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty, StringProperty)
from mathutils import Vector

from .osm_world import metres_per_unit

WINDOW = 'cmems_annotations_window'
TAG = 'cmems_annotation'
RADIUS = 'Radius (m)'
WIDTH = 'Stroke width (m)'
BASE_RADIUS = 'cmems_annotation_base_radius'
UNIT = 'cmems_annotation_metres_per_unit'
RING_STYLES = (
    ('SOLID', 'Solid', 'Single continuous ring'),
    ('DASHED', 'Dashed', 'Segmented ring'),
    ('DOUBLE', 'Double', 'Two concentric rings'),
)


class CMEMS_AnnotationPoint(bpy.types.PropertyGroup):
    position: FloatVectorProperty(size=3)


class CMEMS_AnnotationSettings(bpy.types.PropertyGroup):
    name: StringProperty(name='Name')
    radius: FloatProperty(name='Ring radius (m)', default=250, min=0.01, soft_max=10000)
    ring_style: EnumProperty(name='Ring style', items=RING_STYLES, default='SOLID')
    width: FloatProperty(name='Stroke width (m)', default=5, min=.001, soft_max=100)
    lift: FloatProperty(name='Lift (m)', default=1, description='Offset above the captured cursor height')
    color: FloatVectorProperty(name='Stroke color', size=3, subtype='COLOR', min=0, max=1,
                               default=(.66, .80, .72))
    opacity: FloatProperty(name='Stroke opacity', default=1, min=0, max=1, subtype='FACTOR')
    reveal_frames: IntProperty(name='Reveal duration (frames)', default=24, min=1, soft_max=240)
    points: CollectionProperty(type=CMEMS_AnnotationPoint)
    show_markers: BoolProperty(name='Find an annotation', default=True)
    edit_spline: IntProperty(name='Line', default=1, min=1)
    edit_point: IntProperty(name='Point', default=1, min=1)


def ring_paths(radius, style):
    def arc(r, start, end, steps):
        return [(r * math.cos(start + (end - start) * i / steps),
                 r * math.sin(start + (end - start) * i / steps), 0) for i in range(steps + 1)]

    if style == 'DASHED':
        return [arc(radius, math.tau * i / 12, math.tau * (i + .72) / 12, 8) for i in range(12)]
    return [arc(r, 0, math.tau, 96) for r in ((radius, radius * 1.06) if style == 'DOUBLE' else (radius,))]


def add_paths(curve, paths):
    for path in paths:
        spline = curve.splines.new('POLY')
        spline.points.add(len(path) - 1)
        spline.points.foreach_set('co', [value for point in path for value in (*point, 1)])


def update_ring_style(obj, _context):
    if BASE_RADIUS in obj:
        obj.data.splines.clear()
        add_paths(obj.data, ring_paths(obj[BASE_RADIUS] / obj[UNIT], obj.cmems_ring_style))


def add_parameter_driver(owner, data_path, obj, expression, variables, index=None):
    fcurve = owner.driver_add(data_path) if index is None else owner.driver_add(data_path, index)
    driver = fcurve.driver
    driver.expression = expression
    for name, prop in variables:
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        target = variable.targets[0]
        target.id = obj
        target.data_path = f'["{prop}"]'


def add_stroke_width(obj, unit):
    obj[WIDTH] = obj.data.bevel_depth * 2 * unit
    obj.id_properties_ui(WIDTH).update(min=0.0, soft_max=100.0, description='Full stroke width in metres')
    variables = [('width', WIDTH)]
    expression = f'width / {2 * unit!r}'
    if BASE_RADIUS in obj:
        variables.append(('radius', RADIUS))
        expression += f' * {obj[BASE_RADIUS]!r} / max(radius, 0.01)'
    add_parameter_driver(obj.data, 'bevel_depth', obj, expression, variables)


def add_ring_parameters(obj, radius, style, unit):
    obj.cmems_ring_style = style
    obj[BASE_RADIUS] = obj[RADIUS] = radius
    obj[UNIT] = unit
    obj.id_properties_ui(RADIUS).update(min=0.01, soft_max=10000.0, description='Inner ring radius in metres')
    # Uniform delta scale leaves the ordinary transform available for animation.
    # The stroke driver compensates for this scale to keep its width independent.
    for axis in range(3):
        add_parameter_driver(obj, 'delta_scale', obj, f'max(radius, 0.01) / {radius!r}',
                             [('radius', RADIUS)], axis)
    add_stroke_width(obj, unit)


def recover_ring(curve):
    """Read the center and radius of a solid, dashed or double XY poly ring."""
    splines = curve.splines
    if not splines or any(s.type != 'POLY' or len(s.points) < 3 for s in splines):
        return None
    paths = [[p.co.xyz for p in s.points] for s in splines]
    first = paths[0]
    a = first[0]
    samples = [path[0] for path in paths] if len(paths) > 2 else first
    b, c = samples[len(samples) // 3] - a, samples[2 * len(samples) // 3] - a
    determinant = 2 * (b.x * c.y - b.y * c.x)
    if abs(determinant) < 1e-12:
        return None
    center = a + Vector(((b.length_squared * c.y - c.length_squared * b.y) / determinant,
                         (b.x * c.length_squared - c.x * b.length_squared) / determinant, 0))
    radii = [(path[0] - center).length for path in paths]
    tolerance = max(1e-4, radii[0] * .001)
    if any(abs((p - center).length - r) > tolerance or abs(p.z - a.z) > tolerance
           for path, r in zip(paths, radii) for p in path):
        return None
    closed = [s.use_cyclic_u or (path[0] - path[-1]).length < tolerance
              for s, path in zip(splines, paths)]
    if all(closed) and len(paths) in (1, 2):
        return center, min(radii), 'DOUBLE' if len(paths) == 2 else 'SOLID'
    if not any(closed) and len(paths) > 2 and max(radii) - min(radii) < tolerance:
        return center, radii[0], 'DASHED'
    return None


def upgrade_ring(obj, unit):
    recovered = recover_ring(obj.data)
    if recovered is None or recovered[1] * unit < .01:
        raise ValueError('Select a solid, dashed or double poly ring.')
    center, radius, style = recovered
    if (obj.animation_data or obj.parent or obj.children or obj.constraints or
            obj.data.animation_data or obj.data.shape_keys or tuple(obj.delta_scale) != (1, 1, 1)):
        raise ValueError('Radius controls require a ring with an unanimated, independent transform.')
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    obj.location += obj.matrix_world.to_3x3() @ center
    for spline in obj.data.splines:
        for point in spline.points:
            point.co.xyz -= center
    add_ring_parameters(obj, radius * unit, style, unit)


def annotation_collection(scene):
    group = next((c for c in scene.collection.children if c.get('cmems_annotations')), None)
    if group is None:
        group = bpy.data.collections.new('Map Annotations')
        group['cmems_annotations'] = True
        scene.collection.children.link(group)
    return group


def stroke_material(name, color, opacity, strength=1):
    material = bpy.data.materials.new(name + ' · stroke')
    material.diffuse_color = (*color, opacity)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    ink = tree.nodes.new('ShaderNodeEmission')
    ink.name = 'Annotation Color'
    ink.inputs[0].default_value = (*color, 1)
    ink.inputs[1].default_value = strength
    trans = tree.nodes.new('ShaderNodeBsdfTransparent')
    mix = tree.nodes.new('ShaderNodeMixShader')
    mix.name = 'Annotation Opacity'
    mix.inputs[0].default_value = opacity
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
    add_paths(curve, paths)
    obj = bpy.data.objects.new(name, curve)
    annotation_collection(scene).objects.link(obj)
    obj.location = origin
    obj[TAG] = True
    curve.materials.append(stroke_material(name, props.color, props.opacity))
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
        obj = create_annotation(context, props.name.strip() or 'Map Ring', origin,
                                ring_paths(props.radius / scale, props.ring_style))
        add_ring_parameters(obj, props.radius, props.ring_style, scale)
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
            obj = create_annotation(context, props.name.strip() or 'Map Polyline', first, [path])
            add_stroke_width(obj, metres_per_unit(scene))
            points.clear()
        return {'FINISHED'}


def active_line_point(obj, props):
    if BASE_RADIUS in obj or not obj.data.splines:
        return None
    spline = obj.data.splines[min(props.edit_spline, len(obj.data.splines)) - 1]
    if spline.type == 'POLY' and spline.points:
        return spline.points[min(props.edit_point, len(spline.points)) - 1]
    return None


class CMEMS_OT_annotation_edit(bpy.types.Operator):
    bl_idname = 'cmems.annotation_edit'
    bl_label = 'Edit map annotation'
    bl_options = {'UNDO'}

    action: EnumProperty(items=(('SELECT', 'Select annotation', ''),
                               ('RING', 'Enable radius controls', ''),
                               ('CURSOR', 'Move point to cursor', '')))
    object_name: StringProperty(options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        obj = selected_annotation(context)
        if self.action == 'SELECT':
            target = context.view_layer.objects.get(self.object_name)
            if not target or not target.visible_get(view_layer=context.view_layer):
                self.report({'WARNING'}, 'Show the annotation in the Outliner first.')
                return {'CANCELLED'}
            for item in getattr(context, 'selected_objects', None) or ():
                item.select_set(False)
            target.select_set(True)
            context.view_layer.objects.active = target
        elif self.action == 'RING' and obj and BASE_RADIUS not in obj:
            try:
                upgrade_ring(obj, metres_per_unit(context.scene))
            except ValueError as exc:
                self.report({'WARNING'}, str(exc))
                return {'CANCELLED'}
        elif self.action == 'CURSOR' and obj:
            point = active_line_point(obj, context.scene.cmems_annotations)
            if point is None:
                return {'CANCELLED'}
            point.co.xyz = obj.matrix_world.inverted_safe() @ context.scene.cursor.location
        else:
            return {'CANCELLED'}
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
    obj = selected_annotation(context)
    markers = layout.box()
    markers.prop(props, 'show_markers', icon='CURVE_DATA')
    if props.show_markers:
        for mark in context.view_layer.objects:
            if mark.type == 'CURVE' and mark.get(TAG):
                button = markers.operator('cmems.annotation_edit', text=mark.name,
                                          depress=mark == obj)
                button.action = 'SELECT'
                button.object_name = mark.name
    if obj:
        draw_selected(layout.box(), obj, props)
    else:
        layout.label(text='Select a map annotation to edit or animate it.')

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


def draw_selected(selected, obj, props):
    selected.prop(obj, 'name', text='Annotation')
    selected.prop(obj, 'location', text='Center' if BASE_RADIUS in obj else 'Location')
    if BASE_RADIUS in obj:
        selected.prop(obj, f'["{RADIUS}"]')
        selected.prop(obj, 'cmems_ring_style')
    elif WIDTH not in obj and recover_ring(obj.data):
        selected.operator('cmems.annotation_edit', text='Enable radius controls').action = 'RING'
    else:
        point = active_line_point(obj, props)
        if point is not None:
            splines = obj.data.splines
            if len(splines) > 1:
                selected.prop(props, 'edit_spline', text=f'Line (1–{len(splines)})')
            count = len(splines[min(props.edit_spline, len(splines)) - 1].points)
            selected.prop(props, 'edit_point', text=f'Point (1–{count})')
            row = selected.row(align=True)
            for axis, label in enumerate('XYZ'):
                row.prop(point, 'co', index=axis, text=label)
            selected.operator('cmems.annotation_edit', text='Move point to cursor').action = 'CURSOR'
    if WIDTH in obj:
        selected.prop(obj, f'["{WIDTH}"]')
    else:
        selected.prop(obj.data, 'bevel_depth', text='Stroke radius')
    material = obj.active_material
    if material and material.node_tree:
        for name, label in (('Annotation Color', 'Stroke color'), ('Annotation Opacity', 'Stroke opacity')):
            node = material.node_tree.nodes.get(name)
            if node:
                selected.prop(node.inputs[0], 'default_value', text=label)
    if obj.mode != 'OBJECT':
        selected.label(text='Switch to Object Mode to use the annotation controls.')
        selected.enabled = False
    transform = selected.column(align=True)
    rotation = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}.get(
        obj.rotation_mode, 'rotation_euler')
    transform.prop(obj, rotation, text='Rotation')
    transform.prop(obj, 'scale')
    transform.operator('cmems.annotation_transform', icon='KEY_HLT')
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
           CMEMS_OT_annotation_polyline, CMEMS_OT_annotation_edit,
           CMEMS_OT_annotation_reveal, CMEMS_OT_annotation_transform,
           CMEMS_OT_annotation_window,
           CMEMS_PT_annotations_sidebar, CMEMS_PT_annotations_window)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cmems_annotations = PointerProperty(type=CMEMS_AnnotationSettings)
    bpy.types.Object.cmems_ring_style = EnumProperty(name='Ring style', items=RING_STYLES,
                                                     options=set(), update=update_ring_style)


def unregister():
    del bpy.types.Object.cmems_ring_style
    del bpy.types.Scene.cmems_annotations
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
