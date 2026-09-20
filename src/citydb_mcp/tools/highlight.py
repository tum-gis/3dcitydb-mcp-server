"""Resolve feature GMLIDs to the ids a 3D viewer can actually highlight.

A 3D Tiles export contains only features that own geometry. A ``Room`` (or any
other space/container) often has none of its own — its geometry lives on child
features (``InteriorWallSurface``, ``FloorSurface``, ...) linked through
``property.val_feature_id`` — so its ``objectid`` alone matches nothing in the
tileset. ``resolve_highlight_targets`` expands each requested ``objectid`` into
the set of ``objectid``s a viewer should style, plus a WGS84 centroid and
radius for the camera.

Read-only; runs through ``DatabaseConnection.execute`` like every other tool.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep this module importable without a database driver
    from ..db import DatabaseConnection

logger = logging.getLogger(__name__)

MAX_OBJECTIDS = 200
MAX_DEPTH = 5
MAX_ROWS = 5000
MIN_RADIUS_M = 3.0

# val_relation_type = 1 is containment ("the referenced feature is a part of the
# parent"); 0 is a general association (e.g. sibling spaces) and must not be
# followed, or unrelated features and cycles get pulled in.
_TREE_SQL = """
WITH RECURSIVE requested AS (
    SELECT f.id, f.objectid, oc.classname
    FROM feature f
    JOIN objectclass oc ON oc.id = f.objectclass_id
    WHERE f.objectid = ANY(%s)
),
tree AS (
    SELECT r.id AS root_id, r.id AS feature_id, 0 AS depth, ARRAY[r.id] AS path
    FROM requested r
    UNION ALL
    SELECT t.root_id, p.val_feature_id, t.depth + 1, t.path || p.val_feature_id
    FROM tree t
    JOIN property p ON p.feature_id = t.feature_id
                   AND p.val_relation_type = 1
                   AND p.val_feature_id IS NOT NULL
    WHERE t.depth < %s
      AND NOT (p.val_feature_id = ANY(t.path))
)
SELECT r.objectid AS root_objectid,
       r.classname AS root_classname,
       f.objectid AS tile_objectid,
       EXISTS (SELECT 1 FROM geometry_data g WHERE g.feature_id = f.id) AS has_geometry,
       EXISTS (SELECT 1 FROM property ip
               WHERE ip.feature_id = f.id AND ip.val_implicitgeom_id IS NOT NULL) AS has_implicit
FROM tree t
JOIN requested r ON r.id = t.root_id
JOIN feature f ON f.id = t.feature_id
LIMIT %s
"""

# The top-level feature each requested feature is (transitively) part of, found by
# walking containment upwards. Used to tell the viewer which way is "outside".
_TOP_SQL = """
WITH RECURSIVE requested AS (
    SELECT f.id, f.objectid FROM feature f WHERE f.objectid = ANY(%s)
),
up AS (
    SELECT r.id AS root_id, r.id AS feature_id, 0 AS depth, ARRAY[r.id] AS path
    FROM requested r
    UNION ALL
    SELECT u.root_id, p.feature_id, u.depth + 1, u.path || p.feature_id
    FROM up u
    JOIN property p ON p.val_feature_id = u.feature_id AND p.val_relation_type = 1
    WHERE u.depth < %s AND NOT (p.feature_id = ANY(u.path))
)
SELECT DISTINCT ON (u.root_id) r.objectid AS root_objectid, f.objectid AS top_objectid
FROM up u
JOIN requested r ON r.id = u.root_id
JOIN feature f ON f.id = u.feature_id
ORDER BY u.root_id, u.depth DESC
"""

# Extent (WGS84) of whatever geometry column `e` the wrapped query yields.
# Tiles are placed using the raw z values, so height_m is the mid z of the
# extent (only from geometries that actually have a z dimension).
_EXTENT_SQL = """
WITH env AS (
    SELECT ST_Transform(e, 4326) AS e
    FROM ({source}) s
    WHERE e IS NOT NULL AND ST_SRID(e) > 0
),
ext AS (
    SELECT ST_Extent(ST_Force2D(e))::geometry AS g,
           MIN(ST_ZMin(e)) FILTER (WHERE ST_NDims(e) >= 3) AS zmin,
           MAX(ST_ZMax(e)) FILTER (WHERE ST_NDims(e) >= 3) AS zmax
    FROM env
)
SELECT ST_Y(ST_Centroid(g)) AS lat,
       ST_X(ST_Centroid(g)) AS long,
       ST_Distance(
           ST_SetSRID(ST_MakePoint(ST_XMin(g), ST_YMin(g)), 4326)::geography,
           ST_SetSRID(ST_MakePoint(ST_XMax(g), ST_YMax(g)), 4326)::geography
       ) / 2.0 AS radius_m,
       (zmin + zmax) / 2.0 AS height_m
FROM ext
WHERE g IS NOT NULL
"""

# The whole dataset's bounding box in WGS84 — the same ST_Extent(envelope) the
# prompt's "Bounding Box" is built from (get_db_context_snapshot), transformed
# for the viewer's initial camera. `height_*` are the envelopes' z range.
_DATASET_EXTENT_SQL = """
WITH ext AS (
    SELECT ST_SetSRID(ST_Extent(envelope)::geometry,
                      (SELECT srid FROM database_srs LIMIT 1)) AS g,
           MIN(ST_ZMin(envelope)) FILTER (WHERE ST_NDims(envelope) >= 3) AS zmin,
           MAX(ST_ZMax(envelope)) FILTER (WHERE ST_NDims(envelope) >= 3) AS zmax
    FROM feature
    WHERE envelope IS NOT NULL
)
SELECT ST_XMin(t) AS west, ST_YMin(t) AS south, ST_XMax(t) AS east, ST_YMax(t) AS north,
       zmin AS height_min, zmax AS height_max
FROM (SELECT ST_Transform(g, 4326) AS t, zmin, zmax FROM ext WHERE g IS NOT NULL) s
"""

_ENVELOPE_SOURCE = "SELECT envelope AS e FROM feature WHERE objectid = ANY(%s)"
_GEOMETRY_SOURCE = (
    "SELECT ST_Envelope(g.geometry) AS e FROM geometry_data g "
    "JOIN feature f ON f.id = g.feature_id WHERE f.objectid = ANY(%s)"
)


def _clean_ids(objectids) -> list[str]:
    seen: dict[str, None] = {}
    for oid in objectids or []:
        if oid is None:
            continue
        s = str(oid).strip()
        if s:
            seen.setdefault(s)
    return list(seen)[:MAX_OBJECTIDS]


def _extent(db: DatabaseConnection, source: str, ids: list[str]) -> dict | None:
    if not ids:
        return None
    try:
        row = db.execute_single(_EXTENT_SQL.format(source=source), (ids,))
    except Exception as exc:  # unknown SRID, missing extension, ...
        logger.warning("highlight extent query failed: %s", exc)
        return None
    if not row or row.get("lat") is None or row.get("long") is None:
        return None
    height = row.get("height_m")
    return {
        "lat": float(row["lat"]),
        "long": float(row["long"]),
        "radius_m": max(float(row.get("radius_m") or 0.0), MIN_RADIUS_M),
        "height_m": float(height) if height is not None else None,
    }


def _anchor(db: DatabaseConnection, objectids: list[str]) -> dict | None:
    """Centre {lat, long} of the top-level feature all of `objectids` belong to.

    None if they belong to different top-level features (or the lookup fails);
    the viewer then has no single "outside" to face.
    """
    if not objectids:
        return None
    try:
        rows = db.execute(_TOP_SQL, (objectids, MAX_DEPTH))
    except Exception as exc:
        logger.warning("highlight anchor query failed: %s", exc)
        return None
    tops = {r["top_objectid"] for r in rows}
    if len(tops) != 1:
        return None
    ext = _extent(db, _ENVELOPE_SOURCE, list(tops))
    return {"lat": ext["lat"], "long": ext["long"]} if ext else None


def resolve_highlight_targets(db: DatabaseConnection, objectids: list[str]) -> dict:
    """Expand ``objectids`` into viewer-highlightable ids and a camera target.

    Returns::

        {
          "resolved": [{"objectid", "classname", "tile_ids": [...],
                        "implicit_only": bool}],
          "missing": [objectid, ...],       # not in the database
          "not_tileable": [objectid, ...],  # exist, but neither they nor any
                                            # contained feature own geometry
          "tile_ids": [...],                # union over all resolved
          "centroid": {"lat", "long", "radius_m", "height_m"|None,
                       "anchor": {"lat", "long"}|None} | None,
          "truncated": bool,
        }

    ``tile_ids`` for one feature is its own objectid (if it owns geometry) plus
    every contained descendant that does. The union — not either/or — because
    the tiler may have tiled a different LoD than the one owning the geometry.
    """
    ids = _clean_ids(objectids)
    if not ids:
        return {"resolved": [], "missing": [], "not_tileable": [],
                "tile_ids": [], "centroid": None, "truncated": False}

    rows = db.execute(_TREE_SQL, (ids, MAX_DEPTH, MAX_ROWS))

    by_root: dict[str, dict] = {}
    for row in rows:
        entry = by_root.setdefault(row["root_objectid"], {
            "objectid": row["root_objectid"],
            "classname": row["root_classname"],
            "tile_ids": {},
            "implicit_only": True,
        })
        if row["has_geometry"] or row["has_implicit"]:
            entry["tile_ids"].setdefault(row["tile_objectid"])
        if row["has_geometry"]:
            entry["implicit_only"] = False

    resolved = []
    not_tileable = []
    all_tile_ids: dict[str, None] = {}
    for oid in ids:  # keep the caller's order
        entry = by_root.get(oid)
        if entry is None:
            continue
        tile_ids = list(entry["tile_ids"])
        if not tile_ids:
            not_tileable.append(oid)
        for t in tile_ids:
            all_tile_ids.setdefault(t)
        resolved.append({
            "objectid": oid,
            "classname": entry["classname"],
            "tile_ids": tile_ids,
            "implicit_only": bool(tile_ids) and entry["implicit_only"],
        })

    missing = [oid for oid in ids if oid not in by_root]

    # Camera target: envelope of the requested features, falling back to the
    # contained features' envelopes, then to their raw geometry.
    known = [r["objectid"] for r in resolved]
    tile_list = list(all_tile_ids)
    centroid = (
        _extent(db, _ENVELOPE_SOURCE, known)
        or _extent(db, _ENVELOPE_SOURCE, tile_list)
        or _extent(db, _GEOMETRY_SOURCE, tile_list)
    )

    if centroid:
        centroid["anchor"] = _anchor(db, known)

    return {
        "resolved": resolved,
        "missing": missing,
        "not_tileable": not_tileable,
        "tile_ids": tile_list,
        "centroid": centroid,
        "truncated": len(rows) >= MAX_ROWS,
    }


def get_dataset_extent(db: DatabaseConnection) -> dict | None:
    """Bounding box of all features in WGS84: {west, south, east, north, height_min, height_max}.

    None if the database has no envelopes or the SRID cannot be transformed.
    """
    try:
        row = db.execute_single(_DATASET_EXTENT_SQL)
    except Exception as exc:  # unknown SRID, missing table, ...
        logger.warning("dataset extent query failed: %s", exc)
        return None
    if not row or any(row.get(k) is None for k in ("west", "south", "east", "north")):
        return None
    return {
        k: (float(row[k]) if row.get(k) is not None else None)
        for k in ("west", "south", "east", "north", "height_min", "height_max")
    }
