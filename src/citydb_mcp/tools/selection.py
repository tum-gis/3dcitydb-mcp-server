"""Browse the CityGML containment tree around one picked feature, and describe
a multi-feature viewer selection for the chat agent.

Companion to ``highlight.py``: a click in the 3D viewer usually lands on a
boundary surface (a wall, a roof), not the feature the user actually means
(the building). ``get_feature_tree`` lets the viewer show the containment
chain around whatever was picked — its ancestors, its own children, and its
siblings — so the user can select the right level themselves.
``describe_selection`` then turns a finished multi-feature selection into
what the chat agent needs to scope a query to it.

Read-only; runs through ``DatabaseConnection.execute`` like every other tool.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .highlight import MAX_DEPTH, resolve_highlight_targets

if TYPE_CHECKING:  # keep this module importable without a database driver
    from ..db import DatabaseConnection

logger = logging.getLogger(__name__)

MAX_TREE_CHILDREN = 200

# Single-row lookup of the picked feature itself.
# -- PICKED_SQL
_PICKED_SQL = """
-- PICKED_SQL
SELECT f.objectid AS objectid, oc.classname AS classname
FROM feature f
JOIN objectclass oc ON oc.id = f.objectclass_id
WHERE f.objectid = %s
"""

# Containment walked upwards from the picked feature, every level kept (unlike
# highlight._TOP_SQL, which collapses to just the top). depth=0 is the picked
# feature itself (excluded by the caller); depth=1 is its immediate container.
# Same cycle guard and MAX_DEPTH cap as highlight.py's upward walk.
_UP_SQL = """
-- UP_SQL
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
SELECT f.objectid AS objectid, oc.classname AS classname, u.depth AS depth
FROM up u
JOIN feature f ON f.id = u.feature_id
JOIN objectclass oc ON oc.id = f.objectclass_id
WHERE u.depth > 0
ORDER BY u.depth ASC
"""

# One level of features directly contained in `parent_objectid` (children when
# called with the picked feature; siblings when called with its immediate
# parent). Same has_geometry predicate as highlight._TREE_SQL.
_DOWN_SQL = """
-- DOWN_SQL
SELECT f.objectid AS objectid, oc.classname AS classname,
       EXISTS (SELECT 1 FROM geometry_data g WHERE g.feature_id = f.id) AS has_geometry
FROM property p
JOIN feature f ON f.id = p.val_feature_id
JOIN objectclass oc ON oc.id = f.objectclass_id
WHERE p.feature_id = (SELECT id FROM feature WHERE objectid = %s)
  AND p.val_relation_type = 1
  AND p.val_feature_id IS NOT NULL
LIMIT %s
"""


def _clean_id(objectid) -> str:
    return str(objectid).strip() if objectid else ""


def _children_of(db: DatabaseConnection, parent_objectid: str, *, exclude: str | None = None) -> tuple[list[dict], bool]:
    """One level of contained features, capped, with a truncation flag."""
    rows = db.execute(_DOWN_SQL, (parent_objectid, MAX_TREE_CHILDREN + 1))
    if exclude is not None:
        rows = [r for r in rows if r["objectid"] != exclude]
    truncated = len(rows) > MAX_TREE_CHILDREN
    items = [
        {"objectid": r["objectid"], "classname": r["classname"], "has_geometry": bool(r["has_geometry"])}
        for r in rows[:MAX_TREE_CHILDREN]
    ]
    return items, truncated


def get_feature_tree(db: DatabaseConnection, objectid: str) -> dict:
    """The containment neighbourhood of one picked feature.

    Returns::

        {
          "picked": {"objectid", "classname"} | None,   # None if not in the database
          "ancestors": [{"objectid", "classname", "depth"}, ...],  # nearest parent first
          "children": [{"objectid", "classname", "has_geometry"}, ...],
          "children_truncated": bool,
          "siblings": [{"objectid", "classname", "has_geometry"}, ...],
          "siblings_truncated": bool,
        }

    ``children`` is one level down from the picked feature (e.g. a building's
    boundary surfaces); ``siblings`` is one level down from its immediate
    parent, excluding the picked feature itself (e.g. the other surfaces of
    the same building). Both are capped at ``MAX_TREE_CHILDREN``.
    """
    oid = _clean_id(objectid)
    empty = {
        "picked": None, "ancestors": [],
        "children": [], "children_truncated": False,
        "siblings": [], "siblings_truncated": False,
    }
    if not oid:
        return empty

    picked_row = db.execute_single(_PICKED_SQL, (oid,))
    if not picked_row:
        return empty
    picked = {"objectid": picked_row["objectid"], "classname": picked_row["classname"]}

    up_rows = db.execute(_UP_SQL, ([oid], MAX_DEPTH))
    ancestors = sorted(
        (
            {"objectid": r["objectid"], "classname": r["classname"], "depth": r["depth"]}
            for r in up_rows
        ),
        key=lambda a: a["depth"],
    )

    children, children_truncated = _children_of(db, oid)

    siblings: list[dict] = []
    siblings_truncated = False
    if ancestors:
        siblings, siblings_truncated = _children_of(db, ancestors[0]["objectid"], exclude=oid)

    return {
        "picked": picked,
        "ancestors": ancestors,
        "children": children,
        "children_truncated": children_truncated,
        "siblings": siblings,
        "siblings_truncated": siblings_truncated,
    }


def describe_selection(db: DatabaseConnection, objectids: list[str]) -> dict:
    """What the chat agent needs about a finished multi-feature viewer selection.

    Returns::

        {
          "features": [{"objectid", "classname", "tile_ids": [...]}],
          "count": int,
          "by_classname": {"Building": 3, "WallSurface": 1},
          "missing": [objectid, ...],
          "truncated": bool,
        }

    Delegates to ``resolve_highlight_targets`` for the tree-expansion and
    tileability logic (a Room's boundary surfaces, etc.) — never re-queries
    what it already computes. Drops the camera centroid: selecting a feature
    the user is already looking at must never move the camera.
    """
    result = resolve_highlight_targets(db, objectids)

    by_classname: dict[str, int] = {}
    for r in result["resolved"]:
        by_classname[r["classname"]] = by_classname.get(r["classname"], 0) + 1

    return {
        "features": [
            {"objectid": r["objectid"], "classname": r["classname"], "tile_ids": r["tile_ids"]}
            for r in result["resolved"]
        ],
        "count": len(result["resolved"]),
        "by_classname": by_classname,
        "missing": result["missing"],
        "truncated": result["truncated"],
    }
