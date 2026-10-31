# Flood scene (3D viewer)

The 3D viewer can show a simple flood scenario: a water surface at a given height over the whole
dataset, and every building coloured by whether the water reaches it. The chatbot triggers it from
an ordinary `run_query` result, so it works with any model, including local Ollama models, and needs
no extra tool.

It is a **bathtub model** for a quick first estimate: the water surface is flat, there is no flow,
no connectivity check and no flood defences. It is not a replacement for hydraulic models.

## Requirements

- Fullstack Docker setup with the viewer (`ENABLE_VIZ=true`, see [option3-docker-fullstack.md](option3-docker-fullstack.md)).
- Buildings with **absolute heights** (e.g. m NHN, m NAP). Datasets whose buildings all start at
  z = 0 cannot be used.
- Optional: a terrain in the same height system as the data (e.g. basemap.de DGM5 for German NHN
  data), so the terrain hides the water on dry ground and the flood outline becomes visible.

## Enable it

In `production/.env`:

```
FLOOD_SCENE=true
# Optional: function codes that are not buildings (excluded from counts), e.g. ALKIS
FLOOD_EXCLUDE_FUNCTIONS=51007_1500,51009_1610,51009_1700,53001_1800
# Optional, viewer only: a gauge widget next to the water level
FLOOD_GAUGE_NAME=Passau/Donau
FLOOD_GAUGE_ZERO_M=286.439
FLOOD_STAGES=700,740,770,850
FLOOD_REFERENCE_CM=1289
FLOOD_REFERENCE_LABEL=Juni 2013
```

`FLOOD_SCENE=true` adds one short, dataset-independent block to the system prompt (`[FLOOD SCENE]`,
see `flood_scene_instructions()` in `production/webui/llm_utils.py`). It tells the model the column
convention below, how to count flooded buildings, and the excluded function codes, and it carries
six generic SQL examples (flooded count, flooded buildings of given types, facade area under water,
effect of a barrier, an address and the nearest dry building, flooded count per water level). The
examples use a placeholder level and get the `FLOOD_EXCLUDE_FUNCTIONS` filter inserted
automatically. The gauge settings only draw marks in the viewer; they are not added to the prompt.

Dataset knowledge (which gauge, its gauge zero, a historic flood level, address format) is given
to the chatbot in the conversation, e.g. as the first message. See
[`production/data/passau-notes.md`](../data/passau-notes.md) for a worked example.

## How it works

1. **Column convention.** A query result with a constant column `flood_level_m` (water level in the
   height system of the data) triggers the scene. An optional `protection_m` is the height of a
   property-level flood barrier. If the rows also carry an `objectid`, those buildings are in focus.

   ```sql
   SELECT count(*) AS betroffen, 299.33 AS flood_level_m
   FROM feature f
   WHERE f.objectclass_id = 901 AND ST_ZMin(f.envelope) < 299.33;
   ```

2. **Server side** (`extract_flood_scene` / `build_flood_payload` in `llm_utils.py`). The web UI
   reads the level from the last successful result and loads the base height of **every** Building
   with one fixed query (`get_flood_buildings` in `src/citydb_mcp/tools/highlight.py`):
   `ST_ZMin(envelope)`, minus `FLOOD_EXCLUDE_FUNCTIONS`. Neither the 500-row `run_query` limit nor
   the 200-id highlight limit applies. Results are cached for 5 minutes.

3. **Viewer** (`production/webmap-overlay/flood-bridge.js`).
   - Water: one flat polygon over the dataset extent with Cesium's water material, moved to the
     level (raw z values, the same convention the tiles are placed with). Terrain depth testing
     hides it where the ground is higher. It rises from just below the lowest building and pauses
     at each configured warning level.
   - Colours per building: red = base below the water, green = held back by `protection_m`,
     light grey = dry. Highlights (orange) and the user's selection (purple) are drawn on top.
   - Focus buildings, route view (up to 6, e.g. an address and the nearest dry buildings): the
     first flooded one (the "anchor", e.g. the user's house) dark red, dry ones dark green with a
     dashed line from the anchor and a label with the straight-line distance (`ST_Distance` of the
     envelopes, as in the chat answer); the camera looks at the anchor towards the dry buildings.
     Longer focus lists: orange, camera framing from the highlight. Everything else fades.
   - Tile features are boundary surfaces. They are mapped to their building by objectid prefix
     (`<building>_<suffix>`); surfaces that break this rule are sent explicitly.
   - Panel: affected count (with flooded/protected split), water level and gauge reading, a gauge
     widget, a level slider and "Hide flood". `window._citydbFlood.debug()` in the viewer frame
     prints the current state.

A building counts as affected when its **lowest point** is below the water. The water depth at a
building is `level − ST_ZMin(envelope)`; a barrier of height `h` protects it while
`ST_ZMin(envelope) + h ≥ level`.

## Limitations

- Flat water surface: rivers slope downstream, and tributaries have their own levels.
- No connectivity: low areas behind hills or dikes count as flooded.
- Flood defences, cellars, raised ground floors and the time course of a flood are not modelled.
- Colouring needs the tileset to use the building objectid as prefix of its surface ids, or the
  surfaces as direct `boundary` children of the Building; BuildingParts may be coloured incompletely.
- After rebuilding the image, browsers may keep the old viewer scripts in their cache; reload the
  viewer frame (or open DevTools with "Disable cache") if the scene does not appear.
