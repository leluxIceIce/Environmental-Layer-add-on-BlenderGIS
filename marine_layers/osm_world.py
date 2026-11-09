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
from mathutils.geometry import delaunay_2d_cdt

from . import osm_terrain, osm_tiles

WINDOW = 'cmems_osm_world_window'
GROUND_SCHEMA = 3
TILE_RING = ((0, 0), (1, 0), (1, 1), (0, 1))
CATEGORIES = ('land', 'water', 'boundaries', 'buildings', 'roads', 'side_roads', 'paths', 'places', 'street_names')
OPACITY_FIELDS = {kind: kind + '_opacity' for kind in CATEGORIES}
OPACITY_FIELDS['roads'] = 'road_opacity'
COLORS = {
    'land': (0.0, 0.0, 0.0),
    'water': (0.018, 0.022, 0.026),
    'boundaries': (0.14, 0.15, 0.16),
    'buildings': (0.08, 0.085, 0.09),
    'roads': (0.29, 0.30, 0.31),
    'side_roads': (0.23, 0.24, 0.25),
    'paths': (0.26, 0.24, 0.21),
    'places': (0.86, 0.87, 0.88),
    'street_names': (0.58, 0.59, 0.61),
}
TITLES = {
    'land': 'Land', 'water': 'Water', 'boundaries': 'Boundaries', 'buildings': 'Buildings',
    'roads': 'Main roads', 'side_roads': 'Side roads', 'paths': 'Paths & sidewalks',
    'places': 'City & place labels', 'street_names': 'Road labels',
}
ROAD_WEIGHTS = {
    'motorway': 1.45, 'trunk': 1.25, 'primary': 1, 'secondary': .82, 'tertiary': .68,
    'minor': .58, 'service': .40, 'track': .34, 'raceway': .5, 'busway': .48,
}
ROAD_KINDS = ('roads', 'side_roads', 'paths')
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
    if view.is_perspective:
        return focus_bounds(view, reference, area.spaces.active.lens, region.width / max(1, region.height))
    points = []
    for position in ((0, 0), (region.width, 0), (region.width, region.height), (0, region.height)):
        ray = view3d_utils.region_2d_to_vector_3d(region, view, position)
        if abs(ray.z) < 0.35:
            return focus_bounds(view, reference, area.spaces.active.lens, region.width / max(1, region.height))
        origin = view3d_utils.region_2d_to_origin_3d(region, view, position,
                                                     clamp=max(10, view.view_distance * 3))
        distance = (elevation - origin.z) / ray.z
        point = origin + ray * distance
        points.append((origin_x + point.x * scale, origin_y + point.y * scale))
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def focus_bounds(view, reference, lens=50, aspect=1):
    origin_x, origin_y, scale = reference
    x, y = view.view_location.xy
    x, y = origin_x + x * scale, origin_y + y * scale
    radius = max(1, view.view_distance * scale) * 0.8 * 50 / max(1, lens)
    rx, ry = radius * min(1, aspect), radius / max(1, aspect)
    return x - rx, y - ry, x + rx, y + ry


def set_map_clip(space, scale):
    view = space.region_3d
    if view.view_perspective == 'CAMERA':
        return
    perspective = view.view_perspective == 'PERSP'
    distance = view.view_distance
    # Prepare the near plane before Blender switches from orthographic to perspective.
    start = max(0.001 / scale, distance * 0.04)
    end = max(start * 2, 100 / scale, distance * (8 if perspective else 3))
    if not math.isclose(space.clip_end, end, rel_tol=0.02):
        space.clip_end = end
    if not math.isclose(space.clip_start, start, rel_tol=0.02):
        space.clip_start = start


def metres_per_unit(scene):
    if scene.unit_settings.system != 'NONE':
        return scene.unit_settings.scale_length
    return max(scene.get('scale', 1.0), 1e-6)


def map_height(scene):
    return scene.cmems_osm.relief_m / metres_per_unit(scene)


def display_unit(bounds, region, scale):
    west, south, east, north = bounds
    resolution = max((east - west) / max(region.width, 1), (north - south) / max(region.height, 1))
    return resolution * 1.6 / scale


def polygon_rings(paths, extent):
    groups = []
    for ring in paths:
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))
        if area > 0:
            groups.append([])
        if groups and area:
            points = reversed(ring) if area > 0 else ring
            groups[-1].append([(x / extent, 1 - y / extent) for x, y in points])
    return groups


def split_ground(groups, divisions=0, inland=()):
    """Triangulate a tile once, using the source rings to classify each face."""
    vertices = [Vector(point) for point in TILE_RING]
    rings = [list(range(4))]
    owners = [-1]
    edges = []
    if divisions:
        side = divisions + 1
        vertices.extend(Vector((x / divisions, y / divisions)) for y in range(side) for x in range(side))
        for y in range(side):
            for x in range(side):
                index = 4 + y * side + x
                if x < divisions:
                    edges.append((index, index + 1))
                if y < divisions:
                    edges.append((index, index + side))
                if x < divisions and y < divisions:
                    edges.append((index, index + side + 1))
    for index, group in enumerate(groups):
        for loop in group:
            start = len(vertices)
            vertices.extend(Vector(point) for point in loop)
            rings.append(list(range(start, len(vertices))))
            owners.append(index)
    points, _, faces, _, _, origins = delaunay_2d_cdt(vertices, edges, rings, 0, 1e-7)
    land, water, lakes = [], [], []
    for face, sources in zip(faces, origins):
        if 0 not in sources:
            continue
        inside = set()
        for ring in sources:
            if ring != 0:
                inside.symmetric_difference_update((owners[ring],))
        target = (lakes if inside.issubset(inland) else water) if inside else land
        target.append([points[index] for index in face])
    return land, water, lakes


def polygon_faces(paths, extent):
    return split_ground(polygon_rings(paths, extent))[1]


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


def make_material(name, hillshade=False):
    material = bpy.data.materials.new(f'OSM · {name}')
    material['cmems_osm_material'] = True
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    emission.name = 'OSM Color'
    if hillshade:
        base = tree.nodes.new('ShaderNodeRGB')
        base.name = 'OSM Base Color'
        normal = tree.nodes.new('ShaderNodeNewGeometry')
        light = tree.nodes.new('ShaderNodeVectorMath')
        light.operation = 'DOT_PRODUCT'
        light.inputs[1].default_value = (0.62, -0.41, 0.67)
        slope = tree.nodes.new('ShaderNodeMath')
        slope.operation = 'SUBTRACT'
        slope.inputs[1].default_value = 0.67
        slope.use_clamp = True
        shade = tree.nodes.new('ShaderNodeMath')
        shade.name = 'OSM Hillshade'
        shade.operation = 'MULTIPLY'
        shade.use_clamp = True
        tint = tree.nodes.new('ShaderNodeMixRGB')
        tint.blend_type = 'ADD'
        tint.inputs[0].default_value = 1
        tree.links.new(normal.outputs['Normal'], light.inputs[0])
        tree.links.new(light.outputs['Value'], slope.inputs[0])
        tree.links.new(slope.outputs[0], shade.inputs[0])
        tree.links.new(base.outputs[0], tint.inputs[1])
        tree.links.new(shade.outputs[0], tint.inputs[2])
        tree.links.new(tint.outputs[0], emission.inputs[0])
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
    height = map_height(scene)
    new_ground = root.get('osm_ground_schema', 0) >= 1
    shade_gain = min(12, 2 ** (max(0, 10 - root.get('osm_zoom', 10)) / 2))
    root.hide_viewport = root.hide_render = not props.visible
    for category in root.children:
        kind = category.get('osm_category')
        if kind not in CATEGORIES:
            continue
        opacity = getattr(props, OPACITY_FIELDS[kind])
        hidden = opacity == 0 or not getattr(props, kind + '_visible')
        category.hide_viewport = category.hide_render = hidden
        objects = [obj for obj in category.objects if obj.get('cmems_osm_geometry')]
        if not objects:
            continue
        material = bpy.data.materials.get(category.get('osm_material', ''))
        color = (*getattr(props, kind + '_color'), 1)
        if material and material.node_tree:
            material.diffuse_color = (*color[:3], opacity)
            nodes = material.node_tree.nodes
            if nodes.get('OSM Base Color') and nodes.get('OSM Hillshade'):
                nodes['OSM Base Color'].outputs[0].default_value = color
                nodes['OSM Hillshade'].inputs[1].default_value = (
                    props.hillshade * shade_gain if props.terrain_enabled else 0)
            elif nodes.get('OSM Color'):
                nodes['OSM Color'].inputs[0].default_value = color
            if nodes.get('OSM Opacity'):
                nodes['OSM Opacity'].inputs[0].default_value = opacity
        for obj in objects:
            unit = pixel or obj['osm_pixel']
            offset = obj.get('osm_z_offset', obj.get('osm_drop', 0))
            draped = obj.get('osm_draped')
            relief = props.terrain_exaggeration if props.terrain_enabled else 0
            if new_ground:
                if kind == 'water' and obj.type == 'MESH' and not draped:
                    obj.location.z = min(height, 0) + offset if props.water_underlay else height
                else:
                    obj.location.z = height + offset + obj.get('osm_elevation', 0) * relief
            if draped:
                obj.scale.z = max(relief, 1e-6) * obj.get('osm_curve_profile', 1)
            if obj.type == 'CURVE':
                width = props.road_width if kind in ROAD_KINDS else 0.65
                obj.data.bevel_depth = unit * width / 2
                if not draped:
                    obj.scale.z = min(1, offset / (4 * obj.data.bevel_depth))
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


def opacity_property(kind):
    return FloatProperty(name='Opacity', description=f'Opacity of {TITLES[kind].lower()}',
                         default=1, min=0, max=1, subtype='FACTOR', update=style_changed)


class CMEMS_OSM_settings(bpy.types.PropertyGroup):
    visible: BoolProperty(name='Show world map', default=True, update=style_changed)
    land_visible: BoolProperty(name='Land', default=True, update=style_changed)
    water_visible: BoolProperty(name='Water', default=True, update=style_changed)
    boundaries_visible: BoolProperty(name='Boundaries', default=True, update=style_changed)
    buildings_visible: BoolProperty(name='Buildings', default=True, update=style_changed)
    roads_visible: BoolProperty(name=TITLES['roads'], default=True, update=style_changed)
    side_roads_visible: BoolProperty(name=TITLES['side_roads'], default=True, update=style_changed)
    paths_visible: BoolProperty(name=TITLES['paths'], default=True, update=style_changed)
    places_visible: BoolProperty(name=TITLES['places'], default=True, update=style_changed)
    street_names_visible: BoolProperty(name=TITLES['street_names'], default=True, update=style_changed)
    land_color: color_property('land')
    water_color: color_property('water')
    boundaries_color: color_property('boundaries')
    buildings_color: color_property('buildings')
    roads_color: color_property('roads')
    side_roads_color: color_property('side_roads')
    paths_color: color_property('paths')
    places_color: color_property('places')
    street_names_color: color_property('street_names')
    land_opacity: opacity_property('land')
    water_opacity: opacity_property('water')
    boundaries_opacity: opacity_property('boundaries')
    buildings_opacity: opacity_property('buildings')
    road_opacity: opacity_property('roads')
    side_roads_opacity: opacity_property('side_roads')
    paths_opacity: opacity_property('paths')
    places_opacity: opacity_property('places')
    street_names_opacity: opacity_property('street_names')
    road_width: FloatProperty(name='Road width', default=1.1, min=0.1, max=8, update=style_changed)
    place_size: FloatProperty(name='Place label size', default=10, min=2, max=48, update=style_changed)
    street_size: FloatProperty(name='Road label size', default=8, min=2, max=48, update=style_changed)
    uppercase: BoolProperty(name='Uppercase labels', default=False, update=style_changed)
    terrain_enabled: BoolProperty(name='Terrain relief', default=True, update=style_changed,
                                  description='Load measured Mapzen elevation automatically as the map updates')
    terrain_exaggeration: FloatProperty(name='Vertical exaggeration', default=1, min=.1, max=8,
                                        update=style_changed)
    hillshade: FloatProperty(name='Hillshade', default=.45, min=0, max=1, subtype='FACTOR', update=style_changed)
    relief_m: FloatProperty(name='Z offset (m)', default=0.01, precision=4, update=style_changed,
                            description='Vertical offset added to the OSM surface, in metres')
    water_underlay: BoolProperty(name='Water below environmental layers', default=True, update=style_changed,
                                 description='Put OSM water behind environmental surfaces at world Z = 0')
    auto_clip: BoolProperty(name='Automatic viewport clipping', default=True,
                            description='Adjust clipping in the map viewport to improve depth precision while orbiting')
    cache_path: StringProperty(name='OSM cache folder', subtype='DIR_PATH')
    cache_mb: IntProperty(name='Cache limit (MB)', default=512, min=32, max=8192)
    cache_only: BoolProperty(name='Cache only', default=False)
    follow_view: BoolProperty(default=True, options={'HIDDEN'})
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
    def __init__(self, tiles, reference, bounds, pixel=None, unit_scale=1, elevation_paths=None):
        self.reference = reference
        self.bounds = bounds
        self.pending = iter(tiles.items())
        self.built = 0
        self.root = bpy.data.collections.new('OSM World Map')
        self.root['Attribution'] = osm_tiles.ATTRIBUTION
        self.root['osm_ground_schema'] = GROUND_SCHEMA
        if tiles:
            self.root['osm_zoom'] = next(iter(tiles))[0]
        elevation_paths = elevation_paths or {}
        self.terrain = osm_terrain.TerrainTiles(elevation_paths if all(elevation_paths.values()) else {})
        if self.terrain.paths:
            self.root['Terrain attribution'] = osm_terrain.ATTRIBUTION
            self.root['Terrain source credits'] = osm_terrain.CREDITS_URL
            self.root['osm_terrain_tiles'] = len(self.terrain.paths)
            self.root['osm_terrain_zoom'] = self.terrain.zoom
        if pixel:
            self.root['osm_display_unit'] = pixel
        self.pixel = pixel
        self.unit_scale = unit_scale
        self.categories = {}
        self.materials = {}
        self.labels = []

    def link(self, kind, data, location, offset, pixel):
        if kind not in self.categories:
            collection = bpy.data.collections.new(TITLES[kind])
            collection['osm_category'] = kind
            self.root.children.link(collection)
            self.categories[kind] = collection
            self.materials[kind] = make_material(TITLES[kind], kind == 'land' and bool(self.terrain.paths))
            collection['osm_material'] = self.materials[kind].name
        data.materials.append(self.materials[kind])
        obj = bpy.data.objects.new(TITLES[kind], data)
        obj.location = (*location, 0)
        obj['cmems_osm_geometry'] = True
        obj['osm_z_offset'] = offset
        obj['osm_pixel'] = pixel
        obj.hide_select = True
        self.categories[kind].objects.link(obj)
        return obj

    def mesh(self, kind, faces, span, location, offset, pixel, terrain=None):
        vertices, polygons, indices = [], [], {}
        for face in faces:
            polygon = []
            for point in face:
                x, y = point[:2]
                key = x, y
                if key not in indices:
                    indices[key] = len(vertices)
                    height = terrain.elevation(x, y) if terrain else 0
                    vertices.append((x * span, y * span, height))
                polygon.append(indices[key])
            polygons.append(polygon)
        if polygons:
            mesh = bpy.data.meshes.new(TITLES[kind])
            mesh.from_pydata(vertices, [], polygons)
            if terrain and kind == 'land':
                mesh.polygons.foreach_set('use_smooth', [True] * len(polygons))
            obj = self.link(kind, mesh, location, offset, pixel)
            obj['osm_draped'] = terrain is not None

    def lines(self, kind, paths, extent, span, location, offset, pixel, terrain=None):
        curve = None
        # Flatten the stroke cross-section while preserving the draped centerline.
        profile = min(1, offset / (3 * pixel)) if terrain else 1
        for path, weight in paths:
            for clipped in clipped_paths(path, extent):
                if curve is None:
                    curve = bpy.data.curves.new(TITLES[kind], 'CURVE')
                    curve.dimensions = '3D'
                    curve.bevel_resolution = 0
                coords = [(x / extent, 1 - y / extent) for x, y in clipped]
                if terrain:
                    coords = list(terrain.drape(coords))
                spline = curve.splines.new('POLY')
                spline.points.add(len(coords) - 1)
                points = []
                for x, y in coords:
                    height = terrain.elevation(x, y) if terrain else 0
                    points.extend((x * span, y * span, height / profile, 1))
                spline.points.foreach_set('co', points)
                spline.points.foreach_set('radius', [weight] * len(coords))
        if curve is not None:
            obj = self.link(kind, curve, location, offset, pixel)
            obj['osm_draped'] = terrain is not None
            obj['osm_curve_profile'] = profile

    def add_labels(self, kind, layer, zoom, span, location, pixel, offset, terrain):
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
                    u, v = x / extent, 1 - y / extent
                    position = location[0] + u * span, location[1] + v * span
                    height = terrain.elevation(u, v) if terrain else 0
                    self.labels.append((priority, name[:90], kind, position, pixel, offset, height))

    def add_roads(self, layer, span, location, pixel, gap, terrain):
        extent, features = layer
        roads = {kind: [] for kind in ROAD_KINDS}
        for shape, paths, tags in features:
            if shape != 2:
                continue
            highway = str(tags.get('class', '')).removesuffix('_construction')
            if highway in ('path', 'pier'):
                weight = .50 if tags.get('subclass') in ('cycleway', 'pedestrian') else .38
                kind = 'paths'
            elif highway in ROAD_WEIGHTS:
                weight = ROAD_WEIGHTS[highway]
                kind = 'side_roads' if highway in ('minor', 'service', 'track') else 'roads'
            else:
                continue
            roads[kind].extend((path, weight) for path in paths)
        for kind, paths in roads.items():
            self.lines(kind, paths, extent, span, location, gap * 3, pixel, terrain)

    def add_tile(self, tile, layers):
        zoom = tile[0]
        x, y, size = osm_tiles.tile_bounds(tile)
        origin_x, origin_y, scale = self.reference
        location = (x - origin_x) / scale, (y - origin_y) / scale
        span = size / scale
        pixel = self.pixel or span / 384
        terrain = self.terrain.grid(tile, self.unit_scale)
        # Separation below a pixel at tile scale keeps the graphics legible when orbiting.
        gap = max(0.002 / self.unit_scale, span / 32_000)
        extent, features = layers.get('water', (1, ()))
        groups, inland = [], set()
        for shape, paths, tags in features:
            if shape != 3:
                continue
            for group in polygon_rings(paths, extent):
                if terrain and tags.get('class') not in ('ocean', 'dock'):
                    inland.add(len(groups))
                groups.append(group)
        land, water, lakes = split_ground(groups, terrain.divisions if terrain else 0, inland)
        self.mesh('land', land, span, location, 0, pixel, terrain)
        self.mesh('water', water, span, location, -2 * gap, pixel)
        self.mesh('water', lakes, span, location, -gap, pixel, terrain)
        if 'building' in layers:
            extent, features = layers['building']
            faces = (face for shape, paths, _ in features if shape == 3
                     for face in polygon_faces(paths, extent))
            self.mesh('buildings', faces, span, location, 2 * gap, pixel, terrain)
        for kind, name, minimum_zoom, depth in (('boundaries', 'boundary', 0, 1),
                                                ('water', 'waterway', 10, 1)):
            if zoom < minimum_zoom or name not in layers:
                continue
            extent, features = layers[name]
            paths = ((path, 1) for shape, paths, _ in features if shape == 2 for path in paths)
            self.lines(kind, paths, extent, span, location, gap * depth, pixel, terrain)
        if 'transportation' in layers:
            self.add_roads(layers['transportation'], span, location, pixel, gap, terrain)
        if 'place' in layers:
            self.add_labels('places', layers['place'], zoom, span, location, pixel, 4 * gap, terrain)
        if zoom >= 13 and 'transportation_name' in layers:
            self.add_labels('street_names', layers['transportation_name'], zoom, span, location, pixel, 4 * gap, terrain)

    def finish_labels(self):
        origin_x, origin_y, scale = self.reference
        west, south, east, north = self.bounds
        seen, occupied = set(), []
        def outside_view(label):
            x, y = label[3]
            return not (west <= origin_x + x * scale <= east and south <= origin_y + y * scale <= north)

        for _, name, kind, position, pixel, offset, height in sorted(self.labels, key=lambda label: (outside_view(label), label)):
            x, y = position
            key = kind, name
            size = pixel * (10 if kind == 'places' else 8)
            half_width = size * (len(name) * 0.28 + 0.5)
            left, right, bottom, top = x - half_width, x + half_width, y - size * 0.25, y + size
            if key in seen or any(left < r and right > l and bottom < t and top > b for l, r, b, t in occupied):
                continue
            occupied.append((left, right, bottom, top))
            seen.add(key)
            font = bpy.data.curves.new(name, 'FONT')
            font.align_x = 'CENTER'
            obj = self.link(kind, font, position, offset, pixel)
            obj.name = name
            obj['osm_label'] = name
            obj['osm_elevation'] = height
            if len(occupied) >= 100:
                break

    def step(self):
        item = next(self.pending, None)
        if item is None:
            self.finish_labels()
            return True
        self.add_tile(*item)
        self.built += 1
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
        self.paused = False

    def cancel(self):
        if self.future:
            self.future.cancel()
            self.future = None
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
        space = area.spaces.active
        if props.auto_clip and props.visible:
            set_map_clip(space, reference[2])
        if self.paused or not props.visible or space.region_3d.view_perspective == 'CAMERA':
            return
        bounds = view_bounds(area, region, reference, map_height(self.scene))
        pixel = display_unit(bounds, region, reference[2])
        tiles = osm_tiles.tile_plan(bounds, (region.width, region.height))
        folder = cache_folder(self.scene)
        desired = tiles, reference, folder, props.cache_only, props.cache_mb, props.terrain_enabled
        if desired != self.desired:
            self.desired, self.since = desired, time.monotonic()
            if self.build:
                purge(self.build.root)
                self.build = None
        if self.future and self.future.done():
            future, self.future = self.future, None
            if self.request == desired:
                data, elevation_paths = future.result()
                if not data:
                    raise ValueError('No cached tiles for this view. Turn off Cache only to load it.')
                self.build = MapBuild(data, reference, bounds, pixel, metres_per_unit(self.scene), elevation_paths)
                self.loaded = len(data)
                self.missing_elevation = sum(path is None for path in elevation_paths.values())
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
            else:
                props.status = f'Building zoom {tiles[0][0]} · {self.build.built}/{self.loaded} tiles…'
            return
        if desired == self.applied and props.map_collection:
            props.status = f'Zoom {tiles[0][0]} · {self.loaded}/{len(tiles)} tiles' + (
                ' · cache only' if props.cache_only else '')
            root = props.map_collection
            if self.missing_elevation:
                props.status += ' · elevation unavailable for this view'
            elif root.get('osm_terrain_tiles'):
                props.status += ' · terrain loaded'
            if not math.isclose(root.get('osm_display_unit', 0), pixel, rel_tol=0.12):
                root['osm_display_unit'] = pixel
                apply_style(self.scene, root)
        if not tiles:
            props.status = 'View is outside the Web Mercator map extent.'
        elif (self.future is None and desired not in (self.applied, self.failed)
              and time.monotonic() - self.since >= 0.25):
            self.request = desired
            self.future = _executor.submit(osm_terrain.fetch_view, folder, tiles, props.cache_only,
                                           props.cache_mb, props.terrain_enabled)
            layers = 'roads and elevation' if props.terrain_enabled else 'map tiles'
            props.status = f'Loading {layers} · zoom {tiles[0][0]} · {len(tiles)} tiles…'


def tick_maps():
    for key, session in tuple(_sessions.items()):
        try:
            session.tick()
        except ReferenceError:
            session.cancel()
            _sessions.pop(key, None)
        except Exception as exc:
            session.cancel()
            session.failed = session.desired
            session.scene.cmems_osm.status = str(exc)
    for window in bpy.context.window_manager.windows:
        if window.screen and window.screen.get(WINDOW):
            for area in window.screen.areas:
                area.tag_redraw()
    return 0.18 if _sessions else None


def start_map_timer():
    if not bpy.app.timers.is_registered(tick_maps):
        bpy.app.timers.register(tick_maps, first_interval=0.25)


def start_map_session(scene, window, area):
    global _executor
    key = scene.as_pointer()
    previous = _sessions.get(key)
    if previous:
        previous.cancel()
    session = MapSession(scene, window, area)
    session.paused = not scene.cmems_osm.follow_view
    _sessions[key] = session
    if not session.paused and _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='osm-world')
    start_map_timer()


def restore_saved_maps():
    for scene in bpy.data.scenes:
        props = scene.cmems_osm
        if 'label_opacity' in props:
            for field in ('places_opacity', 'street_names_opacity'):
                if not props.is_property_set(field):
                    props[field] = props['label_opacity']
        for kind in ('side_roads', 'paths'):
            for suffix, old_field in (('_visible', 'roads_visible'), ('_opacity', 'road_opacity')):
                field = kind + suffix
                if props.is_property_set(old_field) and not props.is_property_set(field):
                    props[field] = getattr(props, old_field)
    if bpy.app.background or not bpy.context.window_manager:
        return
    for window in bpy.context.window_manager.windows:
        scene = window.scene
        props = scene.cmems_osm
        key = scene.as_pointer()
        if key in _sessions or not props.map_collection:
            continue
        with bpy.context.temp_override(window=window):
            try:
                viewport_window, area, _ = viewport(bpy.context)
            except ValueError:
                continue
        start_map_session(scene, viewport_window, area)
        props.status = 'Updating saved map…' if props.follow_view else 'Paused · showing saved map'


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
    bl_description = 'Load OSM tiles and terrain for the map viewport'
    destination: StringProperty(default='')

    def execute(self, context):
        scene = context.scene
        key = scene.as_pointer()
        if self.destination == 'STOP':
            scene.cmems_osm.follow_view = False
            session = _sessions.get(key)
            if session:
                session.cancel()
                session.paused = True
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
            props = scene.cmems_osm
            legacy_height = props.get('elevation')
            if not props.is_property_set('relief_m') and legacy_height not in (None, -0.25):
                props.relief_m = legacy_height * metres_per_unit(scene)
            if self.destination in ('WORLD', 'DENMARK') or not has_origin:
                view.view_perspective = 'ORTHO'
                view.view_rotation = (1, 0, 0, 0)
                if self.destination == 'WORLD':
                    x, y, span = 0, 0, osm_tiles.WORLD
                else:
                    x, y = mercator(10.3, 56.1)
                    span = 850_000
                view.view_location = ((x - origin_x) / scale, (y - origin_y) / scale, 0)
                aspect = max(region.width, region.height) / max(1, min(region.width, region.height))
                view.view_distance = span / scale * (space.lens / 50) * max(1, 0.76 * aspect)
            if self.destination == 'DETAIL':
                if view.view_perspective == 'CAMERA':
                    view.view_perspective = 'PERSP'
                view.view_location = scene.cursor.location
                view.view_distance = 1400 / scale * space.lens / 50
            if props.auto_clip:
                set_map_clip(space, scale)
            space.overlay.show_floor = space.overlay.show_ortho_grid = False
            space.overlay.show_axis_x = space.overlay.show_axis_y = False
            if space.shading.type in ('SOLID', 'WIREFRAME'):
                space.shading.type = 'MATERIAL'
            area.tag_redraw()
            props.follow_view = True
            start_map_session(scene, window, area)
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
    layout.operator('cmems.osm_view', text='Street detail at 3D cursor', icon='CURSOR').destination = 'DETAIL'
    layout.prop(props, 'visible')
    wrapped(layout, props.status)
    root = props.map_collection
    if root:
        zoom = root.get('osm_zoom')
        if zoom is not None:
            layout.label(text=f'Displayed map · zoom {zoom}')
        if root.get('osm_ground_schema') != GROUND_SCHEMA:
            wrapped(layout, 'Showing an older saved map. Load current view to refresh it.', icon='INFO')
        categories = {category.get('osm_category') for category in root.children if category.objects}
        road_layers = [TITLES[kind] for kind in ROAD_KINDS if kind in categories]
        if road_layers:
            wrapped(layout, 'Loaded road layers · ' + ', '.join(road_layers))
            if not any(props.visible and getattr(props, kind + '_visible')
                       and getattr(props, OPACITY_FIELDS[kind]) > 0 for kind in ROAD_KINDS if kind in categories):
                layout.label(text='Loaded roads are hidden')
        if 'side_roads' not in categories or 'paths' not in categories:
            wrapped(layout, 'Zoom in for side streets and paths.', icon='INFO')
    relief = layout.box()
    relief.label(text='Terrain and relief', icon='MESH_GRID')
    relief.prop(props, 'terrain_enabled')
    if props.terrain_enabled:
        terrain_zoom = root.get('osm_terrain_zoom') if root else None
        if terrain_zoom is not None:
            relief.label(text=f'Elevation mesh loaded · zoom {terrain_zoom}')
        elif root and root.get('osm_terrain_tiles'):
            relief.label(text='Elevation mesh loaded')
        else:
            relief.label(text='No elevation mesh on the displayed map')
    relief.prop(props, 'terrain_exaggeration')
    relief.prop(props, 'hillshade', slider=True)
    relief.prop(props, 'relief_m')
    relief.prop(props, 'water_underlay')
    relief.prop(props, 'auto_clip')
    if props.terrain_enabled:
        relief.operator('wm.url_open', text='Terrain data credits', icon='URL').url = osm_terrain.CREDITS_URL
    style = layout.box()
    style.label(text='Map style', icon='COLOR')
    for kind in CATEGORIES:
        row = style.row(align=True)
        row.use_property_split = False
        row.prop(props, kind + '_visible', text='')
        row.prop(props, kind + '_color')
        row.prop(props, OPACITY_FIELDS[kind], slider=True)
    style.prop(props, 'road_width')
    style.prop(props, 'place_size')
    style.prop(props, 'street_size')
    style.prop(props, 'uppercase')
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
    credit = osm_tiles.ATTRIBUTION
    if props.terrain_enabled and props.map_collection.get('osm_terrain_tiles'):
        credit += ' · ' + osm_terrain.ATTRIBUTION
    blf.draw(font, credit)
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
