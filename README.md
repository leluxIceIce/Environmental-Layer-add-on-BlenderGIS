# BlenderGIS Environmental Layer — 0.4.0

Adds **Environmental Layer** to the existing **GIS → Web geodata** menu and an Environmental Layer submenu inside **GIS**. Uses BlenderGIS's scene coordinates and existing map objects. The optional sidebar is a second access point.

## Update / activate

Install `BlenderGIS_Marine_Layers_0.4.0.zip` using **Preferences → Add-ons → ▼ → Install from Disk**. Keep **BlenderGIS** and **BlenderGIS Marine Layers** enabled. Save your project and restart Blender after replacing the old version, so the old modal loader is no longer in memory.

If the installed files were already updated for you, only save and restart. The version displayed in Preferences should be **0.4.0**.

## Existing map and terrain

1. Open **GIS → Web geodata → Environmental Layer**.
2. Choose **Existing BlenderGIS terrain** and select **Target terrain**. The saved Nykøbing Mors project uses the mesh named **srtm**; its satellite map is a separate BlenderGIS image named **GOOGLE_SAT_WM**. Do not choose the unrelated `AAABIGBOY` mesh.
3. Leave **Use BlenderGIS basemap when material is missing** enabled to map the existing Google/ESRI/OSM image onto a bare terrain automatically. An existing textured material is preserved and copied instead. Without an available basemap, a neutral material is created.
4. Choose **North Sea coast — 100 m monthly → Load available layers**.
5. Choose chlorophyll, an available period (September 2026 was verified), a palette and a range. Chlorophyll starts at **0–40 mg m−3**.
6. Choose **Create new copy** and click **Load as new copy**. For an intentional overwrite, choose **Replace target layer**. Reopen the GIS menu if the popup closed while the request was running. Use **Show Material Preview** to see the material.

**Create new copy is now the default**, as requested. Each load duplicates the target into the Environmental Layers collection, with private geometry/material data and a name containing variable and date. The source geometry/materials are retained. By default the source is hidden in the current view and render to avoid coincident surfaces; it stays in the Outliner. Disable Hide source after creating copy to keep it visible. Choose **Replace target layer** explicitly to update the same object instead. Additional UV maps and private material copies are used; original material slots can be restored with **Remove Marine layer**.

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

Marine imagery is packed into the saved blend file. Underlying satellite textures remain the existing BlenderGIS image assets.

## Preferences and renderer

**Preferences → Add-ons → BlenderGIS Marine Layers** contains the current access mode, **Test public connection**, cache location, request timeout, parallel downloads, catalogue cache lifetime and map defaults.

The current backend uses public Copernicus Marine WMTS map images; no API key/login is used. Native numeric data downloads through the Marine Toolbox require Copernicus Marine account credentials and are outside this image-layer release. The OpenTopography key shown in BlenderGIS preferences belongs to that elevation provider, not this Marine image service. GMRT at marine-geo.org and Copernicus Marine are separate sources.

**Render & system** in the environmental UI exposes Blender's actual scene renderer. Cycles samples, viewport samples and device are shown for Cycles. These are scene settings; loading data does not automatically switch the renderer.

## Copy versions and coverage in 0.4.0

**Create new copy** is the default for existing targets. Copies retain terrain height/modifiers and independent materials, UVs and source metadata. They are named by base object, variable and timestamp and grouped in **Environmental Layers**. The source is retained; optional hiding prevents coincident faces from flickering. A failed/no-data request does not create an empty version or hide the source.

**Replace target layer** is an explicit alternative. The button label changes with the selected policy, so replacement is intentional.

The advertised dataset bounding box is checked before a request. The Baltic daily physics product starts near **9.0416°E** and therefore excludes the Nykøbing Mors site. The UI now reports this clearly. This check does not imply that all points inside a product's broad bounding box have valid nearshore/model data: the downloaded image is also checked for valid alpha inside the requested footprint. The verified North Sea high-resolution coastal-water product covers the local optical fields.

Validated copy operations preserve source object/data/material references, coordinates, modifiers and independent appearance; second loads create a second retained version. Explicit replace preserves object count. Failure rollback and Baltic bounds rejection are checked in Blender 4.5.5.

## Material recovery (retained from 0.3.1)

Deleting a layer material no longer blocks loading. The next update creates the material from the downloaded field again; no satellite image or user-supplied picture is required. Added, replaced and reordered material slots are reconciled by tracked material identity. Removal restores the current user baseline and only removes slots created by the layer. Legacy standalone files with zero original materials are supported. Standalone NoData transparency is recreated with the material.

The loaded add-on version is visible at the top of the environmental panel. After an update, **save your project and completely quit Blender with Command-Q before reopening it**. Opening another blend file alone does not reload Python add-ons.

## Loader / crash repair

Version 0.1.1 used a retained modal operator marked REGISTER/UNDO for both catalogue and imagery requests. The supplied crash report ends in Blender's native operator-completion/registration path. The report alone does not identify every internal cause.

Version 0.3.0 and later replace this path with an isolated download process polled by an application timer. The request operator returns immediately. Only a separate, synchronous undoable operator applies the finished material/geometry on the main thread. No modal download operator is kept alive, registered for redo, or completed later. **Cancel request** stops the worker. File load and add-on shutdown cancel pending requests.

The catalogue and connection test require **no mesh at all**. Applying to an existing terrain uses an explicit target, can recognize an `srtm`/DEM/export mesh when a different object is active, and no longer requires a satellite material.

## Validation

Blender **4.5.5 LTS**, BlenderGIS **2.2.10**, macOS:

- Actual GUI event loop: `Load available layers` without any mesh; catalogue completed; standalone generated; repeated load applied to the same object; existing GIS menu drawn; no crash during the test.
- Empty ungeoreferenced scene: exactly one surface created, correct map origin, packed Marine image, flat geometry and linked NoData alpha.
- Bare terrain with another object active: explicit/recognized target selected, material created, no new map object, unchanged vertex coordinates; removal restores zero original materials.
- Saved **SiteMAP_satellite.blend** read in a separate process: the actual `srtm` mesh has no materials. The field was applied to it while preserving its vertices and elevation modifiers and reusing `GOOGLE_SAT_WM` as the base texture. Only a separate validation copy was saved.
- Registered Preferences, real public connection, monthly selection/steps, source metadata export, per-object appearance and Cycles/Eevee UI properties checked.

The original native crash was not reproduced in the old-version test; the new loader and exact catalogue action were verified through the real GUI lifecycle. The user's open scene and original saved site file were not changed by the validation runs.

Validation copies, if supplied, are ready-to-open examples, not replacements for the original project.

## Source and attribution

`marine_layers/__init__.py`: menu integration, preferences, scene/geometry/material handling and request lifecycle.

`marine_layers/core.py`: public product capabilities, time/depth metadata, bounded WMTS tile requests and cache. No Blender imports or third-party packages in the worker.

Contains modified Copernicus Marine Service information. Preserve product, variable and period when publishing a figure.

- WMTS API: https://help.marine.copernicus.eu/en/articles/6478168-how-to-use-the-copernicus-marine-web-map-tile-service-wmts
- Coastal-water product: https://data.marine.copernicus.eu/product/OCEANCOLOUR_NWS_BGC_HR_L4_NRT_009_209/description
- BlenderGIS: https://github.com/domlysz/BlenderGIS

GPL-3.0-or-later. BlenderGIS's own source files are not patched; its menus are extended through Blender's menu hooks.
