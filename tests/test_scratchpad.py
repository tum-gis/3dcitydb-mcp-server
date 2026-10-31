"""Tests for the spatial scratchpad: saving, actions, the SQL guard, geometry
shortening in run_query, the MCP tools and the web UI side (backends, payload).

The database is faked; the isolation itself (query role, view) is checked live,
see production/docs/scratchpad.md.

Usage:
    pytest tests/test_scratchpad.py -v
"""

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

from citydb_mcp.tools import runtime_tools  # noqa: E402
from citydb_mcp.tools import scratchpad as sp  # noqa: E402

UID = "11111111-1111-4111-8111-111111111111"


class FakeDb:
    """Answers by the first matching key in ``replies`` (substring of the SQL,
    so list the more specific keys first)."""

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


def _shape_reply(gtype="POINT", ndims=2):
    """Fake for _SHAPE_SQL: what the geometry looks like after the expression
    in the query (lift / extrude / flatten), the EWKB marks whether it changed."""
    def reply(text, params):
        g, n, closed = gtype, ndims, False
        if "CG_Extrude" in text:
            g, n, closed = "POLYHEDRALSURFACE", 3, gtype in ("POLYGON", "MULTIPOLYGON")
        elif "ST_Force3DZ" in text:
            n = 3
        elif "ST_Force2D" in text:
            n = 2
        changed = any(op in text for op in ("CG_Extrude", "ST_Force3DZ", "ST_Force2D"))
        return [{"vertices": 5, "gtype": g, "ndims": n, "members": [g], "closed": closed,
                 "shape_ewkb": "changed" if changed else params["ewkb"]}]
    return reply


@pytest.fixture(autouse=True)
def _enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "true")
    # never write to a real audit log from the tests
    monkeypatch.setenv("CITYDB_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))


def _audit_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ── save_item ─────────────────────────────────────────────────────────────────

def test_save_featureset_reruns_the_sql_as_the_user():
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}],
                  "array_agg(DISTINCT": [{"n": 2, "ids": ["B1", "B2"]}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "affected_2013", "SELECT f.objectid FROM feature f;",
                       color="#dc2626", qdb=qdb, wdb=wdb)
    assert out == {"ok": True, "name": "affected_2013", "kind": "featureset", "class": "FeatureSet",
                   "count": 2, "type": "objectids"}
    assert all(c[2] == {"citydb.user_id": UID} for c in qdb.calls)  # read as the user
    upsert = wdb.calls[-1]
    assert "ON CONFLICT" in upsert[0]
    assert upsert[1][:6] == (UID, "affected_2013", None, "featureset", "FeatureSet", "#dc2626")
    assert upsert[1][9] == ["B1", "B2"]


def test_save_geometry_keeps_it_out_of_the_model():
    qdb = FakeDb({"shape_ewkb": _shape_reply("POINT", 3),
                  "ST_HasArc": [{"n": 1, "vertices": 4, "ewkb": "01ab"}],
                  "LIMIT 1": [{"geom": "<geometry>"}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 3}]})
    out = sp.save_item(UID, "my_house", "SELECT geom FROM x", label="My house", size_m=2, qdb=qdb, wdb=wdb)
    assert out["ok"] and out["kind"] == "geometry" and out["type"] == "POINT Z" and out["class"] == "3Dmarker"
    params = wdb.calls[-1][1]
    assert params[3] == "geometry" and params[7] == "01ab" and params[9] is None
    assert params[5] == sp.DEFAULT_COLOR  # no colour given
    assert json.loads(params[10])["size_m"] == 2.0


@pytest.mark.parametrize("name,sql,error", [
    ("bad name", "SELECT 1 AS objectid", "invalid name"),
    ("ok", "DELETE FROM feature", "only SELECT"),
    ("ok", "SELECT 1 AS objectid; SELECT 2", "single statement"),
    ("ok", "SELECT * FROM citydb.scratchpad", "my_scratchpad"),
    ("ok", "SELECT set_config('citydb.user_id', 'x', false) AS objectid", "my_scratchpad"),
])
def test_save_rejects_bad_input(name, sql, error):
    out = sp.save_item(UID, name, sql, qdb=FakeDb(), wdb=FakeDb())
    assert not out["ok"] and error in out["error"]


def test_save_needs_a_user_and_a_known_column():
    assert "user id" in sp.save_item("not-a-uuid", "x", "SELECT 1", qdb=FakeDb(), wdb=FakeDb())["error"]
    qdb = FakeDb({"LIMIT 1": [{"n": 1}]})
    assert "geom column" in sp.save_item(UID, "x", "SELECT 1 AS n", qdb=qdb, wdb=FakeDb())["error"]
    assert "no rows" in sp.save_item(UID, "x", "SELECT 1", qdb=FakeDb(), wdb=FakeDb())["error"]


def test_save_enforces_limits():
    qdb = FakeDb({"ST_HasArc": [{"n": 1, "vertices": sp.MAX_VERTICES + 1, "ewkb": "01"}],
                  "LIMIT 1": [{"geom": "g"}]})
    assert "too large" in sp.save_item(UID, "x", "SELECT g AS geom", qdb=qdb, wdb=FakeDb())["error"]
    qdb = FakeDb({"LIMIT 1": [{"objectid": "a"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["a"]}]})
    wdb = FakeDb({"count(*) AS n": [{"n": sp.MAX_ITEMS}]})
    assert "full" in sp.save_item(UID, "x", "SELECT 'a' AS objectid", qdb=qdb, wdb=wdb)["error"]


# ── actions, purge ────────────────────────────────────────────────────────────

def test_actions_report_found_and_missing():
    wdb = FakeDb({"UPDATE": [{"name": "a"}]})
    out = sp.apply_action(UID, "hide", ["a", "b", "a", "no space"], wdb=wdb)
    assert out == {"ok": True, "action": "hide", "found": ["a"], "missing": ["b"]}
    assert wdb.calls[0][1] == (False, UID, ["a", "b"])
    assert sp.apply_action(UID, "show", ["a"], wdb=FakeDb({"UPDATE": []}))["missing"] == ["a"]
    wdb = FakeDb({"DELETE": [{"name": "a"}]})
    assert sp.apply_action(UID, "delete", ["a"], wdb=wdb)["found"] == ["a"]
    assert not sp.apply_action(UID, "drop", ["a"], wdb=FakeDb())["ok"]


def test_delete_user_and_touch_only_for_valid_ids():
    wdb = FakeDb({"DELETE": [{"?column?": 1}, {"?column?": 1}]})
    assert sp.delete_user(UID, wdb=wdb) == 2
    assert sp.delete_user("../x", wdb=wdb) == 0
    sp.touch_user("nope", wdb=wdb)
    assert len(wdb.calls) == 1


# ── guard, settings, run_query ────────────────────────────────────────────────

def test_guard_and_settings():
    assert sp.guard_sql("SELECT * FROM my_scratchpad") is None
    assert sp.guard_sql("select * from SCRATCHPAD") is not None
    assert sp.guard_sql("SELECT current_setting('x')") is not None
    assert sp.user_settings(UID) == {"citydb.user_id": UID}
    assert sp.user_settings("x") == {}


def test_run_query_applies_settings_guard_and_shortens_geometries():
    point = "01010000A0E864000000000000000024400000000000003440000000000000F03F"
    db = FakeDb({"SELECT": [{"objectid": "B1", "geom": point, "name": "0123"}]})
    out = runtime_tools.run_query(db, "SELECT objectid, geom FROM x", settings={"citydb.user_id": UID})
    assert out["results"][0]["geom"] == "<geometry POINT Z, 33 B>"
    assert out["results"][0]["name"] == "0123"  # short strings stay
    assert db.calls[0][2] == {"citydb.user_id": UID}
    blocked = runtime_tools.run_query(db, "SELECT * FROM scratchpad", guard=sp.guard_sql)
    assert not blocked["success"] and "my_scratchpad" in blocked["error"]


def test_describe_ewkb_handles_iso_z_and_garbage():
    iso_polyz = "01" + (1003).to_bytes(4, "little").hex() + "00" * 20
    assert runtime_tools._describe_ewkb(iso_polyz).startswith("<geometry POLYGON Z")
    assert runtime_tools._describe_ewkb("01" + "ff" * 30) is None
    assert runtime_tools._describe_ewkb("hello world, definitely not a geometry value") is None


# ── web UI side ───────────────────────────────────────────────────────────────

def _raw(rows):
    return json.dumps({"success": True, "results": rows, "row_count": len(rows)})


def test_note_scratch_result_collects_focus():
    from webui import llm_utils

    focus = []
    assert llm_utils.note_scratch_result(json.dumps({"scratchpad": {"ok": True, "name": "pin",
                                                                    "kind": "geometry"}}), focus)
    assert llm_utils.note_scratch_result(json.dumps({"scratchpad": [
        {"ok": True, "action": "goto", "found": ["a"]}, {"ok": False, "action": "hide"}]}), focus)
    assert llm_utils.note_scratch_result(json.dumps({"scratchpad": {"ok": True, "name": "set",
                                                                    "kind": "featureset"}}), focus)
    assert not llm_utils.note_scratch_result(_raw([{"n": 1}]), focus)
    assert focus == ["pin", "a", "set"]


def test_prompt_block_only_when_enabled(monkeypatch):
    from webui import llm_utils

    monkeypatch.setattr(llm_utils, "FLOOD_SCENE_ENABLED", False)
    assert llm_utils.extra_prompt_blocks().startswith("[SCRATCHPAD]")
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "false")
    assert llm_utils.extra_prompt_blocks() == ""


def test_list_items_shapes_the_viewer_payload():
    db = FakeDb({"FROM": [{"name": "a", "label": None, "kind": "geometry", "color": "#123456", "alpha": 0.5,
                           "clamp": False, "is_visible": True, "gtype": "POINT", "objectids": None,
                           "count": None, "parts": [{"type": "Point", "coordinates": [13.4, 48.5, 300]}],
                           "west": 13.4, "south": 48.5, "east": 13.4, "north": 48.5,
                           "zmin": 300, "zmax": 300}]})
    item = sp.list_items(db, UID)[0]
    assert item["visible"] and item["parts"][0]["type"] == "Point" and item["objectids"] == []
    assert item["bbox"] == [13.4, 48.5, 13.4, 48.5, 300, 300]
    assert db.calls[0][1] == (UID,)
    assert sp.list_items(db, "x") == []


# ── classes (vocabulary of the original design + FeatureSet) ─────────────────

@pytest.mark.parametrize("args,expected", [
    (("POINT", 3), "3Dmarker"), (("POINT", 2), "2Dmarker"), (("MULTIPOINT", 2), "2Dmarker"),
    (("LINESTRING", 3), "LineString3D"), (("LINESTRING", 3, (), False, True), "LineString2D"),
    (("MULTILINESTRING", 2), "MultiLineString2D"), (("POLYGON", 3), "Polygon3D"),
    (("MULTIPOLYGON", 2), "MultiPolygon2D"),
    (("POLYHEDRALSURFACE", 3, (), True), "Solid3D"), (("POLYHEDRALSURFACE", 3, (), False), "Surface3D"),
    (("GEOMETRYCOLLECTION", 3, ("POLYHEDRALSURFACE",), True), "MultiSolid3D"),
    (("GEOMETRYCOLLECTION", 3, ("POLYGON",), False), "MultiPolygon3D"),
])
def test_classify_derives_the_class(args, expected):
    assert sp.classify(*args) == expected


def test_classify_checks_a_requested_class():
    assert sp.classify("POINT", 3, requested="Point3D") == "Point3D"
    with pytest.raises(ValueError, match="does not fit"):
        sp.classify("POINT", 3, requested="Solid3D")
    with pytest.raises(ValueError, match="unknown class"):
        sp.classify("POINT", 3, requested="Blob")
    with pytest.raises(ValueError, match="mixed"):
        sp.classify("GEOMETRYCOLLECTION", 3, ("POINT", "POLYGON"))
    with pytest.raises(ValueError, match="needs a geometry with heights"):
        sp.classify("POINT", 2, requested="3Dmarker")
    assert sp.classify("POINT", 3, requested="2Dmarker") == "2Dmarker"  # _shape then drops the heights
    assert set(sp.CLASSES) - set(sp.GEOMETRY_CLASSES) == {"FeatureSet"} and len(sp.GEOMETRY_CLASSES) == 15


def test_featureset_rejects_a_geometry_class():
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    out = sp.save_item(UID, "s", "SELECT 'B1' AS objectid", item_class="Solid3D", qdb=qdb, wdb=FakeDb())
    assert not out["ok"] and "needs a geom column" in out["error"]


# ── MCP tools ─────────────────────────────────────────────────────────────────

def test_geojson_becomes_quoted_literals():
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"note": "it's"}, "geometry": {"type": "Point", "coordinates": [13.4, 48.5]}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.5, 48.6]}}]}
    sql = sp._geojson_sql(fc)
    assert sql.count("ST_GeomFromGeoJSON") == 1 and sql.count('"Point"') == 2
    assert "it's" not in sql  # properties are not passed on
    assert "''" in sp._geojson_sql({"type": "Point", "coordinates": [1, 2], "x": "a'b"})
    for bad in ("not json", {"type": "FeatureCollection", "features": []}, [1, 2]):
        with pytest.raises(ValueError):
            sp._geojson_sql(bad)


def test_run_tool_routes_and_validates(monkeypatch):
    seen = {}
    monkeypatch.setattr(sp, "save_item", lambda u, n, q, **kw: seen.update(user=u, name=n, sql=q, **kw)
                        or {"ok": True, "name": n})
    monkeypatch.setattr(sp, "apply_action", lambda u, a, n, **kw: {"ok": True, "action": a, "found": n, **kw})
    out = sp.run_tool("save_to_scratchpad", {"name": "p", "geojson": {"type": "Point", "coordinates": [1, 2]},
                                             "class": "Point3D", "size_m": "3", "clamp": "true"}, UID)
    assert out["scratchpad"]["ok"] and seen["user"] == UID and "ST_GeomFromGeoJSON" in seen["sql"]
    assert seen["item_class"] == "Point3D" and seen["size_m"] == 3.0 and seen["clamp"] is True
    assert not sp.run_tool("save_to_scratchpad", {"name": "p"}, UID)["scratchpad"]["ok"]
    assert not sp.run_tool("save_to_scratchpad", {"name": "p", "sql": "SELECT 1", "geojson": "{}"}, UID)["scratchpad"]["ok"]
    assert sp.run_tool("scratchpad_action", {"action": "hide", "names": "a"}, UID)["scratchpad"]["found"] == ["a"]
    assert sp.run_tool("scratchpad_action", {"action": "delete", "names": ["a", "b"], "confirm": "true"},
                       UID)["scratchpad"]["confirm"] is True
    with pytest.raises(ValueError):
        sp.run_tool("drop_everything", {}, UID)


def test_mcp_clients_without_a_session_share_one_user(monkeypatch):
    assert sp.effective_user_id(UID) == UID
    default = sp.effective_user_id(None)
    assert sp.user_settings(default) and default == sp.effective_user_id("")
    monkeypatch.setattr(sp, "_MCP_CLIENT_USER", "22222222-2222-4222-8222-222222222222")
    assert sp.effective_user_id(None) == "22222222-2222-4222-8222-222222222222"


def test_tool_specs_only_when_the_scratchpad_is_on(monkeypatch):
    from webui import llm_utils

    names = [t["function"]["name"] for t in llm_utils.scratchpad_tool_specs()]
    assert names == ["save_to_scratchpad", "scratchpad_action", "update_scratchpad_item", "list_scratchpad"]
    assert "save_to_scratchpad" in llm_utils.extra_prompt_blocks()
    assert "scratch_save" not in llm_utils.extra_prompt_blocks()  # the column convention is gone
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "false")
    assert llm_utils.scratchpad_tool_specs() == []


def test_scratch_tool_result_and_intent():
    from webui import llm_utils

    ok = llm_utils.scratch_tool_result(json.dumps({"scratchpad": {"ok": True, "name": "a", "count": 3}}))
    assert ok["error"] is None and ok["preview_rows"][0]["name"] == "a" and ok["all_rows"] == []
    bad = llm_utils.scratch_tool_result(json.dumps({"scratchpad": {"ok": False, "error": "invalid name"}}))
    assert bad["error"] == "invalid name"
    listed = llm_utils.scratch_tool_result(json.dumps({"items": [{"name": "a"}, {"name": "b"}], "count": 2}))
    assert listed["row_count"] == 2
    assert llm_utils.wants_scratchpad("Speichere die Gebäude als x") and llm_utils.wants_scratchpad("hide x")
    assert not llm_utils.wants_scratchpad("Wie viele Gebäude gibt es?")


def test_cloud_backend_dispatches_a_scratchpad_tool(monkeypatch):
    from types import SimpleNamespace as NS
    from webui.backends import cloud

    replies = [
        NS(choices=[NS(message=NS(content="", tool_calls=[NS(id="c1", function=NS(
            name="save_to_scratchpad", arguments=json.dumps({"name": "s", "sql": "SELECT 1 AS objectid"})))]))]),
        NS(choices=[NS(message=NS(content="Saved s (1 object).", tool_calls=None))]),
    ]
    sent = []
    monkeypatch.setattr(cloud, "_litellm_kwargs", lambda *a, **k: {"model": "m"})
    monkeypatch.setattr(cloud, "_log_context_usage", lambda *a, **k: (0, 1000, None))
    monkeypatch.setattr(cloud, "safe_completion", lambda kw, **k: sent.append(k) or replies.pop(0))
    ran = []
    extra = {"save_to_scratchpad": {"spec": {"type": "function", "function": {"name": "save_to_scratchpad"}},
                                    "run": lambda a: ran.append(a) or json.dumps(
                                        {"scratchpad": {"ok": True, "name": "s", "count": 1}})}}
    events = list(cloud.native_tool_stream("openai", "m", None, [{"role": "user", "content": "speichere s"}],
                                           lambda sql: pytest.fail("run_query must not run"),
                                           extra_tools=extra))
    assert ran == [{"name": "s", "sql": "SELECT 1 AS objectid"}]
    assert [t["function"]["name"] for t in sent[0]["tools"]] == ["run_query", "save_to_scratchpad"]
    calls = [d for e, d in events if e == "tool_call"]
    assert calls[0]["tool"] == "save_to_scratchpad"
    assert ("final", "Saved s (1 object).") in events


def test_react_parser_accepts_extra_tools_and_json_input():
    pytest.importorskip("langchain")
    from webui.backends import local

    assert local._parse_json_input('```json\n{"name": "a", "geojson": {"type": "Point"}}\n```') == {
        "name": "a", "geojson": {"type": "Point"}}
    with pytest.raises(ValueError):
        local._parse_json_input("save it please")

    class _Cb:
        _parse_errors = 0
        _tools_called = 0
        _consecutive_parse_failures = 0

    parser = local._RobustReActParser(_Cb(), user_question="speichere a",
                                      tool_names={"run_query", "save_to_scratchpad"})
    action = parser._parse_impl('Thought: save it\nAction: save_to_scratchpad\n'
                                'Action Input: {"name": "a", "sql": "SELECT f.objectid FROM feature f"}')
    assert action.tool == "save_to_scratchpad" and '"name": "a"' in action.tool_input


# ── update in place, bulk-delete confirmation, audit log ─────────────────────

def _item(gtype="POINT", ndims=3, ground=300.0, **kw):
    row = {"kind": "geometry", "clamp": False, "size_m": None, "ewkb": "stored"}
    row.update(kw)
    return FakeDb({"shape_ewkb": _shape_reply(gtype, ndims), "AS ground": [{"ground": ground}],
                   "AS size_m, encode": [row]})


def test_update_changes_only_given_fields_and_checks_the_class(tmp_path):
    wdb = _item()
    out = sp.update_item(UID, "pin", color="#00ff00", item_class="Point3D", size_m=5, label="", wdb=wdb)
    assert out == {"ok": True, "name": "pin", "updated": {"label": None, "color": "#00ff00",
                                                          "class": "Point3D", "size_m": 5.0}}
    query, params, _ = wdb.calls[-1]
    assert "UPDATE" in query and "jsonb_build_object('size_m'" in query
    assert params == (None, "#00ff00", "Point3D", 5.0, UID, "pin")
    assert _audit_lines(tmp_path)[-1]["op"] == "update"
    assert "does not fit" in sp.update_item(UID, "pin", item_class="Solid3D", wdb=_item())["error"]
    assert "stays FeatureSet" in sp.update_item(UID, "s", item_class="Point3D",
                                                wdb=_item(kind="featureset"))["error"]
    assert "#rrggbb" in sp.update_item(UID, "pin", color="green", wdb=_item())["error"]
    assert "nothing to change" in sp.update_item(UID, "pin", wdb=_item())["error"]
    assert "no saved item" in sp.update_item(UID, "gone", color="#000000", wdb=FakeDb())["error"]


def test_clamp_change_reclassifies_and_drops_the_heights():
    out = sp.update_item(UID, "line", clamp=True, wdb=_item(gtype="LINESTRING"))
    assert out["updated"] == {"class": "LineString2D", "geometry": "LINESTRING", "clamp": True}


# ── 2D -> 3D: class and stored geometry always match ─────────────────────────

def _lift_to(db):
    return next(p["lift_to"] for _, p, _ in db.calls if isinstance(p, dict) and "lift_to" in p)


def test_height_turns_a_2d_marker_into_a_3d_marker(tmp_path):
    wdb = _item("POINT", 2, ground=295.0)
    out = sp.update_item(UID, "pin", height_m=20, wdb=wdb)
    assert out == {"ok": True, "name": "pin", "updated": {"class": "3Dmarker", "geometry": "POINT Z"}}
    assert _lift_to(wdb) == 315.0  # ground + height
    query, params, _ = wdb.calls[-1]
    assert "geom = ST_GeomFromEWKB" in query and params == ("3Dmarker", "changed", UID, "pin")


def test_a_3d_class_alone_lifts_onto_the_ground_and_a_flat_class_flattens():
    wdb = _item("POINT", 2, ground=295.0)
    assert sp.update_item(UID, "pin", item_class="3Dmarker", wdb=wdb)["updated"]["geometry"] == "POINT Z"
    assert _lift_to(wdb) == 295.0
    out = sp.update_item(UID, "pin", item_class="2Dmarker", wdb=_item("POINT", 3))
    assert out["updated"] == {"class": "2Dmarker", "geometry": "POINT"}
    # a sphere put on the ground rests on it (centre one radius up)
    wdb = _item("POINT", 2, ground=295.0)
    sp.update_item(UID, "pin", item_class="Point3D", size_m=10, wdb=wdb)
    assert _lift_to(wdb) == 305.0


def test_an_area_becomes_a_solid_with_extrude_m():
    err = sp.update_item(UID, "bbox", item_class="Solid3D", wdb=_item("POLYGON", 2))["error"]
    assert "extrude_m" in err
    wdb = _item("POLYGON", 2, ground=310.0)
    out = sp.update_item(UID, "bbox", extrude_m=25, wdb=wdb)
    assert out["updated"] == {"class": "Solid3D", "geometry": "POLYHEDRALSURFACE Z"}
    shape_call = next((q, p) for q, p, _ in wdb.calls if "CG_Extrude" in q)
    assert shape_call[1]["extrude"] == 25.0 and shape_call[1]["lift_to"] == 310.0
    # a line becomes a wall (open surface)
    assert sp.update_item(UID, "l", extrude_m=3, wdb=_item("LINESTRING", 2))["updated"]["class"] == "Surface3D"


def test_height_errors_and_base_z():
    assert "cannot be extruded" not in sp.update_item(UID, "p", height_m=5, wdb=_item("POINT", 2)).get("error", "")
    assert "only areas and lines" in sp.update_item(UID, "p", extrude_m=5, wdb=_item("POINT", 2))["error"]
    assert "is flat" in sp.update_item(UID, "p", height_m=5, item_class="2Dmarker", wdb=_item())["error"]
    assert "positive" in sp.update_item(UID, "a", extrude_m=-1, wdb=_item("POLYGON", 2))["error"]
    assert "give base_z" in sp.update_item(UID, "p", height_m=5, wdb=_item("POINT", 2, ground=None))["error"]
    wdb = _item("POINT", 2, ground=None)
    assert sp.update_item(UID, "p", base_z=350, wdb=wdb)["ok"] and _lift_to(wdb) == 350.0
    assert not any("AS ground" in q for q, _, _ in wdb.calls)  # no ground estimate needed
    assert "no geometry of its own" in sp.update_item(UID, "s", height_m=5, wdb=_item(kind="featureset"))["error"]


def test_save_with_extrude_and_a_featureset_with_height():
    qdb = FakeDb({"shape_ewkb": _shape_reply("POLYGON", 2), "AS ground": [{"ground": 300.0}],
                  "ST_HasArc": [{"n": 1, "vertices": 5, "ewkb": "poly"}], "LIMIT 1": [{"geom": "g"}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "block", "SELECT ST_Extent(envelope) AS geom FROM feature", extrude_m=10,
                       qdb=qdb, wdb=wdb)
    assert out["class"] == "Solid3D" and out["type"] == "POLYHEDRALSURFACE Z"
    assert wdb.calls[-1][1][7] == "changed"  # the extruded geometry is stored
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    out = sp.save_item(UID, "s", "SELECT 'B1' AS objectid", height_m=5, qdb=qdb, wdb=FakeDb())
    assert "no geometry of its own" in out["error"]


@pytest.mark.parametrize("value,expected", [(20, 20.0), ("20", 20.0), ("20 m", 20.0), ("1,5", 1.5),
                                            (None, None), ("", None), (-3, -3.0)])
def test_number_arguments(value, expected):
    assert sp._number({"h": value}, "h") == expected


@pytest.mark.parametrize("value", ["abc", True, "20 km"])
def test_number_arguments_rejected(value):
    with pytest.raises(ValueError):
        sp._number({"h": value}, "h")


def test_height_parameters_reach_the_tools(monkeypatch):
    seen = {}
    monkeypatch.setattr(sp, "update_item", lambda *a, **kw: seen.update(kw) or {"ok": True})
    sp.run_tool("update_scratchpad_item", {"name": "pin", "height_m": "20 m"}, UID)
    assert seen["height_m"] == 20.0 and seen["extrude_m"] is None
    out = sp.run_tool("update_scratchpad_item", {"name": "pin", "height_m": "high"}, UID)
    assert not out["scratchpad"]["ok"]
    for spec in ("save_to_scratchpad", "update_scratchpad_item"):
        assert {"height_m", "extrude_m", "base_z"} <= set(sp.TOOL_SCHEMAS[spec]["inputSchema"]["properties"])


def test_deleting_several_items_needs_confirmation(tmp_path):
    out = sp.apply_action(UID, "delete", ["a", "b"], wdb=FakeDb())
    assert not out["ok"] and out["needs_confirmation"] and out["names"] == ["a", "b"]
    wdb = FakeDb({"DELETE": [{"name": "a"}, {"name": "b"}]})
    assert sp.apply_action(UID, "delete", ["a", "b"], wdb=wdb, confirm=True)["found"] == ["a", "b"]
    assert sp.apply_action(UID, "delete", ["a"], wdb=FakeDb({"DELETE": [{"name": "a"}]}))["ok"]
    assert [line["op"] for line in _audit_lines(tmp_path)] == ["delete", "delete"]
    out = sp.run_tool("scratchpad_action", {"action": "delete", "names": ["a", "b"]}, UID)
    assert out["scratchpad"]["needs_confirmation"]


def test_audit_records_saves_and_purges(tmp_path):
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    sp.save_item(UID, "s", "SELECT 'B1' AS objectid", qdb=qdb, wdb=FakeDb({"count(*) AS n": [{"n": 0}]}))
    sp.delete_user(UID, wdb=FakeDb({"DELETE": [{"name": "s"}]}))
    sp.apply_action(UID, "goto", ["s"], wdb=FakeDb({"UPDATE": [{"name": "s"}]}))  # not a change
    lines = _audit_lines(tmp_path)
    assert [(line["op"], line["user_id"]) for line in lines] == [("save", UID), ("purge", UID)]
    assert lines[0]["class"] == "FeatureSet" and lines[0]["count"] == 1 and lines[1]["names"] == ["s"]


def test_audit_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("CITYDB_MCP_AUDIT_LOG", "")
    sp.apply_action(UID, "hide", ["a"], wdb=FakeDb({"UPDATE": [{"name": "a"}]}))
    assert not (tmp_path / "audit.jsonl").exists()


def test_confirmation_hint_tells_the_model_to_ask():
    from webui import llm_utils

    data = llm_utils.scratch_tool_result(json.dumps({"scratchpad": sp.apply_action(UID, "delete", ["a", "b"],
                                                                                   wdb=FakeDb())}))
    assert data["needs_confirmation"] and "Ask the user" in llm_utils.scratch_tool_hint(data)
    assert "Fix the arguments" in llm_utils.scratch_tool_hint({"error": "x"})


# ── long results: 5 rows in the answer, the full list attached ───────────────

def test_full_result_is_attached_only_for_long_results():
    from webui import llm_utils

    rows = [{"objectid": f"B{i}", "name": "a,b" if i == 0 else f"n{i}"} for i in range(12)]
    out = llm_utils.format_full_result(_raw(rows))
    assert out.count("<details data-full-result>") == 2 and "(12 rows)" in out
    assert '"a,b"' in out and "objectid,name" in out  # CSV quoting
    assert llm_utils.format_full_result(_raw(rows[:10])) == ""
    assert llm_utils.format_full_result("no json") == ""


def test_attached_list_never_goes_back_to_the_model():
    from webui import chat_runtime, llm_utils

    answer = "Here are 5 of 12 buildings:\n\n| objectid |\n|---|\n| B0 |"
    attached = answer + llm_utils.format_full_result(_raw([{"objectid": f"B{i}"} for i in range(12)]))
    assert llm_utils.strip_full_result(attached) == answer
    messages = chat_runtime.build_messages("next question", [["show me", attached]], "SCHEMA")
    assert all("data-full-result" not in m["content"] for m in messages)
    assert any(m["role"] == "assistant" and m["content"] == answer for m in messages)


def test_long_result_note_only_for_short_lists_above_the_limit():
    from webui import llm_utils

    assert llm_utils.long_result_note(64) == ""  # default: every row is listed
    assert llm_utils.long_result_note(10, full_lists=False) == ""
    assert "5 of 64" in llm_utils.long_result_note(64, full_lists=False)


def test_list_mode_picks_the_table_rules():
    from webui import llm_utils

    full, short = llm_utils.chat_instructions(True), llm_utils.chat_instructions(False)
    assert "@@" not in full and "@@" not in short
    assert llm_utils.CHAT_INSTRUCTIONS == full
    assert "MUST contain EVERY row returned by the query" in full and "FIRST 5 rows" not in full
    assert "FIRST 5 rows" in short and "5 of N" in short
    # the rest of the instructions is identical
    assert full.split("RULE 1:")[0] == short.split("RULE 1:")[0]
    assert full.split("RULE 7:")[1] == short.split("RULE 7:")[1]


def test_list_mode_reaches_the_prompt_and_the_backend(monkeypatch):
    from webui import chat_runtime

    msgs = chat_runtime.build_messages("q", [], "db", full_lists=False)
    assert "FIRST 5 rows" in msgs[0]["content"]
    assert "FIRST 5 rows" not in chat_runtime.build_messages("q", [], "db")[0]["content"]
    seen = {}

    def fake_stream(*args, **kwargs):
        seen.update(kwargs)
        yield "final", "ok"

    import webui.backends as backends
    monkeypatch.setattr(backends, "stream", fake_stream)
    settings = chat_runtime.ChatTurnSettings(provider="openai", model="m", full_lists=False)
    list(chat_runtime.stream_chat_turn(settings, [], lambda sql: ""))
    assert seen["full_lists"] is False
    assert chat_runtime.ChatTurnSettings(provider="openai", model="m").full_lists is True
