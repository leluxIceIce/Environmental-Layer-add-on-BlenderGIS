# Changelog

The initial Git history was reconstructed from saved installer ZIPs. Tags through 0.5.3 identify imported release snapshots.

## 0.7.0

- Added Mapzen elevation meshes, terrain-following map features, vertical exaggeration and hillshade.
- Loaded street detail around the viewport focus in tilted views and added a street-detail jump to the 3D cursor.
- Separated main roads, side roads, and pedestrian paths and sidewalks. Added clearer independent controls for city and road labels.
- Reduced the default label scale and narrowed minor roads and paths.

## 0.6.2

- Added independent opacity and visibility controls for all seven OSM map categories.
- Preserved saved road opacity and used the previous shared label opacity as the starting value for both label categories.

## 0.6.1

- Rebuilt OSM land and water as non-overlapping triangles along the coastline.
- Added a 10 mm default land height and an independent water underlay for environmental overlays.
- Adjusted viewport depth precision in perspective, including while the map is paused.
- Preserved the current angle when reloading the map and retained the last tiles when orbiting toward the horizon.

## 0.6.0

- Added a standalone OSM World Map window and GIS sidebar panel with World and Denmark views.
- Loaded visible OpenFreeMap vector tiles as Blender geometry, with map detail following the viewport zoom.
- Added a black default map style, per-category colours and visibility, and independent road and label controls.
- Shared the Web Mercator origin and scale with the existing environmental layers.
- Added a configurable disk cache and cache-only navigation.

## 0.5.5

- Separated the animated preview, static map creation and ZIP export into distinct timeline controls.
- Added one static object per chosen date in a dedicated Outliner collection.
- Saved automatic ZIPs directly in the selected output folder and displayed the exact file location for manual exports.
- Enabled splitting and exporting older linked timelines from their existing PNGs.

## 0.5.4

- Packed each timeline map as a dated material on one Blender object. The scene frame selects the map through Geometry Nodes.
- Added a complete timeline ZIP to every export and enabled rebuilding it from the packed object.
- Added in-place conversion for timelines created with the earlier linked image sequence.

## 0.5.3

- Reloaded the add-on's cached modules when enabling an updated build. This prevents an older greyscale filter from running alongside the updated interface.
- Logged Blender-side processing errors alongside the download logs.
- Verified the in-session update, a single-date salinity surface, and a 24-frame salinity sequence in Blender 4.5.5 using cached Copernicus tiles. The sequence remained linked after saving and reopening.

## 0.5.2

- Remapped custom palettes using the full RGB legend from Copernicus. Grey and coloured legends can both drive a custom ramp while retaining the source transparency.

## 0.5.1

- Moved the Environmental Layer controls into a separate Blender window.
- Made the timeline start and end editable next to the first and last planned source dates.
- Accepted the slightly tinted RGB values in the Copernicus greyscale palette.
- Credited Felix Berger as maintainer.

## 0.5.0

- Added timeline downloads with one animated Blender object, numbered PNG frames, an SVG legend and a JSON manifest.
- Added White to Red, Sediment, Cividis, Ice and Magma palettes, plus a daily gap-filled coastal product preset.

## 0.4.3

- Added bounded native chlorophyll raster preparation from Copernicus Marine ARCO Zarr and included the values in the web export.

## 0.4.2

- Recovered the matching managed material during web export when the selected terrain's current material lacked the requested environmental overlay.

## 0.4.1

- Added GLB and JSON export with baked overlay colours, alpha and geographic metadata.

## 0.4.0

- Made independent terrain copies the default and checked the advertised product footprint before downloading.

## 0.3.1

- Recovered environmental materials after material edits and scene switches.

## 0.3.0

- Moved catalogue and imagery requests into a cancellable background worker.
- Added standalone georeferenced environmental surfaces.

## 0.1.0

- Added public Copernicus Marine WMTS overlays for BlenderGIS terrain.
