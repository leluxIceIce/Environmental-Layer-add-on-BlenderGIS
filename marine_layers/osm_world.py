# SPDX-License-Identifier: GPL-3.0-or-later
"""A native Blender basemap that follows a georeferenced 3D viewport."""

from concurrent.futures import ThreadPoolExecutor
import math
import time

import blf
import bpy
from bpy.props import BoolProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.geometry import delaunay_2d_cdt, tessellate_polygon

from . import osm_tiles

WINDOW = 'cmems_osm_world_window'
CATEGORIES = ('land', 'water', 'boundaries', 'buildings', 'roads', 'places', 'street_names')
COLORS = {
    'land': (0.0, 0.0, 0.0),
    'water': (0.018, 0.022, 0.026),
    'boundaries': (0.14, 0.15, 0.16),
    'buildings': (0.08, 0.085, 0.09),
    'roads': (0.29, 0.30, 0.31),
    'places': (0.86, 0.87, 0.88),
    'street_names': (0.58, 0.59, 0.61),
}
TITLES = {
    'land': 'Land', 'water': 'Water', 'boundaries': 'Boundaries', 'buildings': 'Buildings',
    'roads': 'Roads', 'places': 'Place names', 'street_names': 'Road names',
}
_sessions = {}
_executor = None
_credit_handler = None


def mercator(lon, lat):
    return osm_tiles.HALF * lon / 180, osm_tiles.HALF * math.asinh(math.tan(math.radians(lat))) / math.pi


def scene_reference(scene, initialize=False):
    from . import gis_modules
    geo_module, reproj = gis_modules()
    geo = geo_module.GeoScene(scene)
    if not geo.isGeoref and initialize and not geo.isPartiallyGeoref:
        geo.crs = 'EPSG:3857'
        geo.scale = 1
        geo.setOriginPrj(*mercator(10.3, 56.1))
    if not geo.isGeoref or not reproj.SRS(geo.crs).isWM:
        raise ValueError('OSM World Map needs a BlenderGIS scene in Web Mercator (EPSG:3857).')
    if not all(math.isfinite(value) for value in (geo.crsx, geo.crsy, geo.scale)) or geo.scale <= 0:
        raise ValueError('Check the BlenderGIS scene origin and scale.')
    return geo.crsx, geo.crsy, geo.scale


def viewport(context, preferred=None):
    views = [(window, area, region) for window in context.window_manager.windows
             if window.scene == context.scene and window.screen for area in window.screen.areas
             if area.type == 'VIEW_3D' for region in area.regions if region.type == 'WINDOW']
    if preferred:
        selected = next((view for view in views if (view[0].as_pointer(), view[1].as_pointer()) == preferred), None)
        if selected:
            return selected
    if not views:
        raise ValueError('Open a 3D viewport for the map.')
    return max(views, key=lambda view: view[2].width * view[2].height)


def view_bounds(area, region, reference, elevation):
    view = area.spaces.active.region_3d
    origin_x, origin_y, scale = reference
    points = []
    for position in ((0, 0), (region.width, 0), (region.width, region.height), (0, region.height)):
        ray = view3d_utils.region_2d_to_vector_3d(region, view, position)
        if abs(ray.z) < 1e-8:
            raise ValueError('Use a top view to see the OSM ground plane.')
        origin = view3d_utils.region_2d_to_origin_3d(region, view, position,
                                                     clamp=max(10, view.view_distance * 3))
        distance = (elevation - origin.z) / ray.z
        if view.is_perspective and distance < 0:
            raise ValueError('Use a top view to see the OSM ground plane.')
        point = origin + ray * distance
        points.append((origin_x + point.x * scale, origin_y + point.y * scale))
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def set_map_clip(space, scale):
    if space.region_3d.view_perspective == 'ORTHO':
        end = max(space.clip_start * 2, 1000 / scale, space.region_3d.view_distance * 3)
        if not math.isclose(space.clip_end, end, rel_tol=1e-5):
            space.clip_end = end


def display_unit(bounds, region, scale):
    west, south, east, north = bounds
    resolution = max((east - west) / max(region.width, 1), (north - south) / max(region.height, 1))
    return resolution * 2.5 / scale


def clip_ring(points, extent):
    for axis, boundary, sign in ((0, 0, 1), (0, extent, -1), (1, 0, 1), (1, extent, -1)):
        output = []
        if not points:
            return output
        previous = points[-1]
        for current in points:
            a, b = sign * (previous[axis] - boundary), sign * (current[axis] - boundary)
            if (a >= 0) != (b >= 0):
                factor = a / (a - b)
                output.append(tuple(p + (q - p) * factor for p, q in zip(previous, current)))
            if b >= 0:
                output.append(current)
            previous = current
        points = output
    return points


def polygon_faces(paths, extent):
    groups = []
    for ring in paths:
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))
        if area > 0:
            groups.append([])
        if groups:
            groups[-1].append(clip_ring(ring, extent))
    for group in groups:
        if len(group[0]) < 3:
            continue
        loops = [[Vector((x / extent, 1 - y / extent)) for x, y in ring]
                 for ring in group if len(ring) >= 3]
        if len(loops) == 1:
            loop = [Vector((*point, 0)) for point in loops[0]]
            yield from ([loop[index] for index in triangle] for triangle in tessellate_polygon([loop]))
        else:
            vertices, rings = [], []
            for loop in loops:
                rings.append(list(range(len(vertices), len(vertices) + len(loop))))
                vertices.extend(reversed(loop))
            points, _, faces, *_ = delaunay_2d_cdt(vertices, [], rings, 5, 1e-7, False)
            yield from ([points[index] for index in face] for face in faces if len(face) >= 3)


def clipped_paths(path, extent):
    result = []
    for first, last in zip(path, path[1:]):
        dx, dy = last[0] - first[0], last[1] - first[1]
        low, high = 0.0, 1.0
        for slope, distance in ((-dx, first[0]), (dx, extent-first[0]),
                                (-dy, first[1]), (dy, extent-first[1])):
            if slope < 0:
                low = max(low, distance / slope)
            elif slope > 0:
                high = min(high, distance / slope)
            elif distance < 0:
                low, high = 1, 0
        if low >= high:
            if result:
                yield result
                result = []
            continue
        a = (first[0] + low * dx, first[1] + low * dy)
        b = (first[0] + high * dx, first[1] + high * dy)
        if result and result[-1] != a:
            yield result
            result = []
        if not result:
            result.append(a)
        result.append(b)
    if result:
        yield result


def make_material(name):
    material = bpy.data.materials.new(f'OSM · {name}')
    material['cmems_osm_material'] = True
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    emission.name = 'OSM Color'
    mix = tree.nodes.new('ShaderNodeMixShader')
    mix.name = 'OSM Opacity'
    transparent = tree.nodes.new('ShaderNodeBsdfTransparent')
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs[0])
    return material


def apply_style(scene, root=None):
    props = scene.cmems_osm
    root = root or props.map_collection
    if root is None:
        return
    pixel = root.get('osm_display_unit')
    root.hide_viewport = root.hide_render = not props.visible
    for category in root.children:
        kind = category.get('osm_category')
        if kind not in CATEGORIES:
            continue
        category.hide_viewport = category.hide_render = not getattr(props, kind + '_visible', True)
        objects = [obj for obj in category.objects if obj.get('cmems_osm_geometry')]
        if not objects:
            continue
        material = bpy.data.materials.get(category.get('osm_material', ''))
        color = (*getattr(props, kind + '_color'), 1)
        opacity = (props.road_opacity if kind == 'roads' else
                   props.label_opacity if kind in ('places', 'street_names') else 1)
        if material and material.node_tree:
            material.diffuse_color = (*color[:3], opacity)
            for name, value in (('OSM Color', color), ('OSM Opacity', opacity)):
                node = material.node_tree.nodes.get(name)
                if node:
                    node.inputs[0].default_value = value
        for obj in objects:
            unit = pixel or obj['osm_pixel']
            obj.location.z = props.elevation - obj.get('osm_drop', 0)
            if obj.type == 'CURVE':
                width = props.road_width if kind == 'roads' else 0.85
                obj.data.bevel_depth = unit * width / 2
                obj.scale.z = min(1, obj['osm_drop'] / (4 * obj.data.bevel_depth))
            if obj.type == 'FONT':
                size = props.place_size if kind == 'places' else props.street_size
                obj.scale.x = obj.scale.y = unit * size
                label = obj['osm_label']
                obj.data.body = label.upper() if props.uppercase else label


def style_changed(self, context):
    apply_style(self.id_data)


def color_property(kind):
    return FloatVectorProperty(name=TITLES[kind], subtype='COLOR', size=3, min=0, max=1,
                               default=COLORS[kind], update=style_changed)


class CMEMS_OSM_settings(bpy.types.PropertyGroup):
    visible: BoolProperty(name='Show world map', default=True, update=style_changed)
    water_visible: BoolProperty(name='Water', default=True, update=style_changed)
    boundaries_visible: BoolProperty(name='Boundaries', default=True, update=style_changed)
    buildings_visible: BoolProperty(name='Buildings', default=True, update=style_changed)
    roads_visible: BoolProperty(name='Roads', default=True, update=style_changed)
    places_visible: BoolProperty(name='Place names', default=True, update=style_changed)
    street_names_visible: BoolProperty(name='Road names', default=True, update=style_changed)
    land_color: color_property('land')
    water_color: color_property('water')
    boundaries_color: color_property('boundaries')
    buildings_color: color_property('buildings')
    roads_color: color_property('roads')
    places_color: color_property('places')
    street_names_color: color_property('street_names')
    road_width: FloatProperty(name='Road width', default=1.1, min=0.1, max=8, update=style_changed)
    road_opacity: FloatProperty(name='Road opacity', default=1, min=0, max=1, update=style_changed)
    place_size: FloatProperty(name='Place label size', default=14, min=5, max=48, update=style_changed)
    street_size: FloatProperty(name='Road label size', default=10, min=5, max=48, update=style_changed)
    label_opacity: FloatProperty(name='Label opacity', default=1, min=0, max=1, update=style_changed)
    uppercase: BoolProperty(name='Uppercase labels', default=False, update=style_changed)
    elevation: FloatProperty(name='Map Z (scene units)', default=-0.25, update=style_changed)
    cache_path: StringProperty(name='OSM cache folder', subtype='DIR_PATH')
    cache_mb: IntProperty(name='Cache limit (MB)', default=512, min=32, max=8192)
    cache_only: BoolProperty(name='Cache only', default=False)
    map_collection: PointerProperty(type=bpy.types.Collection, options={'HIDDEN'})
    status: StringProperty(default='Start the map to load the visible area.')


def purge(root):
    for child in list(root.children):
        if child.get('osm_category') in CATEGORIES and child.users == 1:
            purge(child)
    for obj in list(root.objects):
        if not obj.get('cmems_osm_geometry'):
            continue
        if len(obj.users_collection) > 1:
            root.objects.unlink(obj)
            continue
        data = obj.data
        materials = list(data.materials)
        bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            (bpy.data.meshes if isinstance(data, bpy.types.Mesh) else bpy.data.curves).remove(data)
        for material in materials:
            if material and material.users == 0 and material.get('cmems_osm_material'):
                bpy.data.materials.remove(material)
    if not root.children and not root.objects:
        bpy.data.collections.remove(root)


class MapBuild:
    def __init__(self, tiles, reference, bounds, pixel=None):
        self.reference = reference
        self.bounds = bounds
        self.pending = iter(tiles.items())
        self.root = bpy.data.collections.new('OSM World Map')
        self.root['Attribution'] = osm_tiles.ATTRIBUTION
        if pixel:
            self.root['osm_display_unit'] = pixel
        self.pixel = pixel
        self.categories = {}
        self.materials = {}
        self.labels = []

    def link(self, kind, data, location, drop, pixel):
        if kind not in self.categories:
            collection = bpy.data.collections.new(TITLES[kind])
            collection['osm_category'] = kind
            self.root.children.link(collection)
            self.categories[kind] = collection
            self.materials[kind] = make_material(TITLES[kind])
            collection['osm_material'] = self.materials[kind].name
        data.materials.append(self.materials[kind])
        obj = bpy.data.objects.new(TITLES[kind], data)
        obj.location = (*location, 0)
        obj['cmems_osm_geometry'] = True
        obj['osm_drop'] = drop
        obj['osm_pixel'] = pixel
        obj.hide_select = True
        self.categories[kind].objects.link(obj)
        return obj

    def mesh(self, kind, faces, span, location, drop, pixel):
        vertices, polygons = [], []
        for face in faces:
            polygons.append(tuple(range(len(vertices), len(vertices) + len(face))))
            vertices.extend((point[0] * span, point[1] * span, 0) for point in face)
        if polygons:
            mesh = bpy.data.meshes.new(TITLES[kind])
            mesh.from_pydata(vertices, [], polygons)
            self.link(kind, mesh, location, drop, pixel)

    def lines(self, kind, paths, extent, span, location, drop, pixel):
        curve = None
        for path, weight in paths:
            for clipped in clipped_paths(path, extent):
                if curve is None:
                    curve = bpy.data.curves.new(TITLES[kind], 'CURVE')
                    curve.dimensions = '3D'
                    curve.bevel_resolution = 0
                spline = curve.splines.new('POLY')
                spline.points.add(len(clipped) - 1)
                for point, (x, y) in zip(spline.points, clipped):
                    point.co = (x / extent * span, (1 - y / extent) * span, 0, 1)
                    point.radius = weight
        if curve is not None:
            self.link(kind, curve, location, drop, pixel)

    def add_labels(self, kind, layer, zoom, span, location, pixel):
        extent, features = layer
        for shape, paths, tags in features:
            name = tags.get('name:latin') or tags.get('name:en') or tags.get('name')
            if not isinstance(name, str) or not name.strip():
                continue
            if kind == 'places':
                place = tags.get('class')
                capital = str(tags.get('capital')) == '2'
                if zoom <= 4 and place != 'country' and not capital:
                    continue
                if zoom <= 8 and place not in ('continent', 'country', 'state', 'city', 'town') and not capital:
                    continue
                if shape != 1:
                    continue
                points = (point for path in paths for point in path)
                priority = {'continent': 0, 'country': 1, 'city': 4, 'town': 5}.get(place, 6)
                if capital:
                    priority = 2
            else:
                if shape != 2:
                    continue
                path = max((line for path in paths for line in clipped_paths(path, extent)),
                           key=len, default=None)
                if not path:
                    continue
                middle = len(path) // 2
                points = [tuple((a + b) / 2 for a, b in zip(path[middle - 1], path[middle]))]
                priority = 9
            for x, y in points:
                if 0 <= x < extent and 0 <= y < extent:
                    position = location[0] + x / extent * span, location[1] + (1 - y / extent) * span
                    self.labels.append((priority, name[:90], kind, position, pixel))

    def add_tile(self, tile, layers):
        zoom = tile[0]
        x, y, size = osm_tiles.tile_bounds(tile)
        origin_x, origin_y, scale = self.reference
        location = (x - origin_x) / scale, (y - origin_y) / scale
        span = size / scale
        pixel = self.pixel or span / 384
        gap = max(0.002, span / 400_000)
        self.mesh('land', [((0, 0), (1, 0), (1, 1), (0, 1))], span, location, gap * 5, pixel)
        for kind, name, depth in (('water', 'water', 4), ('buildings', 'building', 2)):
            if name in layers:
                extent, features = layers[name]
                faces = (face for shape, paths, _ in features if shape == 3
                         for face in polygon_faces(paths, extent))
                self.mesh(kind, faces, span, location, gap * depth, pixel)
        for kind, name, minimum_zoom, depth in (('boundaries', 'boundary', 0, 3),
                                                ('water', 'waterway', 10, 3),
                                                ('roads', 'transportation', 9, 1)):
            if zoom < minimum_zoom or name not in layers:
                continue
            extent, features = layers[name]
            paths = ((path, 1.7 if tags.get('class') in ('motorway', 'trunk', 'primary') else 1)
                     for shape, paths, tags in features if shape == 2 for path in paths)
            self.lines(kind, paths, extent, span, location, gap * depth, pixel)
        if 'place' in layers:
            self.add_labels('places', layers['place'], zoom, span, location, pixel)
        if zoom >= 13 and 'transportation_name' in layers:
            self.add_labels('street_names', layers['transportation_name'], zoom, span, location, pixel)

    def finish_labels(self):
        origin_x, origin_y, scale = self.reference
        west, south, east, north = self.bounds
        seen, occupied = set(), []
        def outside_view(label):
            x, y = label[3]
            return not (west <= origin_x + x * scale <= east and south <= origin_y + y * scale <= north)

        for _, name, kind, position, pixel in sorted(self.labels, key=lambda label: (outside_view(label), label)):
            x, y = position
            key = kind, name
            size = pixel * (14 if kind == 'places' else 10)
            half_width = size * (len(name) * 0.28 + 0.5)
            left, right, bottom, top = x - half_width, x + half_width, y - size * 0.25, y + size
            if key in seen or any(left < r and right > l and bottom < t and top > b for l, r, b, t in occupied):
                continue
            occupied.append((left, right, bottom, top))
            seen.add(key)
            font = bpy.data.curves.new(name, 'FONT')
            font.align_x = 'CENTER'
            obj = self.link(kind, font, position, 0, pixel)
            obj.name = name
            obj['osm_label'] = name
            if len(occupied) >= 100:
                break

    def step(self):
        item = next(self.pending, None)
        if item is None:
            self.finish_labels()
            return True
        self.add_tile(*item)
        return False


def cache_folder(scene):
    from . import cache_folder as marine_cache
    folder = scene.cmems_osm.cache_path.strip()
    if folder:
        from . import absolute_output_path
        return absolute_output_path(folder) / 'openfreemap'
    return marine_cache(bpy.context) / 'osm-world'


class MapSession:
    def __init__(self, scene, window, area):
        self.scene = scene
        self.preferred = window.as_pointer(), area.as_pointer()
        self.desired = self.applied = self.failed = None
        self.since = 0
        self.future = self.build = None

    def cancel(self):
        if self.future:
            self.future.cancel()
        if self.build:
            purge(self.build.root)
            self.build = None

    def tick(self):
        props = self.scene.cmems_osm
        window = next((window for window in bpy.context.window_manager.windows if window.scene == self.scene), None)
        if window is None:
            return
        with bpy.context.temp_override(window=window):
            _, area, region = viewport(bpy.context, self.preferred)
        reference = scene_reference(self.scene)
        set_map_clip(area.spaces.active, reference[2])
        bounds = view_bounds(area, region, reference, props.elevation)
        pixel = display_unit(bounds, region, reference[2])
        tiles = osm_tiles.tile_plan(bounds, (region.width, region.height))
        folder = cache_folder(self.scene)
        desired = tiles, reference, folder, props.cache_only, props.cache_mb
        if desired != self.desired:
            self.desired, self.since = desired, time.monotonic()
            if self.build:
                purge(self.build.root)
                self.build = None
        if self.future and self.future.done():
            future, self.future = self.future, None
            if self.request == desired:
                data = future.result()
                if not data:
                    raise ValueError('No cached tiles for this view. Turn off Cache only to load it.')
                self.build = MapBuild(data, reference, bounds, pixel)
                self.loaded = len(data)
        if self.build:
            if self.build.step():
                root = self.build.root
                apply_style(self.scene, root)
                previous = props.map_collection
                self.scene.collection.children.link(root)
                props.map_collection = root
                self.build = None
                if previous and previous.name in self.scene.collection.children:
                    if previous.users == 1:
                        purge(previous)
                    else:
                        self.scene.collection.children.unlink(previous)
                self.applied = desired
                props.status = f'Zoom {tiles[0][0]} · {self.loaded}/{len(tiles)} tiles' + (
                    ' · cache only' if props.cache_only else '')
            return
        if desired == self.applied and props.map_collection:
            root = props.map_collection
            if not math.isclose(root.get('osm_display_unit', 0), pixel, rel_tol=0.12):
                root['osm_display_unit'] = pixel
                apply_style(self.scene, root)
        if not tiles:
            props.status = 'View is outside the Web Mercator map extent.'
        elif (self.future is None and desired not in (self.applied, self.failed)
              and time.monotonic() - self.since >= 0.25):
            self.request = desired
            self.future = _executor.submit(osm_tiles.fetch_tiles, folder, tiles, props.cache_only, props.cache_mb)
            props.status = f'Loading OSM zoom {tiles[0][0]} · {len(tiles)} tiles…'


def tick_maps():
    for key, session in tuple(_sessions.items()):
        try:
            session.tick()
        except ReferenceError:
            session.cancel()
            _sessions.pop(key, None)
        except Exception as exc:
            session.cancel()
            session.future = None
            session.failed = session.desired
            session.scene.cmems_osm.status = str(exc)
    for window in bpy.context.window_manager.windows:
        if window.screen and window.screen.get(WINDOW):
            for area in window.screen.areas:
                area.tag_redraw()
    return 0.18 if _sessions else None


def stop_all():
    global _executor
    for session in _sessions.values():
        session.cancel()
    _sessions.clear()
    if bpy.app.timers.is_registered(tick_maps):
        bpy.app.timers.unregister(tick_maps)
    if _executor:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


class CMEMS_OT_osm_view(bpy.types.Operator):
    bl_idname = 'cmems.osm_view'
    bl_label = 'OSM view'
    destination: StringProperty(default='')

    def execute(self, context):
        global _executor
        scene = context.scene
        key = scene.as_pointer()
        if self.destination == 'STOP':
            session = _sessions.pop(key, None)
            if session:
                session.cancel()
            scene.cmems_osm.status = 'Paused · last map remains available'
            return {'FINISHED'}
        try:
            from . import geo_signature
            has_origin = all(value is not None for value in geo_signature(scene)[:3])
            reference = scene_reference(scene, initialize=True)
            previous = _sessions.get(key)
            preferred = previous.preferred if previous else None
            window, area, region = viewport(context, preferred)
            origin_x, origin_y, scale = reference
            space = area.spaces.active
            view = space.region_3d
            view.view_perspective = 'ORTHO'
            view.view_rotation = (1, 0, 0, 0)
            if self.destination in ('WORLD', 'DENMARK') or not has_origin:
                if self.destination == 'WORLD':
                    x, y, span = 0, 0, osm_tiles.WORLD
                else:
                    x, y = mercator(10.3, 56.1)
                    span = 850_000
                view.view_location = ((x - origin_x) / scale, (y - origin_y) / scale, 0)
                aspect = max(region.width, region.height) / max(1, min(region.width, region.height))
                view.view_distance = span / scale * (space.lens / 50) * max(1, 0.76 * aspect)
            set_map_clip(space, scale)
            space.overlay.show_floor = space.overlay.show_ortho_grid = False
            space.overlay.show_axis_x = space.overlay.show_axis_y = False
            if space.shading.type in ('SOLID', 'WIREFRAME'):
                space.shading.type = 'MATERIAL'
            area.tag_redraw()
            if previous:
                previous.cancel()
            _sessions[key] = MapSession(scene, window, area)
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='osm-world')
            if not bpy.app.timers.is_registered(tick_maps):
                bpy.app.timers.register(tick_maps, first_interval=0.25)
            return {'FINISHED'}
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMEMS_OT_osm_window(bpy.types.Operator):
    bl_idname = 'cmems.osm_window'
    bl_label = 'OSM World Map'

    def execute(self, context):
        from . import open_control_window
        try:
            open_control_window(context, WINDOW)
            return {'FINISHED'}
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def draw_map_panel(layout, context):
    from . import wrapped
    props = context.scene.cmems_osm
    row = layout.row(align=True)
    row.operator('cmems.osm_view', text='World', icon='WORLD').destination = 'WORLD'
    row.operator('cmems.osm_view', text='Denmark').destination = 'DENMARK'
    row = layout.row(align=True)
    row.operator('cmems.osm_view', text='Load current view').destination = 'VIEW'
    row.operator('cmems.osm_view', text='Pause').destination = 'STOP'
    layout.prop(props, 'visible')
    wrapped(layout, props.status)
    style = layout.box()
    style.label(text='Map style', icon='COLOR')
    for kind in CATEGORIES:
        row = style.row(align=True)
        if hasattr(props, kind + '_visible'):
            row.prop(props, kind + '_visible', text='')
        row.prop(props, kind + '_color')
    style.prop(props, 'road_width')
    style.prop(props, 'road_opacity')
    style.prop(props, 'place_size')
    style.prop(props, 'street_size')
    style.prop(props, 'label_opacity')
    style.prop(props, 'uppercase')
    style.prop(props, 'elevation')
    cache = layout.box()
    cache.label(text='On-demand cache', icon='FILE_FOLDER')
    cache.prop(props, 'cache_path')
    cache.prop(props, 'cache_mb')
    cache.prop(props, 'cache_only')
    wrapped(layout, osm_tiles.ATTRIBUTION, width=55)
    layout.operator('wm.url_open', text='OpenStreetMap data licence', icon='URL').url = (
        'https://www.openstreetmap.org/copyright')


def draw_map_credit():
    props = bpy.context.scene.cmems_osm
    if not props.visible or not props.map_collection:
        return
    font = 0
    blf.size(font, 12)
    blf.position(font, 12, 14, 0)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0, 0, 0, 1)
    blf.shadow_offset(font, 1, -1)
    blf.color(font, 1, 1, 1, 1)
    blf.draw(font, osm_tiles.ATTRIBUTION)
    blf.disable(font, blf.SHADOW)


class CMEMS_PT_osm_sidebar(bpy.types.Panel):
    bl_idname = 'CMEMS_PT_osm_sidebar'
    bl_label = 'OSM World Map'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GIS'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_map_panel(self.layout, context)


class CMEMS_PT_osm_window(bpy.types.Panel):
    bl_idname = 'CMEMS_PT_osm_window'
    bl_label = 'OSM World Map'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'scene'
    bl_order = -1000

    @classmethod
    def poll(cls, context):
        return bool(context.screen and context.screen.get(WINDOW))

    def draw(self, context):
        draw_map_panel(self.layout, context)


CLASSES = (CMEMS_OSM_settings, CMEMS_OT_osm_view, CMEMS_OT_osm_window, CMEMS_PT_osm_sidebar, CMEMS_PT_osm_window)


def register():
    global _credit_handler
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cmems_osm = PointerProperty(type=CMEMS_OSM_settings)
    _credit_handler = bpy.types.SpaceView3D.draw_handler_add(draw_map_credit, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _credit_handler
    stop_all()
    bpy.types.SpaceView3D.draw_handler_remove(_credit_handler, 'WINDOW')
    _credit_handler = None
    del bpy.types.Scene.cmems_osm
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
