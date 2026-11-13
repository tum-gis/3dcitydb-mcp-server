"""Tests for the scratchpad's JSON export / import (the viewer's item list).

The database is faked like in test_scratchpad.py; the round trip against a
real database is described in production/docs/scratchpad.md.

Usage:
    pytest tests/test_scratchpad_io.py -v
"""

import datetime
import json
import os
import sys

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from citydb_mcp.tools import scratchpad as sp  # noqa: E402

UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ROW_PARAMS = 14  # parameters per row in the import INSERT


class FakeDb:
    """Answers by the first matching key in ``replies`` (substring of the SQL)."""

    schema = "citydb"

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def execute(self, query, params=None, settings=None):
        text = query if isinstance(query, str) else repr(query)
        self.calls.append((text, params, settings))
        for key, rows in self.replies.items():
            if key in text:
                return rows(text, params) if callable(rows) else rows
        return []


@pytest.fixture(autouse=True)
def _enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "true")
    monkeypatch.setenv("CITYDB_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))


def _audit_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _shape(gtype="POLYGON", ndims=3, vertices=5):
    def reply(text, params):
        return [{"vertices": vertices, "gtype": gtype, "ndims": ndims, "members": [gtype],
                 "closed": False, "shape_ewkb": "ab" + str(len(params["ewkt"]))}]
    return reply


def _insert_reply(text, params):
    return [{"name": params[i + 1]} for i in range(0, len(params), ROW_PARAMS)]


def _import_db(existing=(), shape=None, found=None):
    return FakeDb({
        "database_srs": [{"srid": 25832}],
        "ST_GeomFromEWKT": shape or _shape(),
        "count(DISTINCT objectid)": [{"n": found if found is not None else 0}],
        "INSERT INTO": _insert_reply,
        "SELECT name FROM": [{"name": n} for n in existing],
    })


def _file(*items, **top):
    return {"format": "3dcitydb-scratchpad", "version": 1, "srid": 25832,
            "exported_at": "2026-11-13T10:00:00+00:00", "items": list(items), **top}


def _geom_item(name="area", **kw):
    item = {"name": name, "label": "An area", "kind": "geometry", "class": "Polygon3D",
            "color": "#00ff00", "alpha": 0.5, "clamp": False, "is_visible": True, "data": {"count": 1},
            "created_at": "2026-11-01T08:00:00+00:00", "geom": "SRID=25832;POLYGON Z ((0 0 1,1 0 1,1 1 1,0 0 1))"}
    item.update(kw)
    return item


def _set_item(name="houses", **kw):
    item = {"name": name, "kind": "featureset", "class": "FeatureSet", "color": "#123456",
            "objectids": ["B1", "B2"]}
    item.update(kw)
    return item


def _inserted(db):
    """The rows of the import INSERT as dicts."""
    calls = [c for c in db.calls if "INSERT INTO" in c[0]]
    if not calls:
        return []
    assert len(calls) == 1  # one statement, one transaction
    p = calls[0][1]
    keys = ("user_id", "name", "label", "kind", "class", "color", "alpha", "clamp", "ewkb", "ewkb2",
            "objectids", "is_visible", "data", "created_at")
    return [dict(zip(keys, p[i:i + ROW_PARAMS])) for i in range(0, len(p), ROW_PARAMS)]


# ── export ────────────────────────────────────────────────────────────────────

def test_export_shape(tmp_path):
    ts = datetime.datetime(2026, 11, 1, 8, 0, tzinfo=datetime.timezone.utc)
    db = FakeDb({
        "database_srs": [{"srid": 25832}],
        "ST_AsEWKT": [
            {"name": "area", "label": "An area", "kind": "geometry", "class": "Polygon3D", "color": "#00ff00",
             "alpha": 0.5, "clamp": False, "is_visible": True, "data": {"count": 1}, "created_at": ts,
             "updated_at": ts, "geom": "SRID=25832;POLYGON Z ((0 0 1,1 0 1,1 1 1,0 0 1))", "objectids": None},
            {"name": "houses", "label": None, "kind": "featureset", "class": "FeatureSet", "color": "#123456",
             "alpha": 0.8, "clamp": False, "is_visible": False, "data": {}, "created_at": ts,
             "updated_at": ts, "geom": None, "objectids": ["B1", "B2"]},
        ],
    })
    out = sp.export_items(UID, db=db)
    assert out["format"] == "3dcitydb-scratchpad" and out["version"] == 1 and out["srid"] == 25832
    assert out["exported_at"]
    area, houses = out["items"]
    assert area["geom"].startswith("SRID=25832;POLYGON Z") and "objectids" not in area
    assert area["created_at"] == ts.isoformat() and area["alpha"] == 0.5
    assert houses["objectids"] == ["B1", "B2"] and "geom" not in houses and houses["is_visible"] is False
    json.dumps(out)  # serialisable as is
    assert any(c[1] == (UID,) for c in db.calls)  # only this user's rows
    assert _audit_lines(tmp_path)[-1]["op"] == "export"


def test_export_needs_a_valid_user():
    with pytest.raises(ValueError):
        sp.export_items("not-a-uuid", db=FakeDb())


# ── import: file checks ───────────────────────────────────────────────────────

@pytest.mark.parametrize("payload,error", [
    ("not json", "not a JSON file"),
    ("[]", "expected a JSON object"),
    (json.dumps({"format": "geojson", "version": 1, "items": []}), "format"),
    (json.dumps({"type": "FeatureCollection", "features": []}), "format"),
    (json.dumps({"format": "3dcitydb-scratchpad", "version": 2, "items": []}), "version"),
    (json.dumps({"format": "3dcitydb-scratchpad", "version": 1}), "item list"),
    (json.dumps({"format": "3dcitydb-scratchpad", "version": 1, "srid": "x", "items": []}), "srid"),
    ("OVERSIZE", "too large"),
], ids=["not-json", "array", "geojson-format", "featurecollection", "version", "no-items", "srid",
        "oversize"])
def test_import_rejects_bad_files_and_writes_nothing(payload, error):
    if payload == "OVERSIZE":
        payload = "x" * (sp.MAX_IMPORT_BYTES + 1)
    db = _import_db()
    out = sp.import_items(UID, payload, wdb=db)
    assert out["ok"] is False and error in out["error"] and out["imported"] == 0
    assert db.calls == []


def test_import_rejects_too_many_items_in_the_file():
    db = _import_db()
    out = sp.import_items(UID, _file(*[_set_item(f"s{i}") for i in range(sp.MAX_ITEMS + 1)]), wdb=db)
    assert not out["ok"] and "too many items" in out["error"]
    assert db.calls == []


def test_import_needs_a_valid_user():
    out = sp.import_items("x", _file(_set_item()), wdb=_import_db())
    assert not out["ok"] and "user id" in out["error"]


# ── import: per-item checks ───────────────────────────────────────────────────

@pytest.mark.parametrize("item,reason", [
    (_set_item("bad name"), "invalid name"),
    (_set_item("x; DROP TABLE"), "invalid name"),
    (_set_item(kind="building"), "kind"),
    (_set_item(alpha=2), "alpha"),
    (_set_item(alpha="0.5"), "alpha"),
    (_set_item(clamp="yes"), "clamp"),
    (_set_item(data=[1, 2]), "data"),
    (_set_item(data={"x": "a\x00b"}), "NUL"),
    (_set_item(label=123), "label"),
    (_set_item(objectids="B1"), "objectids list"),
    (_set_item(objectids=["B1", 2]), "strings"),
    (_set_item(objectids=["B"] * (sp.MAX_FEATURESET + 1)), "too many objects"),
    (_set_item(**{"class": "Polygon3D"}), "does not fit a featureset"),
    (_set_item(**{"class": "Bogus"}), "unknown class"),
    (_geom_item(**{"class": "FeatureSet"}), "needs kind featureset"),
    (_geom_item(geom=None), "EWKT"),
    (_geom_item(geom=["POINT(1 2)"]), "EWKT"),
    ("just a string", "not an object"),
])
def test_import_skips_invalid_items(item, reason):
    db = _import_db()
    out = sp.import_items(UID, _file(item, _set_item("good")), wdb=db)
    assert out["ok"] and out["imported"] == 1
    assert len(out["skipped"]) == 1 and reason in out["skipped"][0]["reason"]
    assert [r["name"] for r in _inserted(db)] == ["good"]


def test_import_bad_color_gets_the_default():
    db = _import_db()
    out = sp.import_items(UID, _file(_set_item(color="red"), _set_item("b", color="#12345g")), wdb=db)
    assert out["ok"] and out["imported"] == 2
    assert {r["color"] for r in _inserted(db)} == {sp.DEFAULT_COLOR}


def test_import_drops_a_non_numeric_size_m():
    # list_items casts data->>'size_m' to a number: a string there would break the list
    db = _import_db()
    sp.import_items(UID, _file(_set_item(data={"size_m": "big", "sql": "SELECT 1"})), wdb=db)
    assert json.loads(_inserted(db)[0]["data"]) == {"sql": "SELECT 1"}


def test_import_checks_vertices_and_class_against_the_geometry():
    db = _import_db(shape=_shape("POLYGON", 3, vertices=sp.MAX_VERTICES + 1))
    out = sp.import_items(UID, _file(_geom_item()), wdb=db)
    assert out["imported"] == 0 and "too large" in out["skipped"][0]["reason"]
    assert _inserted(db) == []

    db = _import_db(shape=_shape("POINT", 3))
    out = sp.import_items(UID, _file(_geom_item(**{"class": "Polygon3D"})), wdb=db)
    assert out["imported"] == 0 and "does not fit a POINT" in out["skipped"][0]["reason"]

    db = _import_db(shape=_shape("POINT", 3))
    out = sp.import_items(UID, _file(_geom_item(**{"class": None})), wdb=db)
    assert out["imported"] == 1 and _inserted(db)[0]["class"] == "3Dmarker"  # derived

    db = _import_db(shape=_shape("POINT", 2, vertices=0))
    assert "empty" in sp.import_items(UID, _file(_geom_item()), wdb=db)["skipped"][0]["reason"]


def test_import_unreadable_geometry_is_skipped():
    def boom(text, params):
        raise RuntimeError("parse error - invalid geometry\nHINT: ...")
    db = _import_db(shape=boom)
    out = sp.import_items(UID, _file(_geom_item(geom="POLYGON((garbage"), _set_item()), wdb=db)
    assert out["ok"] and out["imported"] == 1
    assert "could not be read" in out["skipped"][0]["reason"]


def test_import_geometry_is_a_bound_parameter_and_transformed():
    evil = "SRID=4326;POINT(1 2)'); DROP TABLE citydb.scratchpad; --"
    db = _import_db()
    sp.import_items(UID, _file(_geom_item(geom=evil), srid=4326), wdb=db)
    for text, params, _ in db.calls:
        assert "DROP TABLE" not in text  # never in the SQL text
    shape_call = next(c for c in db.calls if "ST_GeomFromEWKT" in c[0])
    assert "ST_GeomFromEWKT(%(ewkt)s)" in shape_call[0] and "ST_Transform" in shape_call[0]
    assert shape_call[1] == {"ewkt": evil, "file_srid": 4326, "db_srid": 25832}
    # the INSERT gets the server's own EWKB of the checked geometry, not the file's text
    row = _inserted(db)[0]
    assert row["ewkb"] == row["ewkb2"] and evil not in json.dumps(_inserted(db))


def test_import_respects_max_items_for_the_user():
    existing = [f"old{i}" for i in range(sp.MAX_ITEMS - 1)]
    db = _import_db(existing=existing)
    out = sp.import_items(UID, _file(_set_item("old0"), _set_item("new1"), _set_item("new2")), wdb=db)
    assert out["ok"] and out["imported"] == 2 and out["replaced"] == ["old0"]
    assert out["skipped"] == [{"name": "new2", "reason": f"scratchpad full ({sp.MAX_ITEMS} items)"}]


def test_import_replace_semantics_and_duplicates(tmp_path):
    db = _import_db(existing=["houses"])
    out = sp.import_items(UID, json.dumps(_file(_set_item(), _geom_item(), _set_item())), wdb=db)
    assert out["ok"] and out["imported"] == 2 and out["replaced"] == ["houses"]
    assert out["skipped"] == [{"name": "houses", "reason": "duplicate name in the file"}]
    insert = next(c for c in db.calls if "INSERT INTO" in c[0])
    assert "ON CONFLICT (user_id, name) DO UPDATE" in insert[0]
    rows = _inserted(db)
    assert [r["name"] for r in rows] == ["houses", "area"]
    area = rows[1]
    assert (area["kind"], area["class"], area["color"], area["alpha"], area["label"]) == \
        ("geometry", "Polygon3D", "#00ff00", 0.5, "An area")
    assert area["objectids"] is None and rows[0]["ewkb"] is None and rows[0]["objectids"] == ["B1", "B2"]
    assert area["created_at"].startswith("2026-11-01T08:00")
    line = _audit_lines(tmp_path)[-1]
    assert line["op"] == "import" and line["user_id"] == UID and line["replaced"] == ["houses"]


def test_import_writes_only_for_the_given_user():
    db = _import_db()
    payload = _file(_set_item(user_id=OTHER), _geom_item(user_id=OTHER), user_id=OTHER)
    out = sp.import_items(UID, payload, wdb=db)
    assert out["imported"] == 2
    assert {r["user_id"] for r in _inserted(db)} == {UID}
    existing_query = next(c for c in db.calls if "SELECT name FROM" in c[0])
    assert existing_query[1] == (UID,)


def test_import_reports_unknown_objectids_but_keeps_them():
    db = _import_db(found=1)
    out = sp.import_items(UID, _file(_set_item(objectids=["B1", "B2", "B3"])), wdb=db)
    assert out["ok"] and out["imported"] == 1 and out["objects_not_found"] == {"houses": 2}
    assert _inserted(db)[0]["objectids"] == ["B1", "B2", "B3"]


def test_import_database_error_writes_nothing():
    def fail(text, params):
        raise RuntimeError("connection lost")
    db = FakeDb({"database_srs": [{"srid": 25832}], "INSERT INTO": fail, "SELECT name FROM": []})
    out = sp.import_items(UID, _file(_set_item()), wdb=db)
    assert out == {"ok": False, "imported": 0, "replaced": [], "skipped": [], "error": "connection lost"}


def test_export_then_import_round_trip():
    ts = datetime.datetime(2026, 11, 1, 8, 0, tzinfo=datetime.timezone.utc)
    rows = [{"name": "houses", "label": "H", "kind": "featureset", "class": "FeatureSet", "color": "#123456",
             "alpha": 0.3, "clamp": False, "is_visible": False, "data": {"count": 2}, "created_at": ts,
             "updated_at": ts, "geom": None, "objectids": ["B1", "B2"]}]
    exported = sp.export_items(UID, db=FakeDb({"database_srs": [{"srid": 25832}], "ST_AsEWKT": rows}))
    db = _import_db(found=2)
    out = sp.import_items(OTHER, json.dumps(exported), wdb=db)
    assert out == {"ok": True, "imported": 1, "replaced": [], "skipped": []}
    row = _inserted(db)[0]
    assert (row["user_id"], row["name"], row["label"], row["alpha"], row["is_visible"]) == \
        (OTHER, "houses", "H", 0.3, False)


# ── web UI side ───────────────────────────────────────────────────────────────

def test_import_notice_texts():
    from webui import app

    ok = app._import_notice({"ok": True, "imported": 2, "replaced": ["a"], "skipped": [],
                             "objects_not_found": {"s": 3}})
    assert not ok["error"] and "Imported 2 item(s), 1 replaced" in ok["text"] and "3 object id(s)" in ok["text"]
    bad = app._import_notice({"ok": False, "error": "not a JSON file"})
    assert bad["error"] and "not a JSON file" in bad["text"]
    skipped = app._import_notice({"ok": True, "imported": 0, "replaced": [],
                                  "skipped": [{"name": "x", "reason": "kind"}]})
    assert skipped["error"] and "Skipped 1" in skipped["text"]
