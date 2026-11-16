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
    ("!!!", "SELECT 1 AS objectid", "invalid name"),
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


def test_save_refuses_a_line_of_length_zero():
    qdb = FakeDb({"ST_HasArc": [{"n": 1, "vertices": 2, "zero_line": True, "ewkb": "01"}],
                  "LIMIT 1": [{"geom": "g"}]})
    out = sp.save_item(UID, "line", "SELECT g AS geom", qdb=qdb, wdb=FakeDb())
    assert not out["ok"] and "ST_3DShortestLine(a, b) itself" in out["error"]
    assert "ST_3DLength(g) = 0" in sp._GEOM_SQL


def test_save_keeps_a_3d_item_far_below_the_ground_with_a_height_hint():
    """Markers at z = height_m (28 m) in a city whose ground is at 499 m: saved
    (tunnels or pipes may lie that deep), with a hint how to fix the height."""
    def qdb(zmax):
        return FakeDb({"ST_HasArc": [{"n": 1, "vertices": 1, "ndims": 3, "zmax": zmax, "ewkb": "01"}],
                       "AS ground": [{"ground": 499.0}], "shape_ewkb": _shape_reply("POINT", 3),
                       "LIMIT 1": [{"geom": "g"}]})
    out = sp.save_item(UID, "m", "SELECT g AS geom", item_class="3Dmarker", qdb=qdb(28.6),
                       wdb=FakeDb({"count(*) AS n": [{"n": 0}]}))
    assert out["ok"], out
    assert "470 m below" in out["note"] and "ST_ZMax(s.envelope)" in out["note"]
    assert "same name" in out["note"] and "underground, keep it" in out["note"]
    out = sp.save_item(UID, "m", "SELECT g AS geom", item_class="3Dmarker", qdb=qdb(527.9),
                       wdb=FakeDb({"count(*) AS n": [{"n": 0}]}))
    assert out["ok"] and "below" not in out.get("note", ""), out


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


@pytest.mark.parametrize("gtype,ndims,requested,expected", [
    ("MULTIPOLYGON", 2, "Polygon2D", "MultiPolygon2D"),  # e.g. CG_OptimalAlphaShape
    ("MULTIPOLYGON", 3, "Polygon3D", "MultiPolygon3D"),
    ("MULTILINESTRING", 3, "LineString3D", "MultiLineString3D"),  # e.g. ST_Collect of rings
    ("MULTILINESTRING", 2, "LineString2D", "MultiLineString2D"),
    ("POLYGON", 2, "Polygon2D", "Polygon2D"),
])
def test_classify_promotes_a_single_class_to_its_multi_variant(gtype, ndims, requested, expected):
    assert sp.classify(gtype, ndims, requested=requested) == expected


@pytest.mark.parametrize("gtype", ["POLYHEDRALSURFACE", "TIN", "GEOMETRYCOLLECTION"])
def test_flat_class_on_a_3d_surface_explains_the_2d_way(gtype):
    with pytest.raises(ValueError) as err:
        sp.classify(gtype, 3, requested="Polygon2D")
    text = str(err.value)
    assert f"class Polygon2D is 2D, but the SELECT returned a 3D {gtype}" in text
    assert "CG_OptimalAlphaShape(ST_Force2D(" in text and "ST_ConcaveHull" in text
    assert "ST_Buffer(ST_Force2D(" in text and "save again" in text
    # other mismatches keep the short message
    with pytest.raises(ValueError, match=r"^class Solid3D does not fit a POINT geometry$"):
        sp.classify("POINT", 3, requested="Solid3D")


@pytest.mark.parametrize("value", ["abc", "", True, -0.1, 2000, "150%", None])
def test_alpha_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="alpha"):
        sp._check_alpha(value)


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


@pytest.mark.parametrize("question", [
    "Most buildings have energy classes. Create one group per class and put all corresponding buildings in them.",
    "Create a single 3D alpha-wrapping around the residential buildings of Röblingweg and show it in red.",
    'Extract all points from the lod2Solid of building B1 and show the pointset as "cornerpoints" in green.',
    "Put a marker on top of the 5 tallest buildings with the height as labels.",
    "Add a 2m buffer (in 2D) around that alpha-wrapping.",
    "Create closed rings (3D lines) delineating the boundaries for each floor.",
    "Erstelle pro Klasse eine Gruppe und ordne alle dazugehörigen Gebäude diesen Gruppen zu.",
    "Setze auf die fünf höchsten Gebäude jeweils oben drauf einen Marker beschriftet mit der Höhe.",
    "Füge einen 2m breiten Puffer (in 2D) um diese Umhüllung hinzu.",
    "Stelle sie in gelb mit 30% Transparenz dar.",
])
def test_create_and_draw_requests_count_as_scratchpad_intent(question):
    from webui import llm_utils

    assert llm_utils.wants_scratchpad(question)
    assert "save_to_scratchpad" in llm_utils.scratch_save_reminder(question)


@pytest.mark.parametrize("question", [
    "How many buildings are there?", "Show me all residential buildings in Röblingweg",
    "List the 5 tallest buildings", "Welche Gebäude haben ein Flachdach?", "Wie hoch ist das Gebäude?",
    "What is the total roof area per street?",
    "How many points does the lod2Solid of building B1 have?", "What colours do the roofs in the dataset have?",
    "Wie viele Punkte hat das lod2Solid des Gebäudes B1?", "Welche Farben haben die Dächer im Datensatz?",
])
def test_plain_questions_are_no_scratchpad_intent(question):
    from webui import llm_utils

    assert not llm_utils.wants_scratchpad(question) and llm_utils.scratch_save_reminder(question) == ""


def test_prompt_block_teaches_compute_in_sql_and_the_recipes():
    from webui import llm_utils

    block = llm_utils.SCRATCHPAD_TOOLS_INSTRUCTIONS
    assert "PostGIS computes, the scratchpad draws" in block
    assert "alpha = 1 - X/100" in block and "30 % -> 0.7" in block
    assert "SEVERAL ITEMS = ONE CALL" in block and "item_name" in block
    assert "feature_geometry(objectid, class, property" in block
    assert "feature_boundary(objectid, class, surface_class" in block
    assert "building_solid" not in block and "building_footprint" not in block
    assert "A `label` column sets the" in block and block.count("' m' AS label") == 2
    for recipe in ("CG_3DConvexHull", "CG_OptimalAlphaShape", "ST_DumpPoints", "ST_ZMax(s.envelope)",
                   "ST_3DShortestLine", "ST_Buffer(ST_Force2D(p.geom)", "generate_series", "ST_3DExtent"):
        assert recipe in block, recipe
    assert len(block) < 6500  # weak models do worse with long prompts


def test_sfcgal_functions_that_fail_at_run_time_are_not_advertised():
    from citydb_mcp.tools import dynamic_tools

    class _Db:
        def __init__(self, works):
            self.works = works

        def execute_single(self, query, params=None):
            if not self.works:
                raise RuntimeError("compiled against SFCGAL 2.0, requires 2.1")
            return {"ok": True}

    broken = "\n".join(dynamic_tools._probe_sfcgal_optional(_Db(False)))
    assert "CG_3DAlphaWrapping is NOT available" in broken and "CG_3DConvexHull" in broken
    assert "CG_Extrude works on lines only" in broken
    working = "\n".join(dynamic_tools._probe_sfcgal_optional(_Db(True)))
    assert working.startswith("CG_3DAlphaWrapping(geom") and "NOT" not in working


def test_save_schema_has_alpha_and_says_it_draws():
    save = sp.TOOL_SCHEMAS["save_to_scratchpad"]
    assert "alpha" in save["inputSchema"]["properties"]
    assert "1 - alpha" in save["inputSchema"]["properties"]["alpha"]["description"]
    assert "DRAWS" in save["description"] and "item_name" in save["description"]
    assert "1 - alpha" in sp.TOOL_SCHEMAS["update_scratchpad_item"]["inputSchema"]["properties"]["alpha"][
        "description"]


def test_save_stores_alpha():
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "s", "SELECT 'B1' AS objectid", alpha=0.5, qdb=qdb, wdb=wdb)
    assert out["ok"] and out["alpha"] == 0.5
    upsert = wdb.calls[-1]
    assert "alpha = EXCLUDED.alpha" in upsert[0] and upsert[1][-1] == 0.5
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "s", "SELECT 'B1' AS objectid", qdb=qdb, wdb=wdb)
    assert "alpha" not in out and wdb.calls[-1][1][-1] == sp.DEFAULT_ALPHA
    bad = sp.save_item(UID, "s", "SELECT 'B1' AS objectid", alpha=2000, qdb=qdb, wdb=FakeDb())
    assert not bad["ok"] and "alpha" in bad["error"]


def test_a_label_column_sets_the_label():
    """Recipes 5/6: the height or distance is computed in the SELECT, so the label
    matches the saved marker / line (a separately looked-up value did not)."""
    qdb = FakeDb({"string_agg(DISTINCT q.label": [{"label": "38.1 m"}],
                  "LIMIT 1": [{"objectid": "B1", "label": "38.1 m"}],
                  "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "line_1", "SELECT 'B1' AS objectid, '38.1 m' AS label", label="19.7 m",
                       qdb=qdb, wdb=wdb)
    assert out["ok"] and out["label"] == "38.1 m" and wdb.calls[-1][1][2] == "38.1 m"
    # without a label column the argument is kept
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "g", "SELECT 'B1' AS objectid", label="Class A", qdb=qdb, wdb=wdb)
    assert out["label"] == "Class A" and wdb.calls[-1][1][2] == "Class A"
    assert not any("string_agg" in c[0] for c in qdb.calls)


def test_save_notes_joined_labels_and_dropped_heights():
    """gpt-oss saved the 5 tallest as ONE 2Dmarker item from a 5-row SELECT with a
    label column: the result now says that the labels were joined and the heights dropped."""
    qdb = FakeDb({"string_agg(DISTINCT q.label": [{"label": "28.0 m, 32.2 m", "labels": 2}],
                  "shape_ewkb": _shape_reply("MULTIPOINT", 3),
                  "ST_HasArc": [{"n": 2, "vertices": 2, "ndims": 3, "ewkb": "01ab"}],
                  "LIMIT 1": [{"geom": "<geometry>", "label": "32.2 m"}]})
    out = sp.save_item(UID, "top", "SELECT geom, label FROM x", item_class="2Dmarker", qdb=qdb,
                       wdb=FakeDb({"count(*) AS n": [{"n": 0}]}))
    assert out["ok"] and out["class"] == "2Dmarker"
    assert "2 different labels were joined" in out["note"] and "one item per row" in out["note"]
    assert "heights of the SELECT were dropped (use 3Dmarker" in out["note"]
    # one label, kept heights: no note
    qdb.replies["string_agg(DISTINCT q.label"] = [{"label": "32.2 m", "labels": 1}]
    out = sp.save_item(UID, "top", "SELECT geom, label FROM x", qdb=qdb,
                       wdb=FakeDb({"count(*) AS n": [{"n": 0}]}))
    assert out["ok"] and out["class"] == "3Dmarker" and "note" not in out


@pytest.mark.parametrize("raw,fixed", [
    ("Röblingweg_residential_alpha", "Roeblingweg_residential_alpha"), ("Eckpunkte grün", "Eckpunkte_gruen"),
    ("Straße-Hülle", "Strasse_Huelle"), ("5_tallest", "_5_tallest"), ("café", "cafe"),
])
def test_safe_name_transliterates(raw, fixed):
    assert sp.safe_name(raw) == fixed and sp.valid_name(fixed)


def test_save_renames_an_invalid_name_instead_of_failing():
    qdb = FakeDb({"LIMIT 1": [{"objectid": "B1"}], "array_agg(DISTINCT": [{"n": 1, "ids": ["B1"]}]})
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "Röblingweg_hull", "SELECT 'B1' AS objectid", qdb=qdb, wdb=wdb)
    assert out["ok"] and out["name"] == "Roeblingweg_hull" and "saved as 'Roeblingweg_hull'" in out["note"]
    assert wdb.calls[-1][1][1] == "Roeblingweg_hull"
    bad = sp.save_item(UID, "äöü", "SELECT 'B1' AS objectid", qdb=qdb, wdb=FakeDb())
    assert bad["ok"] and bad["name"] == "aeoeue"
    bad = sp.save_item(UID, "!!!", "SELECT 'B1' AS objectid", qdb=qdb, wdb=FakeDb())
    assert not bad["ok"] and "invalid name" in bad["error"]


@pytest.mark.parametrize("value,alpha", [(0.5, 0.5), ("0.7", 0.7), ("50%", 0.5), (30, 0.3), (1, 1.0), (0, 0.0)])
def test_alpha_accepts_fractions_and_percent(value, alpha):
    assert sp._check_alpha(value) == pytest.approx(alpha)


def test_run_tool_passes_alpha_to_save(monkeypatch):
    seen = {}
    monkeypatch.setattr(sp, "save_item", lambda u, n, q, **kw: seen.update(kw) or {"ok": True})
    sp.run_tool("save_to_scratchpad", {"name": "w", "sql": "SELECT 1 AS objectid", "alpha": 0.7}, UID)
    assert seen["alpha"] == 0.7
    sp.run_tool("save_to_scratchpad", {"name": "w", "sql": "SELECT 1 AS objectid"}, UID)
    assert seen["alpha"] is None


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


# ── Several items in one call (item_name), helper views, guards ───────────────

def _split_qdb(groups):
    """Fake read connection for a SELECT with an item_name column: ``groups``
    is [(value, color)]; every group query returns two objectids."""
    return FakeDb({"LIMIT 1": [{"objectid": "B1", "item_name": "A", "color": "#16a34a"}],
                   "GROUP BY 1 ORDER BY 1": [{"v": v, "c": c} for v, c in groups],
                   "array_agg(DISTINCT": [{"n": 2, "ids": ["B1", "B2"]}]})


def test_item_name_column_creates_one_item_per_value():
    qdb = _split_qdb([("A", "#16a34a"), ("B", "not a colour"), ("Röbling", None)])
    wdb = FakeDb({"count(*) AS n": [{"n": 0}]})
    out = sp.save_item(UID, "energy", "SELECT b.objectid, p.val_string AS item_name, '#16a34a' AS color "
                       "FROM feature b", color="#dc2626", qdb=qdb, wdb=wdb)
    assert [r["name"] for r in out] == ["energy_A", "energy_B", "energy_Roebling"]
    assert all(r["ok"] and r["count"] == 2 for r in out)
    upserts = [c[1] for c in wdb.calls if "ON CONFLICT" in c[0]]
    assert [u[5] for u in upserts] == ["#16a34a", "#dc2626", "#dc2626"]  # own colour, else the call's
    # Each item re-runs the SELECT restricted to its group, by rank, not by a quoted value.
    group_sql = [c[0] for c in qdb.calls if "item_group = " in c[0]]
    assert group_sql and all("Röbling" not in s for s in group_sql)


def test_item_name_keeps_values_that_already_carry_the_prefix():
    assert sp._group_item_name("marker", "DEBY_LOD2_1") == "marker_DEBY_LOD2_1"
    assert sp._group_item_name("line", "line_marker_X") == "line_marker_X"
    assert sp._group_item_name("energy", "32.2 m") == "energy_32_2_m"
    with pytest.raises(ValueError):
        sp._group_item_name("energy", "%%%")


def test_item_name_rejects_too_many_or_clashing_groups():
    many = _split_qdb([(str(i), None) for i in range(sp.MAX_GROUPS + 1)])
    out = sp.save_item(UID, "g", "SELECT 1 AS objectid, 1 AS item_name", qdb=many, wdb=FakeDb())
    assert not out["ok"] and "more than" in out["error"]
    clash = _split_qdb([("a b", None), ("a_b", None)])
    out = sp.save_item(UID, "g", "SELECT 1 AS objectid, 1 AS item_name", qdb=clash, wdb=FakeDb())
    assert not out["ok"] and "same item name" in out["error"]


def test_split_results_reach_the_viewer_and_the_model():
    from webui import llm_utils

    raw = json.dumps({"scratchpad": [{"ok": True, "name": "energy_A", "kind": "featureset", "count": 1},
                                     {"ok": False, "name": "energy_B", "error": "boom"}]})
    focus = []
    assert llm_utils.note_scratch_result(raw, focus) and focus == ["energy_A"]
    assert llm_utils.scratch_saved_names(raw) == ["energy_A"]
    assert llm_utils.scratch_tool_result(raw)["error"] == "boom"


def test_query_password_does_not_depend_on_the_database(monkeypatch):
    monkeypatch.delenv("CITYDB_QUERY_PASSWORD", raising=False)
    monkeypatch.setenv("CITYDB_PASSWORD", "pw")
    monkeypatch.setenv("CITYDB_NAME", "citydb")
    first = sp.query_password()
    monkeypatch.setenv("CITYDB_NAME", "harthof")
    assert sp.query_password() == first  # one role per server, shared by its databases
    monkeypatch.setenv("CITYDB_QUERY_PASSWORD", "explicit")
    assert sp.query_password() == "explicit"


def test_query_role_is_provisioned_once_when_its_login_fails(monkeypatch, capsys):
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "true")
    monkeypatch.setattr(sp, "_query_role_ok", None)
    logins = iter([RuntimeError("password authentication failed"), [{"?column?": 1}]])

    class _Q:
        def execute(self, *a, **k):
            r = next(logins)
            if isinstance(r, Exception):
                raise r
            return r

    q = _Q()
    monkeypatch.setattr(sp, "query_db", lambda: q)
    provisioned = []
    monkeypatch.setattr(sp, "provision", lambda: provisioned.append(1) or {"error": ""})
    assert sp.run_query_target("owner") == (q, True) and provisioned == [1]
    assert capsys.readouterr().out == ""  # nothing on stdout: that is the stdio MCP channel


def test_query_role_fallback_logs_to_stderr_not_stdout(monkeypatch, capsys):
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "true")
    monkeypatch.setattr(sp, "_query_role_ok", None)

    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("password authentication failed")

    monkeypatch.setattr(sp, "query_db", lambda: _Broken())
    monkeypatch.setattr(sp, "provision", lambda: {"error": "role: permission denied"})
    assert sp.run_query_target("owner") == ("owner", True)
    captured = capsys.readouterr()
    assert captured.out == "" and "query role unavailable" in captured.err


def test_provision_creates_the_helper_views():
    db = FakeDb({"pg_roles": [{"x": 1}]})
    db.conn_params = {"dbname": "citydb"}
    out = sp.provision(db)
    assert out["table"] and out["views"] and out["role"]
    created = " ".join(str(c[0]) for c in db.calls)
    assert "feature_geometry" in created and "feature_boundary" in created
    # For every feature class (roads, bridges, ...), not only buildings.
    views = " ".join(sp._HELPER_VIEWS_DDL)
    assert "'Building'" not in views and "GroundSurface" not in views


@pytest.mark.parametrize("question,first", [
    ("Create one group per energy class", "1"),
    ("Create a single 3D alpha-wrapping around the buildings", "2"),
    ("Create a 2D alpha-wrapping around all footprints", "3"),
    ("Add a 2m buffer (in 2D) around that alpha-wrapping.", "7"),
    ("Put a marker on top of the 5 tallest buildings", "5"),
    ("Create 3D lines from the markers to the closest buildings", "6"),
    ("Wie viele Stockwerke hat das Gebäude? Erstelle Ringe", "8"),
])
def test_the_save_reminder_repeats_the_matching_recipe(question, first):
    from webui import llm_utils

    recipes = llm_utils.scratch_recipes_for(question)
    assert recipes and recipes[0].startswith(first + " ")
    assert recipes[0] in llm_utils.scratch_save_reminder(question)
    if "2D" in question:
        assert not any(r.startswith("2 ") for r in recipes)
    if "3D alpha" in question:
        assert not any(r.startswith("3 ") for r in recipes)


def test_claims_unsaved_item_only_without_a_save():
    from webui import llm_utils

    q = "Create a marker on the tallest building"
    assert llm_utils.claims_unsaved_item(q, "The marker was created.", [])
    assert not llm_utils.claims_unsaved_item(q, "The marker was created.", ["marker_X"])
    assert not llm_utils.claims_unsaved_item("How tall is it?", "It was created in 1990.", [])
    assert not llm_utils.claims_unsaved_item(q, "Could not find the building.", [])
    assert llm_utils.claims_unsaved_item(q, "Die Eckpunkte wurden im Viewer als Punkt-Set angezeigt.", [])


def _cloud_run(monkeypatch, replies, question, run=None):
    from webui.backends import cloud

    sent = []
    monkeypatch.setattr(cloud, "_litellm_kwargs", lambda *a, **k: {"model": "m"})
    monkeypatch.setattr(cloud, "_log_context_usage", lambda *a, **k: (0, 1000, None))
    monkeypatch.setattr(cloud, "safe_completion", lambda kw, **k: sent.append(k) or replies.pop(0))
    extra = {"save_to_scratchpad": {"spec": {"type": "function", "function": {"name": "save_to_scratchpad"}},
                                    "run": run or (lambda a: json.dumps(
                                        {"scratchpad": {"ok": True, "name": "s", "kind": "featureset",
                                                        "count": 1}}))}}
    events = list(cloud.native_tool_stream("openai", "m", None, [{"role": "user", "content": question}],
                                           lambda sql: "{}", extra_tools=extra))
    return events, sent


def test_cloud_asks_once_when_the_answer_claims_an_unsaved_item(monkeypatch):
    from types import SimpleNamespace as NS

    call = NS(id="c1", function=NS(name="save_to_scratchpad",
                                   arguments=json.dumps({"name": "s", "sql": "SELECT 1 AS objectid"})))
    replies = [NS(choices=[NS(message=NS(content="The group s was created.", tool_calls=None))]),
               NS(choices=[NS(message=NS(content="", tool_calls=[call]))]),
               NS(choices=[NS(message=NS(content="Saved s (1 object).", tool_calls=None))])]
    events, sent = _cloud_run(monkeypatch, replies, "Create a group s of all buildings")
    assert ("final", "Saved s (1 object).") in events
    assert any("no save_to_scratchpad call succeeded" in str(m.get("content", ""))
               for m in sent[1]["messages"] if isinstance(m, dict))
    # The answer after a scratchpad result is anchored to the question's language.
    tool_msgs = [m for m in sent[2]["messages"] if isinstance(m, dict) and m.get("role") == "tool"]
    assert tool_msgs and "same language" in tool_msgs[-1]["content"]
    assert "Create a group s of all buildings" in tool_msgs[-1]["content"]


def test_cloud_claim_guard_stops_after_max_corrections(monkeypatch):
    from types import SimpleNamespace as NS
    from webui.backends import cloud

    monkeypatch.setattr(cloud, "MAX_CORRECTIONS", 3)
    claim = "The group s was created."
    replies = [NS(choices=[NS(message=NS(content=claim, tool_calls=None))]) for _ in range(4)]
    events, sent = _cloud_run(monkeypatch, replies, "Create a group s of all buildings")
    assert len(sent) == 4 and ("final", claim) in events  # 3 corrections, then the answer stands


def test_cloud_retries_an_empty_reply_up_to_max_corrections(monkeypatch):
    from types import SimpleNamespace as NS
    from webui.backends import cloud

    monkeypatch.setattr(cloud, "MAX_CORRECTIONS", 3)
    empty = NS(choices=[NS(message=NS(content="", tool_calls=None))])
    answer = NS(choices=[NS(message=NS(content="42", tool_calls=None))])
    events, sent = _cloud_run(monkeypatch, [empty, empty, answer], "How many buildings?")
    assert ("final", "42") in events and len(sent) == 3
    events, sent = _cloud_run(monkeypatch, [empty] * 4, "How many buildings?")
    finals = [d for e, d in events if e == "final"]
    assert len(sent) == 4 and finals and "without an answer" in finals[0]


def test_max_corrections_is_configurable(monkeypatch):
    import importlib

    from webui import llm_utils

    try:
        monkeypatch.setenv("AGENT_MAX_CORRECTIONS", "5")
        assert importlib.reload(llm_utils).MAX_CORRECTIONS == 5
        monkeypatch.setenv("AGENT_MAX_CORRECTIONS", "many")
        assert importlib.reload(llm_utils).MAX_CORRECTIONS == 3
    finally:
        monkeypatch.delenv("AGENT_MAX_CORRECTIONS")
        importlib.reload(llm_utils)


def test_local_parser_corrects_a_claimed_unsaved_item_up_to_max_corrections(monkeypatch):
    import queue

    from langchain_core.agents import AgentFinish
    from langchain_core.exceptions import OutputParserException
    from webui.backends import local

    monkeypatch.setattr(local, "MAX_CORRECTIONS", 3)
    cb = local._EventCallback(queue.Queue())
    cb._tools_called = 1
    parser = local._RobustReActParser(cb, user_question="Put a marker on the tallest building",
                                      tool_names={"run_query", "save_to_scratchpad"})
    claim = "Thought: done\nFinal Answer: The marker was created."
    for _ in range(3):
        with pytest.raises(OutputParserException, match="no save_to_scratchpad call succeeded"):
            parser.parse(claim)
    assert isinstance(parser.parse(claim), AgentFinish)  # after 3 corrections the answer stands
    assert cb._consecutive_parse_failures == 0  # corrections are no format errors
    cb2 = local._EventCallback(queue.Queue())
    cb2._tools_called = 1
    cb2.saved_names.append("marker_X")
    parser2 = local._RobustReActParser(cb2, user_question="Put a marker on the tallest building",
                                       tool_names={"run_query", "save_to_scratchpad"})
    assert isinstance(parser2.parse(claim), AgentFinish)


@pytest.mark.parametrize("question,answer,expected", [
    ("Create a 2D alpha-wrapping around the union of all buildings footprints.",
     "Ein 2D-Alpha-Wrapping um die Gebäudefußabdrücke wurde erstellt und ist im Viewer als gelb dargestellt.",
     "English"),
    ("Erstelle eine 2D-Alpha-Umhüllung um die Vereinigung aller Grundrisse der Gebäude.",
     "The 2D alpha wrapping was created and is shown in yellow with 30 % transparency.", "German"),
    ("Create one group per class.", "The groups energy_A to energy_H were created.", None),
    ("Put a marker on the buildings in Röblingweg.",
     "The markers on Röblingweg were created for the buildings of the street.", None),
    ("How many?", "42", None),
])
def test_wrong_language_nudge(question, answer, expected):
    from webui import llm_utils

    nudge = llm_utils.wrong_language_nudge(question, answer)
    assert (expected in nudge) if expected else nudge == ""


def test_cloud_asks_for_the_language_of_the_question(monkeypatch):
    from types import SimpleNamespace as NS
    from webui.backends import cloud

    monkeypatch.setattr(cloud, "MAX_CORRECTIONS", 2)
    german = "Die Anzahl der Gebäude ist 331 und das ist die Antwort."
    replies = [NS(choices=[NS(message=NS(content=german, tool_calls=None))]),
               NS(choices=[NS(message=NS(content="There are 331 buildings.", tool_calls=None))])]
    events, sent = _cloud_run(monkeypatch, replies, "How many buildings are there in the dataset?")
    assert ("final", "There are 331 buildings.") in events and len(sent) == 2
    assert "Rewrite the same answer in English" in sent[1]["messages"][-1]["content"]
    replies = [NS(choices=[NS(message=NS(content=german, tool_calls=None))]) for _ in range(3)]
    events, sent = _cloud_run(monkeypatch, replies, "How many buildings are there in the dataset?")
    assert len(sent) == 3 and ("final", german) in events  # 2 corrections, then the answer stands


def test_local_parser_asks_for_the_language_of_the_question(monkeypatch):
    import queue

    from langchain_core.agents import AgentFinish
    from langchain_core.exceptions import OutputParserException
    from webui.backends import local

    monkeypatch.setattr(local, "MAX_CORRECTIONS", 2)
    cb = local._EventCallback(queue.Queue())
    cb._tools_called = 1
    parser = local._RobustReActParser(cb, user_question="How many buildings are there in the dataset?",
                                      tool_names={"run_query"})
    german = "Thought: fertig\nFinal Answer: Die Anzahl der Gebäude ist 331 und das ist die Antwort."
    with pytest.raises(OutputParserException) as exc:
        parser.parse(german)
    assert exc.value.send_to_llm and "in English" in exc.value.observation
    with pytest.raises(OutputParserException):
        parser.parse(german)
    assert isinstance(parser.parse(german), AgentFinish)


# ── six languages: English, German, French, Dutch, Japanese, Finnish ─────────

@pytest.mark.parametrize("question", [
    "Crée un groupe par classe énergétique.",
    "Place un marqueur sur les 5 bâtiments les plus hauts.",
    "Ajoute une zone tampon de 2 m autour de l'enveloppe.",
    "Maak een groep per energieklasse.",
    "Teken lijnen van de markers naar de dichtstbijzijnde gebouwen.",
    "Voeg een buffer van 2 m toe rond de omhulling.",
    "最も高い5つの建物の上にマーカーを置いてください。",
    "フットプリントの周りに2Dのアルファシェイプを作成して。",
    "Luo ryhmä jokaiselle energialuokalle.",
    "Piirrä viivat merkeistä lähimpiin rakennuksiin.",
    "Lisää 2 metrin puskuri kuoren ympärille.",
])
def test_scratchpad_intent_in_six_languages(question):
    from webui import llm_utils

    assert llm_utils.wants_scratchpad(question)


@pytest.mark.parametrize("question", [
    "Combien de bâtiments y a-t-il ?",
    "Hoeveel gebouwen zijn er?",
    "建物はいくつありますか？",
    "Kuinka monta rakennusta on?",
])
def test_plain_questions_in_other_languages_are_no_scratchpad_intent(question):
    from webui import llm_utils

    assert not llm_utils.wants_scratchpad(question)


@pytest.mark.parametrize("answer", [
    "Le groupe a été créé dans le visualiseur.",
    "De markers zijn aangemaakt.",
    "マーカーを作成しました。",
    "Merkit on luotu.",
])
def test_save_claims_in_other_languages(answer):
    from webui import llm_utils

    assert llm_utils.claims_unsaved_item("Create a marker on the tallest building", answer, [])


@pytest.mark.parametrize("text,lang", [
    ("Combien de bâtiments sont dans la base de données ?", "fr"),
    ("Il y a 331 bâtiments dans cette ville et ils ont tous une hauteur.", "fr"),
    ("Hoeveel gebouwen zijn er in de database?", "nl"),
    ("Er zijn 331 gebouwen in de stad en het hoogste is 28 m.", "nl"),
    ("データベースに建物はいくつありますか？", "ja"),
    ("建物は331棟あり、最も高いものは28 mです。", "ja"),
    ("Kuinka monta rakennusta tietokannassa on ja mikä on korkein?", "fi"),
    ("How many buildings are there in the dataset?", "en"),
    ("Wie viele Gebäude gibt es in der Datenbank?", "de"),
    ("42", None),
])
def test_language_of_six_languages(text, lang):
    from webui import llm_utils

    assert llm_utils._language_of(text) == lang


def test_language_ignores_sql_in_the_answer():
    from webui import llm_utils

    answer = ("Il y a 331 bâtiments dans la ville et ils sont tous en 3D.\n"
              "```sql\nSELECT count(*) FROM feature f JOIN objectclass o ON o.id = f.objectclass_id "
              "WHERE o.classname = 'Building' AND f.id IS NOT NULL\n```")
    assert llm_utils._language_of(answer) == "fr"
    assert llm_utils.wrong_language_nudge("Combien de bâtiments y a-t-il dans la ville ?", answer) == ""
    nudge = llm_utils.wrong_language_nudge("建物はいくつありますか？", "There are 331 buildings in the city.")
    assert "in Japanese" in nudge


@pytest.mark.parametrize("question,first", [
    ("Place un marqueur sur les 5 bâtiments les plus hauts.", "5"),
    ("Teken lijnen van de markers naar de dichtstbijzijnde gebouwen.", "6"),
    ("フットプリントの周りに2Dのアルファシェイプを作成して。", "3"),
    ("3Dの凸包を作成して。", "2"),
    ("Lisää 2 metrin puskuri.", "7"),
])
def test_recipes_are_routed_in_other_languages(question, first):
    from webui import llm_utils

    recipes = llm_utils.scratch_recipes_for(question)
    assert recipes and recipes[0].startswith(first + " ")


def test_cloud_runs_a_tool_call_written_as_text(monkeypatch):
    """gpt-oss sometimes writes "Action: save_to_scratchpad / Action Input: {...}"
    as text instead of a native call (evaluation 2026-11-16, Q1 and Q8)."""
    from types import SimpleNamespace as NS

    text = ('Action: save_to_scratchpad\nAction Input: {\n  "name": "rings",\n'
            '  "sql": "SELECT ST_Collect(g) AS geom FROM t"\n}')
    ran = []
    replies = [NS(choices=[NS(message=NS(content=text, tool_calls=None))]),
               NS(choices=[NS(message=NS(content="Saved rings.", tool_calls=None))])]
    events, sent = _cloud_run(monkeypatch, replies, "Create closed rings per storey",
                              run=lambda a: ran.append(a) or json.dumps(
                                  {"scratchpad": {"ok": True, "name": "rings", "kind": "geometry"}}))
    assert ran == [{"name": "rings", "sql": "SELECT ST_Collect(g) AS geom FROM t"}]
    assert ("final", "Saved rings.") in events
    history = [m for m in sent[1]["messages"] if isinstance(m, dict)]
    call = next(m for m in history if m.get("role") == "assistant" and m.get("tool_calls"))
    assert call["tool_calls"][0]["function"]["name"] == "save_to_scratchpad"
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == call["tool_calls"][0]["id"]
               for m in history)


@pytest.mark.parametrize("content", [
    "Action: drop_everything\nAction Input: {\"x\": 1}",  # unknown tool
    "Action: save_to_scratchpad\nAction Input: not json",
    "Action: save_to_scratchpad\nAction Input: [1, 2]",
    "The marker is at the top of the building.",
])
def test_text_tool_call_needs_a_known_tool_and_a_json_object(content):
    from webui.backends import cloud

    assert cloud._text_tool_call(content, {"save_to_scratchpad": {}}) is None


def test_text_tool_call_accepts_the_gpt_oss_prefix():
    from webui.backends import cloud

    out = cloud._text_tool_call('Action: functions.save_to_scratchpad\nAction Input: ```json\n{"name": "a"}\n```',
                                {"save_to_scratchpad": {}})
    assert out == ("save_to_scratchpad", '{"name": "a"}')


@pytest.mark.parametrize("question", [
    "Add a 2m buffer (in 2D) around that alpha-wrapping.",
    "Put a marker on top of the 5 tallest buildings with the height as labels.",
])
def test_short_english_questions_are_recognised(question):
    from webui import llm_utils

    assert llm_utils._language_of(question) == "en"


def test_short_answer_in_the_wrong_language_is_caught():
    """Evaluation 2026-11-16, English Q3: a one-line German answer."""
    from webui import llm_utils

    q = "Extract all points from the lod2Solid of building DEBY_LOD2_107800980 and show them as cornerpoints in green."
    a = "Die Punkte wurden als **“cornerpoints”** in grün gespeichert – 1 Punktensatz (Multipoit Z)."
    assert "in English" in llm_utils.wrong_language_nudge(q, a)


def test_no_word_counts_for_two_languages():
    from collections import Counter

    from webui import llm_utils

    seen = Counter(w for words in llm_utils._FUNCTION_WORDS.values() for w in set(words.split()))
    assert [w for w, n in seen.items() if n > 1] == []


def test_null_tool_arguments_are_dropped():
    from webui import llm_utils

    assert llm_utils.tool_args_without_nulls(
        {"name": "l", "sql": "SELECT 1", "base_z": None, "height_m": None, "label": "", "alpha": 0}) == {
        "name": "l", "sql": "SELECT 1", "label": "", "alpha": 0}


def test_cloud_drops_null_arguments_before_the_tool_runs(monkeypatch):
    from types import SimpleNamespace as NS

    call = NS(id="c1", function=NS(name="save_to_scratchpad", arguments=json.dumps(
        {"name": "s", "sql": "SELECT 1 AS objectid", "base_z": None, "height_m": None})))
    ran = []
    replies = [NS(choices=[NS(message=NS(content="", tool_calls=[call]))]),
               NS(choices=[NS(message=NS(content="Saved s.", tool_calls=None))])]
    _cloud_run(monkeypatch, replies, "Create a group s of all buildings",
               run=lambda a: ran.append(a) or json.dumps({"scratchpad": {"ok": True, "name": "s"}}))
    assert ran == [{"name": "s", "sql": "SELECT 1 AS objectid"}]
