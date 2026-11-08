# BlenderGIS Environmental Layer — 0.5.4

Maintained by Felix Berger.

Adds **Environmental Layer** to the existing **GIS → Web geodata** menu and an Environmental Layer submenu inside **GIS**. Both open a separate Blender window with the controls for the same scene. You can move and resize it and keep it open while working in the viewport. The optional sidebar is a second access point.

## Update / activate

Download [BlenderGIS Marine Layers 0.5.4](downloads/BlenderGIS_Marine_Layers_0.5.4.zip) and install the ZIP using **Preferences → Add-ons → ▼ → Install from Disk**. Keep **BlenderGIS** and **BlenderGIS Marine Layers** enabled. To activate the update in the current session, disable and re-enable **BlenderGIS Marine Layers** after installing. You can also save your project and restart Blender.

The Environmental Layer panel should display **0.5.4** once the update is active. See the [changelog](CHANGELOG.md) for release details.

## Download a timeline

1. Open **GIS → Environmental Layer → Download timeline…** and choose a target terrain or a new environmental surface.
2. For daily chlorophyll at Nykøbing Mors, choose **North Sea coast — 100 m daily gap-filled → Load available layers**, then select the **Daily** chlorophyll dataset. The **100 m daily** preset uses the observation mosaics, which can have missing coverage.
3. In **Time & depth**, set **Start date (UTC)**, **End date (exclusive)** and **Frames**. **Use recent dates** fills an editable range from the latest periods. The panel shows the first and last dates it will actually download. **Dataset coverage** shows the full available range separately.
4. Set the palette, value range and texture size. In **Timeline output**, choose the first Blender frame and output folder, then click **Download to new terrain copy** or **Download to new flat surface**. The default folder is `Environmental Timelines` beside the saved blend file. An unsaved blend can use an explicit absolute output folder.
5. Scrub the resulting Blender frame range in **Material Preview**. Expand the object in the Outliner or open its material slots to see the dated frame materials. The selected object's panel shows the current source time and provides **Open export folder**.

Use **1.5.2026 → 1.6.2026** with **31 frames** for every day in May, or with **30 frames** for 30 source dates spread across May. **1.5.2026 → 31.5.2026** with **30 frames** gives 1–30 May. The date planner uses the dataset's full advertised timeline. Each selected period maps to one Blender frame and may be a daily mosaic, a monthly mean or a model time, depending on the dataset.

Each run creates one object with packed frame images. A flat surface has one material slot per date. A terrain with multiple original materials keeps its face assignments, with one set of slots per date. Blender's scene frame selects the active date, and the packed images travel with the saved blend file.

The export folder contains numbered transparent PNGs, `legend.svg`, `timeline.json`, and `timeline.zip` with the complete package. The manifest records the UTC date and coverage status per frame, the dataset, value range, units, requested footprint and tile-aligned map bounds. **Export timeline ZIP** can rebuild the archive from the packed object. For a timeline created in an earlier version, select it and click **Pack existing timeline frames** to use its existing exported PNGs.

A terrain target is copied once. Its source can be hidden through **Hide source after creating copy**.

A selected date with a fully masked footprint keeps its frame as transparent NoData. If every frame is masked, the batch stops. A failed download reports the affected date, and retrying can reuse cached tiles. Cancel before the object is created to discard the staged output. Exported files are kept when you undo the Blender object.

**White to Red**, **Sediment · Turbid**, **Cividis**, **Ice** and **Magma** are available for both single layers and timelines. Timeline colours use one range throughout. White to Red and Sediment remap the ordered RGB stops in the Copernicus legend, so both grey and coloured source ramps can be used. The Sediment ramp samples [cmocean Turbid](https://matplotlib.org/cmocean/#turbid), by Kristen M. Thyng, under the MIT license in `LICENSE.cmocean`.

## Existing map and terrain

1. Open **GIS → Web geodata → Environmental Layer**.
2. Choose **Existing BlenderGIS terrain** and select the terrain mesh under **Target terrain**.
3. Leave **Use BlenderGIS basemap when material is missing** enabled to map the existing Google/ESRI/OSM image onto a bare terrain automatically. An existing textured material is preserved and copied instead. Without an available basemap, a neutral material is created.
4. Choose **North Sea coast — 100 m monthly → Load available layers**.
5. Choose chlorophyll, an available period (September 2026 was verified), a palette and a range. Chlorophyll starts at **0–40 mg m−3**.
6. Choose **Create new copy** and click **Load as new copy**. For an intentional overwrite, choose **Replace target layer**. Use **Show Material Preview** to see the material.

**Create new copy** is the default. Each load duplicates the target into the Environmental Layers collection with private geometry and material data and a name containing the variable and date. By default, the source stays in the Outliner and is hidden in the current view and render to avoid coincident surfaces. Clear **Hide source after creating copy** to keep it visible. Choose **Replace target layer** to update the same object. Use **Remove Marine layer** to restore the original material slots.

## Standalone — no mesh, image or material required

1. **GIS → Environmental Layer → Create new environmental surface…**.
2. Choose **New environmental surface**.
3. Area: choose **Custom geographic bounds** (defaults cover Nykøbing Mors) or **Selected BlenderGIS map** to reuse a selected mesh/image's footprint.
4. Select product, variable, period and palette, then **Create environmental surface**.

The add-on creates a georeferenced grid, material, colour and NoData transparency. An empty scene is given a Web Mercator origin at the chosen area. Existing valid BlenderGIS coordinates are reused. The default surface height is zero in scene units.

**This is a flat environmental surface. Chlorophyll, turbidity and suspended matter are not elevation and are never used to invent terrain relief.** For real 3D terrain/seabed shape, apply the environmental material to an existing DEM/GMRT terrain. Transparent NoData follows the source field's mask, not an exact aerial coastline.

After creating a standalone surface, the destination switches to **Existing terrain**. Later loads follow the chosen copy/replace policy; the default creates another version. Choose **New environmental surface** again only when another object is intended.

## Data and timestamps

- Explicit available-period dropdown, previous/next steps, and a manual UTC timestamp field for older times.
- Displays cadence (monthly/daily/hourly), advertised availability, source variable/units, actual model depth when applicable, and the actual loaded period.
- Monthly time labels describe a monthly mean/composite, not an instantaneous measurement on the first day.
- **Source details** shows product, dataset, variable, scene/map CRS, source metadata update and the separate local load time.
- **Export layer metadata** writes the actual layer and current appearance settings to JSON.
- Colour palettes, range, opacity, blend mode, satellite saturation/contrast, and nearest/linear/cubic image filtering.
- The texture-size target affects display sampling only. Source scientific resolution remains unchanged.

Single-date overlays and timeline frames are packed into the saved blend file. Satellite basemaps use the existing BlenderGIS image assets.

## Preferences and renderer

**Preferences → Add-ons → BlenderGIS Marine Layers** contains the current access mode, **Test public connection**, cache location, request timeout, parallel downloads, catalogue cache lifetime and map defaults.

Map imagery uses public Copernicus Marine WMTS; no API key/login is used. Version 0.4.3 also supports bounded native numerical CHL export from the selected dataset’s public ARCO Zarr source, without account credentials or additional Python packages. Use **Prepare numeric CHL raster** before exporting with numeric values enabled. Unsupported variables, access requirements or encodings are reported explicitly; they are never replaced by inferred colour values. Broader numerical access through the Copernicus Marine Toolbox may require a Copernicus Marine account. The OpenTopography key shown in BlenderGIS preferences belongs to that elevation provider, not these Marine services. GMRT at marine-geo.org and Copernicus Marine are separate sources.

**Render & system** in the environmental UI exposes Blender's actual scene renderer. Cycles samples, viewport samples and device are shown for Cycles. These are scene settings; loading data does not automatically switch the renderer.

## Copy versions and coverage in 0.4.0

**Create new copy** is the default for existing targets. Copies retain terrain height/modifiers and independent materials, UVs and source metadata. They are named by base object, variable and timestamp and grouped in **Environmental Layers**. The source is retained; optional hiding prevents coincident faces from flickering. A failed/no-data request does not create an empty version or hide the source.

**Replace target layer** is an explicit alternative. The button label changes with the selected policy, so replacement is intentional.

The advertised dataset bounding box is checked before a request. The Baltic daily physics product starts near **9.0416°E** and therefore excludes the Nykøbing Mors site. The UI now reports this clearly. This check does not imply that all points inside a product's broad bounding box have valid nearshore/model data: the downloaded image is also checked for valid alpha inside the requested footprint. The verified North Sea high-resolution coastal-water product covers the local optical fields.

Validated copy operations preserve source object/data/material references, coordinates, modifiers and independent appearance; second loads create a second retained version. Explicit replace preserves object count. Failure rollback and Baltic bounds rejection are checked in Blender 4.5.5.

## Material recovery (retained from 0.3.1)

Deleting a layer material no longer blocks loading. The next update creates the material from the downloaded field again; no satellite image or user-supplied picture is required. Added, replaced and reordered material slots are reconciled by tracked material identity. Removal restores the current user baseline and only removes slots created by the layer. Legacy standalone files with zero original materials are supported. Standalone NoData transparency is recreated with the material.

The loaded add-on version is visible at the top of the Environmental Layer panel. Re-enable the add-on after installing an update to reload its Python modules.

## Loader / crash repair

Version 0.1.1 used a retained modal operator marked REGISTER/UNDO for both catalogue and imagery requests. The supplied crash report ends in Blender's native operator-completion/registration path. The report alone does not identify every internal cause.

Version 0.3.0 and later replace this path with an isolated download process polled by an application timer. The request operator returns immediately. Only a separate, synchronous undoable operator applies the finished material/geometry on the main thread. No modal download operator is kept alive, registered for redo, or completed later. **Cancel request** stops the worker. File load and add-on shutdown cancel pending requests.

The catalogue and connection test require **no mesh at all**. Applying to an existing terrain uses an explicit target, can recognize an `srtm`/DEM/export mesh when a different object is active, and no longer requires a satellite material.

## Validation

Version **0.5.4** was checked in a background session of Blender **4.5.5 LTS**. The salinity batch produced 24 packed materials on one mesh and 24 distinct frame assignments. After the export folder was moved, the saved scene reopened and rebuilt a ZIP with byte-identical PNGs. A linked timeline from 0.5.3 was also converted in place.

Version **0.5.3** was checked in a background session of Blender **4.5.5 LTS** on macOS. The test reproduced the older colour error, updated the add-on in the same process, and loaded both a single-date salinity surface and a 24-frame White to Red sequence from cached WMTS tiles. The test scene was saved and reopened with its image sequence linked.

Earlier validation used Blender **4.5.5 LTS** with BlenderGIS **2.2.10** on macOS.

- Actual GUI event loop: `Load available layers` without any mesh; catalogue completed; standalone generated; repeated load applied to the same object; existing GIS menu drawn; no crash during the test.
- Empty ungeoreferenced scene: exactly one surface created, correct map origin, packed Marine image, flat geometry and linked NoData alpha.
- Bare terrain with another object active: explicit/recognized target selected, material created, no new map object, unchanged vertex coordinates; removal restores zero original materials.
- A separate georeferenced test scene was used to apply a field to a bare terrain mesh while preserving its vertices and elevation modifiers and reusing the BlenderGIS basemap as the base texture.
- Registered Preferences, real public connection, monthly selection/steps, source metadata export, per-object appearance and Cycles/Eevee UI properties checked.

The loader and catalogue action were also checked through the Blender GUI event loop.

## Source and attribution

`marine_layers/__init__.py`: menu integration, preferences, scene/geometry/material handling and request lifecycle.

`marine_layers/core.py`: public product capabilities, time/depth metadata, bounded WMTS tile requests and cache. No Blender imports or third-party packages in the worker.

`marine_layers/timeline.py` handles colour remapping and writes the PNG sequence and SVG legend. `marine_layers/science.py` prepares native chlorophyll rasters. `marine_layers/web_export.py` exports the GLB and geographic JSON sidecar.

Contains modified Copernicus Marine Service information. Preserve product, variable and period when publishing a figure.

- WMTS API: https://help.marine.copernicus.eu/en/articles/6478168-how-to-use-the-copernicus-marine-web-map-tile-service-wmts
- Coastal-water product: https://data.marine.copernicus.eu/product/OCEANCOLOUR_NWS_BGC_HR_L4_NRT_009_209/description
- BlenderGIS: https://github.com/domlysz/BlenderGIS

GPL-3.0-or-later. BlenderGIS's own source files are not patched; its menus are extended through Blender's menu hooks.


## Native CHL science export (0.4.3)

On the selected loaded CHL terrain, choose **GIS → Environmental Layer → Prepare numeric CHL raster**. This starts a cancellable background worker. It reads the exact displayed dataset/month from the official native-resolution ARCO Zarr source and caches only the intersecting native chunks; it does not infer values from colours or sample one HTTP request per cell.

When the status says the numeric raster is ready, choose **Export web package · GLB + JSON** with **Include native CHL values** enabled. The JSON embeds `science_raster` with native longitude/latitude coordinates, row-major CHL values (missing cells are `null`), exact time, units, source, coverage, and the matching WMTS legend. Other environmental variables can still be exported as visual packages; native numeric support currently covers CHL only. A missing/mismatched numeric cache stops the numeric export before baking or replacing existing files.

The worker requires no Copernicus credentials or added Python packages for supported public ARCO Blosc/LZ4 datasets. If a provider changes access or encoding, preparation reports that explicitly instead of exporting a substitute. Imported satellite imagery, source materials and stored scene geometry remain unchanged. Fresh export georeferencing always uses the current BlenderGIS scene origin; flat lifted map surfaces record their display elevation separately.
