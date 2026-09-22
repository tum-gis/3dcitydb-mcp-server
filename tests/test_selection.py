"""Unit tests for get_feature_tree and describe_selection.

They use a fake database object, so no PostgreSQL connection is needed.

Usage:
    pytest tests/test_selection.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.tools import selection
from citydb_mcp.tools.selection import get_feature_tree, describe_selection


class FakeDb:
    """Dispatches on the SQL marker comment, like tests/test_highlight.py's FakeDb."""

    def __init__(self, picked=None, ancestors=None, children=None, siblings_by_parent=None):
        self.picked = picked
        self.ancestors = ancestors or []
        self.children = children or []
        # {parent_objectid: [rows]} — _DOWN_SQL is called with children's own
        # parent for both "children of the picked feature" and "siblings of
        # its parent", so results must be keyed by which parent was asked for.
        self.siblings_by_parent = siblings_by_parent or {}
        self.down_calls = []

    def execute_single(self, query, params=None):
        if "PICKED_SQL" in query:
            return self.picked
        raise AssertionError(f"unexpected execute_single call: {query[:40]}")

    def execute(self, query, params=None):
        if "UP_SQL" in query:
            return self.ancestors
        if "DOWN_SQL" in query:
            parent = params[0]
            self.down_calls.append(parent)
            return self.siblings_by_parent.get(parent, self.children)
        raise AssertionError(f"unexpected execute call: {query[:40]}")


def _anc(oid, cls, depth):
    return {"objectid": oid, "classname": cls, "depth": depth}


def _feat(oid, cls, geom=True):
    return {"objectid": oid, "classname": cls, "has_geometry": geom}


def test_unknown_objectid_returns_empty_tree():
    db = FakeDb(picked=None)
    out = get_feature_tree(db, "nope")
    assert out == {
        "picked": None, "ancestors": [],
        "children": [], "children_truncated": False,
        "siblings": [], "siblings_truncated": False,
    }


def test_blank_objectid_does_not_touch_the_database():
    class Boom(FakeDb):
        def execute_single(self, query, params=None):
            raise AssertionError("should not be called")

    out = get_feature_tree(Boom(), "  ")
    assert out["picked"] is None


def test_ancestors_are_ordered_nearest_first():
    db = FakeDb(
        picked={"objectid": "wall-1", "classname": "WallSurface"},
        ancestors=[_anc("bldg", "Building", 2), _anc("room", "Room", 1)],
    )
    out = get_feature_tree(db, "wall-1")
    assert [a["objectid"] for a in out["ancestors"]] == ["room", "bldg"]


def test_children_come_from_the_picked_feature():
    db = FakeDb(
        picked={"objectid": "bldg", "classname": "Building"},
        ancestors=[],
        children=[_feat("roof", "RoofSurface"), _feat("wall", "WallSurface")],
    )
    out = get_feature_tree(db, "bldg")
    assert [c["objectid"] for c in out["children"]] == ["roof", "wall"]
    assert out["children_truncated"] is False
    # No ancestors -> no parent to ask for siblings.
    assert out["siblings"] == []
    assert db.down_calls == ["bldg"]


def test_siblings_come_from_the_immediate_parent_and_exclude_the_picked_feature():
    db = FakeDb(
        picked={"objectid": "wall-1", "classname": "WallSurface"},
        ancestors=[_anc("bldg", "Building", 1)],
        siblings_by_parent={
            "wall-1": [_feat("win-1", "Window")],
            "bldg": [_feat("wall-1", "WallSurface"), _feat("roof", "RoofSurface")],
        },
    )
    out = get_feature_tree(db, "wall-1")
    assert [c["objectid"] for c in out["children"]] == ["win-1"]
    assert [s["objectid"] for s in out["siblings"]] == ["roof"]
    assert db.down_calls == ["wall-1", "bldg"]


def test_children_truncated_flag():
    many = [_feat(f"c{i}", "Window") for i in range(selection.MAX_TREE_CHILDREN + 5)]
    db = FakeDb(picked={"objectid": "bldg", "classname": "Building"}, children=many)
    out = get_feature_tree(db, "bldg")
    assert len(out["children"]) == selection.MAX_TREE_CHILDREN
    assert out["children_truncated"] is True


def test_cycle_in_ancestors_does_not_break_ordering():
    # The recursive SQL itself guards against cycles (NOT (... = ANY(path))); the
    # Python side just has to sort whatever depths come back without assuming
    # any particular row order.
    db = FakeDb(
        picked={"objectid": "a", "classname": "X"},
        ancestors=[_anc("c", "X", 3), _anc("b", "X", 2), _anc("d", "X", 1)],
    )
    out = get_feature_tree(db, "a")
    assert [a["depth"] for a in out["ancestors"]] == [1, 2, 3]


# ── describe_selection ──────────────────────────────────────────────────────
# Delegates to resolve_highlight_targets, so reuse its FakeDb/row shape.
# (tests/ has no __init__.py, so pytest puts it directly on sys.path — the
# same reason "from citydb_mcp..." works above without a "src." prefix.)
from test_highlight import FakeDb as HighlightFakeDb, _row, _EXT  # noqa: E402


def test_describe_selection_tallies_by_classname():
    db = HighlightFakeDb(
        [
            _row("bldg-1", "Building", "bldg-1"),
            _row("bldg-2", "Building", "bldg-2"),
            _row("wall-1", "WallSurface", "wall-1"),
        ],
        extents=[_EXT],
    )
    out = describe_selection(db, ["bldg-1", "bldg-2", "wall-1"])
    assert out["count"] == 3
    assert out["by_classname"] == {"Building": 2, "WallSurface": 1}
    assert "centroid" not in out
    assert {f["objectid"] for f in out["features"]} == {"bldg-1", "bldg-2", "wall-1"}


def test_describe_selection_reports_missing():
    db = HighlightFakeDb([_row("a", "Room", "a")], extents=[_EXT])
    out = describe_selection(db, ["a", "nope"])
    assert out["missing"] == ["nope"]
    assert out["count"] == 1
