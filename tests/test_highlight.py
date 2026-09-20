"""Unit tests for resolve_highlight_targets.

They use a fake database object, so no PostgreSQL connection is needed.

Usage:
    pytest tests/test_highlight.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.tools import highlight
from citydb_mcp.tools.highlight import get_dataset_extent, resolve_highlight_targets


def _row(root, cls, tile, geom=True, implicit=False):
    return {
        "root_objectid": root, "root_classname": cls, "tile_objectid": tile,
        "has_geometry": geom, "has_implicit": implicit,
    }


class FakeDb:
    """Returns canned rows for the tree query and canned extents."""

    def __init__(self, tree_rows, extents=None, top_rows=None):
        self.tree_rows = tree_rows
        self.top_rows = top_rows or []
        self.extents = list(extents or [])
        self.tree_calls = []

    def execute(self, query, params=None):
        if "FROM up u" in query:  # the "which building is this part of" query
            return self.top_rows
        self.tree_calls.append(params)
        return self.tree_rows

    def execute_single(self, query, params=None):
        return self.extents.pop(0) if self.extents else None


_EXT = {"lat": 48.1, "long": 11.5, "radius_m": 12.0, "height_m": 521.5}


def test_room_without_own_geometry_expands_to_children():
    db = FakeDb(
        [
            _row("room-1", "Room", "room-1", geom=False),
            _row("room-1", "Room", "wall-a"),
            _row("room-1", "Room", "floor-b"),
        ],
        extents=[_EXT],
    )
    out = resolve_highlight_targets(db, ["room-1"])
    assert out["resolved"] == [
        {"objectid": "room-1", "classname": "Room",
         "tile_ids": ["wall-a", "floor-b"], "implicit_only": False}
    ]
    assert out["tile_ids"] == ["wall-a", "floor-b"]
    assert out["missing"] == [] and out["not_tileable"] == []
    assert out["centroid"] == {**_EXT, "anchor": None}


def test_feature_with_own_and_child_geometry_returns_both():
    db = FakeDb(
        [_row("bldg", "Building", "bldg"), _row("bldg", "Building", "roof")],
        extents=[_EXT],
    )
    out = resolve_highlight_targets(db, ["bldg"])
    assert out["tile_ids"] == ["bldg", "roof"]


def test_unknown_id_is_missing():
    db = FakeDb([_row("a", "Room", "a")], extents=[_EXT])
    out = resolve_highlight_targets(db, ["a", "nope"])
    assert out["missing"] == ["nope"]
    assert [r["objectid"] for r in out["resolved"]] == ["a"]


def test_feature_without_any_geometry_is_not_tileable():
    db = FakeDb([_row("empty", "Storey", "empty", geom=False)])
    out = resolve_highlight_targets(db, ["empty"])
    assert out["not_tileable"] == ["empty"]
    assert out["resolved"][0]["tile_ids"] == []
    assert out["tile_ids"] == []
    assert out["centroid"] is None


def test_implicit_geometry_only_is_flagged():
    db = FakeDb([_row("door", "Door", "door", geom=False, implicit=True)],
                extents=[_EXT])
    out = resolve_highlight_targets(db, ["door"])
    assert out["resolved"][0]["tile_ids"] == ["door"]
    assert out["resolved"][0]["implicit_only"] is True
    assert out["not_tileable"] == []


def test_duplicate_child_reached_twice_is_listed_once():
    db = FakeDb(
        [_row("r", "Room", "w"), _row("r", "Room", "w"), _row("r", "Room", "f")],
        extents=[_EXT],
    )
    assert resolve_highlight_targets(db, ["r"])["tile_ids"] == ["w", "f"]


def test_input_is_deduplicated_blank_stripped_and_capped():
    db = FakeDb([])
    ids = [f"id{i}" for i in range(highlight.MAX_OBJECTIDS + 50)]
    resolve_highlight_targets(db, ["", None, " id0 ", "id0"] + ids)
    (params,) = db.tree_calls
    sent = params[0]
    assert len(sent) == highlight.MAX_OBJECTIDS
    assert sent[0] == "id0" and len(set(sent)) == len(sent)


def test_empty_input_does_not_touch_the_database():
    db = FakeDb([])
    out = resolve_highlight_targets(db, [])
    assert out["resolved"] == [] and out["centroid"] is None
    assert db.tree_calls == []


def test_centroid_falls_back_through_sources():
    db = FakeDb([_row("r", "Room", "w")], extents=[None, None, _EXT])
    assert resolve_highlight_targets(db, ["r"])["centroid"] == {**_EXT, "anchor": None}


def test_extent_failure_yields_null_centroid_not_an_exception():
    class Boom(FakeDb):
        def execute_single(self, query, params=None):
            raise RuntimeError("Input geometry has unknown (0) SRID")

    out = resolve_highlight_targets(Boom([_row("r", "Room", "w")]), ["r"])
    assert out["tile_ids"] == ["w"]
    assert out["centroid"] is None


def test_radius_has_a_floor_and_missing_height_is_none():
    db = FakeDb([_row("r", "Room", "w")],
                extents=[{"lat": 1.0, "long": 2.0, "radius_m": 0.0, "height_m": None}])
    centroid = resolve_highlight_targets(db, ["r"])["centroid"]
    assert centroid["radius_m"] == highlight.MIN_RADIUS_M
    assert centroid["height_m"] is None


def test_truncation_flag():
    rows = [_row("r", "Room", f"c{i}") for i in range(highlight.MAX_ROWS)]
    assert resolve_highlight_targets(FakeDb(rows, extents=[_EXT]), ["r"])["truncated"] is True


def test_dataset_extent_returns_floats():
    row = {"west": 11.55, "south": 48.13, "east": 11.57, "north": 48.21,
           "height_min": 494, "height_max": 529.6}
    assert get_dataset_extent(FakeDb([], extents=[row])) == {
        "west": 11.55, "south": 48.13, "east": 11.57, "north": 48.21,
        "height_min": 494.0, "height_max": 529.6,
    }


def test_dataset_extent_none_without_envelopes_or_on_error():
    assert get_dataset_extent(FakeDb([])) is None
    assert get_dataset_extent(FakeDb([], extents=[{"west": None, "south": None,
                                                    "east": None, "north": None}])) is None

    class Boom(FakeDb):
        def execute_single(self, query, params=None):
            raise RuntimeError("Input geometry has unknown (0) SRID")

    assert get_dataset_extent(Boom([])) is None


def test_dataset_extent_without_z_keeps_a_null_height():
    row = {"west": 1.0, "south": 2.0, "east": 3.0, "north": 4.0,
           "height_min": None, "height_max": None}
    assert get_dataset_extent(FakeDb([], extents=[row]))["height_min"] is None


def test_anchor_is_the_shared_top_level_feature_centre():
    anchor_row = {"lat": 48.2, "long": 11.56, "radius_m": 30.0, "height_m": 520.0}
    db = FakeDb(
        [_row("w1", "Window", "w1"), _row("w2", "Window", "w2")],
        extents=[_EXT, anchor_row],
        top_rows=[{"root_objectid": "w1", "top_objectid": "bldg"},
                  {"root_objectid": "w2", "top_objectid": "bldg"}],
    )
    out = resolve_highlight_targets(db, ["w1", "w2"])
    assert out["centroid"]["anchor"] == {"lat": 48.2, "long": 11.56}


def test_no_anchor_when_features_belong_to_different_buildings():
    db = FakeDb(
        [_row("w1", "Window", "w1"), _row("w2", "Window", "w2")],
        extents=[_EXT],
        top_rows=[{"root_objectid": "w1", "top_objectid": "b1"},
                  {"root_objectid": "w2", "top_objectid": "b2"}],
    )
    assert resolve_highlight_targets(db, ["w1", "w2"])["centroid"]["anchor"] is None


def test_anchor_lookup_failure_does_not_break_the_result():
    class Boom(FakeDb):
        def execute(self, query, params=None):
            if "FROM up u" in query:
                raise RuntimeError("boom")
            return super().execute(query, params)

    out = resolve_highlight_targets(Boom([_row("w", "Window", "w")], extents=[_EXT]), ["w"])
    assert out["tile_ids"] == ["w"] and out["centroid"]["anchor"] is None
