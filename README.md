# BlenderGIS Marine Layers — first working companion

Add Copernicus Marine map imagery to an existing BlenderGIS terrain material.
The installed BlenderGIS add-on supplies the geographic reference; this companion adds its own **Marine Layers** panel under **GIS**.

## Install and try

1. In Blender: **Edit → Preferences → Add-ons → Install from Disk…**. Select `BlenderGIS_Marine_Layers_0.1.0.zip`, then enable **BlenderGIS Marine Layers**. Keep BlenderGIS enabled too.
2. Open your existing map. Select the **exported terrain mesh** in **Object Mode**.
3. In the 3D View, press **N → GIS → Marine Layers**.
4. Choose a product, e.g. **Baltic Sea — biology**, and click **Load available layers**.
5. Choose **Chlorophyll a Concentration · Daily**, inspect the date, and click **Load / update on selected terrain**.
6. Use **Material Preview** or **Rendered** view. Adjust **Marine opacity**, **Blend**, **Satellite saturation** and **Satellite contrast**. Colours, value range, date or depth changes require **Load / update** again.
7. Save the `.blend`: the resulting Marine image is packed into it. **Remove Marine layer** restores the original material slots.

For a first local test near Grenaa: Baltic biology → daily chlorophyll → 2026-10-27T00:00:00Z → surface → Neon green. On a small coastal map the field may look almost uniform because the model grid is much coarser than the satellite picture. Try a larger sea area to see broader gradients.

No Copernicus login, API key, Python installation or extra packages are needed for these public WMTS image layers.

## What it does

- Product presets for Baltic biology/physics, North Sea biology/physics and global biology/physics. Other public WMTS products can be loaded by product ID.
- Reads current variables and dataset tags from each product's live capabilities. Avoids downloading the entire global catalogue.
- Copies the advertised colour range and exact available date/time into the controls. Enter an ISO date or UTC timestamp for another snapshot. Out-of-range dates fail visibly.
- Converts mesh vertices using BlenderGIS's scene origin, projection and scale. Accounts for object location, rotation and scale. Supports Web Mercator, WGS84 and UTM scenes.
- Fetches a bounded group of 256-pixel map tiles, at most 36 per request. Performs network work in a separate Python process. Esc cancels.
- Uses a dedicated geographic UV map over the actual mesh, so the layer follows terrain relief. The original satellite UVs remain intact.
- Keeps the original materials as references; stages new images, UVs and material copies before applying them to the selected object. Shared source materials/meshes remain untouched.
- Supports server palettes plus artistic neon-green/cyan ramps, live opacity, Mix/Add/Multiply/Screen, satellite saturation and contrast.
- Packs the image and records the product, variable, UTC timestamp, actual model depth, value range, units and rendering style in the object and image metadata.
- Cache keys include the exact dataset, time, depth, style, matrix and tile coordinates.

## Scope of version 0.1.0

This is a **rendered map image overlay**. It does not import native numeric arrays, create particle/flow simulations, animate time series or alter the DEM. One Marine overlay is active per terrain object; loading again updates it. Multiple terrain objects can have independent overlays. Appearance controls act on the currently selected object.

The texture-size target affects the rendered image, not the scientific resolution of the product. A coarse source field remains coarse. NoData transparency is retained. A fixed-depth map shows a field at that depth; projecting it onto terrain does not turn it into a seabed-following measurement. The sea surface in an aerial image is not itself seabed bathymetry.

Depth is entered as positive metres below the surface. The nearest advertised depth is used, and its actual value is shown after loading. Data are interpreted in UTC. Regional product coverage and missing nearshore cells may leave parts of your map transparent. The first version shows linear colour scales.

Existing **Principled BSDF** terrain materials are supported, including BlenderGIS's normal satellite material. Material-slot structure should remain unchanged while the overlay is active. Shaders inside custom node groups and other projections are outside this first version. The overlay changes Base Color; existing lights, roughness, relief, camera and render settings still determine the final appearance.

Removing a layer restores materials and removes its extra UV map. The mesh remains a single-user copy. Blender Undo is also available. Images/materials from previous updates may remain as unused Blender datablocks; use Blender's normal unused-data cleanup when appropriate. Original `.blend` files are never overwritten by the add-on.

## Verification

Validated with **Blender 4.5.5 LTS** and the installed **BlenderGIS 2.2.10** source on macOS:

- Public Baltic product capabilities: 40 layers discovered.
- Public daily chlorophyll image around Grenaa for 27 October 2026; 9 tiles fetched, with actual depth −0.5016462206840515 m.
- Registered in a real background Blender process with BlenderGIS enabled.
- Applied to a raised test mesh with rotation, translation, a non-unit scene scale and a shared source material/mesh.
- Verified geographic UV coordinates, original satellite UV preservation, live opacity, replacement rather than node stacking, packed image persistence and original material restoration.
- Actual download operators and separate worker exercised; save/reopen and cancellation checked.

The test terrain is a synthetic fixture, not a measured DEM. The user's currently open Blender scene was not modified. Interactive placement and appearance in that specific scene still need the short try-out above.

## Source and data acknowledgement

`marine_layers/__init__.py`: Blender panel, materials, geographic mapping and operators.

`marine_layers/core.py`: public WMTS catalogue access, dimensions, tile planning, cache and isolated download worker. Standard library only.

Contains modified Copernicus Marine Service information. When sharing a figure, preserve the actual product/variable/date and follow the selected product's attribution guidance.

Official API reference: https://help.marine.copernicus.eu/en/articles/6478168-how-to-use-the-copernicus-marine-web-map-tile-service-wmts

BlenderGIS: https://github.com/domlysz/BlenderGIS

Source code licence: GPL-3.0-or-later. No BlenderGIS source files are bundled or patched.
