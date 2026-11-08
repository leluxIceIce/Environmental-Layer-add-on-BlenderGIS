# Changelog

The initial Git history was reconstructed from saved installer ZIPs. Each version tag identifies an imported release snapshot.

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
