"""Tests for the flood scene: building query, payload extraction and prompt blocks.

The database is faked, so no PostgreSQL connection is needed.

Usage:
    pytest tests/test_flood_scene.py -v
"""

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

from citydb_mcp.tools import highlight  # noqa: E402
from webui import llm_utils  # noqa: E402


class FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return self.rows


def _result(rows):
    return ("tool_result", {"row_count": len(rows), "all_rows": rows, "preview_rows": rows[:5], "error": None})


@pytest.fixture(autouse=True)
def _flood_enabled(monkeypatch):
    monkeypatch.setattr(llm_utils, "FLOOD_SCENE_ENABLED", True)
    llm_utils._flood_cache.clear()
    for name in ("FLOOD_EXCLUDE_FUNCTIONS", "FLOOD_GAUGE_ZERO_M", "FLOOD_STAGES",
                 "FLOOD_REFERENCE_CM", "FLOOD_REFERENCE_LABEL", "FLOOD_GAUGE_NAME"):
        monkeypatch.delenv(name, raising=False)


# ── get_flood_buildings ───────────────────────────────────────────────────────

def test_flood_buildings_passes_exclusions_and_skips_null_bases():
    db = FakeDb([
        {"objectid": "B1", "base": 295.314, "extra_ids": []},
        {"objectid": "B2", "base": None, "extra_ids": []},
        {"objectid": "B3", "base": 301.0, "extra_ids": ["wall-x"]},
    ])
    out = highlight.get_flood_buildings(db, ("53001_1800",))
    assert out["buildings"] == [["B1", 295.314], ["B3", 301.0]]
    assert out["extra_ids"] == {"B3": ["wall-x"]}
    assert out["truncated"] is False
    query, params = db.calls[0]
    assert "ST_ZMin(f.envelope)" in query
    assert params == (["53001_1800"], highlight.MAX_FLOOD_BUILDINGS)


def test_flood_buildings_reports_truncation(monkeypatch):
    monkeypatch.setattr(highlight, "MAX_FLOOD_BUILDINGS", 2)
    db = FakeDb([{"objectid": f"B{i}", "base": 290.0 + i, "extra_ids": []} for i in range(2)])
    assert highlight.get_flood_buildings(db)["truncated"] is True


# ── extract_flood_scene ───────────────────────────────────────────────────────

def test_scene_from_count_row():
    events = [("tool_call", {"tool": "run_query", "args": {"sql": "SELECT ..."}}),
              _result([{"betroffen": 994, "flood_level_m": 299.33}])]
    assert llm_utils.extract_flood_scene(events) == {"level": 299.33, "protection": 0.0, "focus": []}


def test_scene_reads_protection_and_focus_ids():
    rows = [{"objectid": "B1", "flood_level_m": "299.33", "protection_m": 1.0},
            {"objectid": "B2", "flood_level_m": "299.33", "protection_m": 1.0}]
    assert llm_utils.extract_flood_scene([_result(rows)]) == {
        "level": 299.33, "protection": 1.0, "focus": ["B1", "B2"]}


def test_scene_accepts_close_variants_of_the_level_column():
    for col in ("flood_level_nhn", "water_level_nhn", "Wasserstand_m"):
        rows = [{"flooded_buildings": 1425, col: "299.33"}]
        assert llm_utils.extract_flood_scene([_result(rows)])["level"] == 299.33
    assert llm_utils.extract_flood_scene([_result([{"level": 299.33}])]) is None


def test_scene_level_from_the_flood_condition_when_the_column_is_missing():
    sql = "SELECT count(*) AS n FROM feature f WHERE f.objectclass_id = 901 AND ST_ZMin(f.envelope) < 299.33"
    events = [("tool_call", {"tool": "run_query", "args": {"sql": sql}}), _result([{"n": 1425}])]
    assert llm_utils.extract_flood_scene(events) == {"level": 299.33, "protection": 0.0, "focus": []}
    other = [("tool_call", {"tool": "run_query", "args": {"sql": "SELECT count(*) AS n FROM feature"}}),
             _result([{"n": 1}])]
    assert llm_utils.extract_flood_scene(other) is None


def test_no_scene_without_column_or_when_disabled(monkeypatch):
    assert llm_utils.extract_flood_scene([_result([{"betroffen": 3}])]) is None
    assert llm_utils.extract_flood_scene([_result([{"flood_level_m": None}])]) is None
    monkeypatch.setattr(llm_utils, "FLOOD_SCENE_ENABLED", False)
    assert llm_utils.extract_flood_scene([_result([{"flood_level_m": 299.0}])]) is None


def test_scene_uses_last_successful_result_only():
    events = [_result([{"flood_level_m": 299.33}]),
              ("tool_result", {"error": "boom", "all_rows": []}),
              _result([{"count": 5}])]
    assert llm_utils.extract_flood_scene(events) is None


# ── build_flood_payload ───────────────────────────────────────────────────────

def test_payload_uses_exclusions_from_env_and_caches(monkeypatch):
    calls = []

    def fake_buildings(db, exclude):
        calls.append(exclude)
        return {"buildings": [["B1", 295.3], ["B2", 300.1]], "extra_ids": {}, "truncated": False}

    monkeypatch.setattr(highlight, "get_flood_buildings", fake_buildings)
    monkeypatch.setattr(highlight, "get_dataset_extent", lambda db: {"west": 13.4, "east": 13.5,
                                                                    "south": 48.5, "north": 48.6})
    monkeypatch.setattr(llm_utils, "_get_resolver_db", lambda: object())
    monkeypatch.setenv("FLOOD_EXCLUDE_FUNCTIONS", "51009_1610, 53001_1800")
    monkeypatch.setenv("FLOOD_GAUGE_ZERO_M", "286.439")

    scene = {"level": 299.33, "protection": 1.0, "focus": ["B1"]}
    payload = llm_utils.build_flood_payload(scene)
    llm_utils.build_flood_payload(scene)

    assert calls == [("51009_1610", "53001_1800")]  # second call served from cache
    assert payload["level"] == 299.33 and payload["protection"] == 1.0
    assert payload["from_level"] == pytest.approx(294.3)
    assert payload["buildings"] == [["B1", 295.3], ["B2", 300.1]]
    assert payload["focus"] == ["B1"]
    assert payload["extent"]["west"] == 13.4
    assert payload["gauge"]["zero_m"] == 286.439


def test_payload_is_none_when_query_fails(monkeypatch):
    def boom(db, exclude):
        raise RuntimeError("db down")

    monkeypatch.setattr(highlight, "get_flood_buildings", boom)
    monkeypatch.setattr(llm_utils, "_get_resolver_db", lambda: object())
    assert llm_utils.build_flood_payload({"level": 1.0, "protection": 0.0, "focus": []}) is None


# ── gauge reference (viewer only) and prompt block ────────────────────────────

def test_gauge_reference_needs_gauge_zero():
    assert llm_utils.flood_gauge_info() is None


def test_gauge_reference_from_env(monkeypatch):
    monkeypatch.setenv("FLOOD_GAUGE_NAME", "Passau/Donau")
    monkeypatch.setenv("FLOOD_GAUGE_ZERO_M", "286.439")
    monkeypatch.setenv("FLOOD_STAGES", "700,740,x,850")
    monkeypatch.setenv("FLOOD_REFERENCE_CM", "1289")
    monkeypatch.setenv("FLOOD_REFERENCE_LABEL", "Juni 2013")
    assert llm_utils.flood_gauge_info() == {
        "name": "Passau/Donau", "zero_m": 286.439, "stages_cm": [700.0, 740.0, 850.0],
        "reference_cm": 1289.0, "reference_label": "Juni 2013",
    }


def test_prompt_gets_only_the_generic_flood_hint(monkeypatch):
    monkeypatch.setenv("FLOOD_GAUGE_ZERO_M", "286.439")
    monkeypatch.setenv("FLOOD_REFERENCE_CM", "1289")
    block = llm_utils.extra_prompt_blocks()
    assert block.startswith("[FLOOD SCENE]")
    assert "flood_level_m" in block and "ST_ZMin(feature.envelope)" in block
    assert "286.439" not in block and "1289" not in block  # gauge values stay in the viewer


def test_flood_hint_names_excluded_functions(monkeypatch):
    monkeypatch.setenv("FLOOD_EXCLUDE_FUNCTIONS", "51009_1610,53001_1800")
    assert "'51009_1610', '53001_1800'" in llm_utils.extra_prompt_blocks()
    monkeypatch.delenv("FLOOD_EXCLUDE_FUNCTIONS")
    assert "Not buildings" not in llm_utils.extra_prompt_blocks()


def test_no_extra_blocks_by_default(monkeypatch):
    monkeypatch.setattr(llm_utils, "FLOOD_SCENE_ENABLED", False)
    monkeypatch.setenv("CITYDB_SCRATCHPAD", "false")
    assert llm_utils.extra_prompt_blocks() == ""


def test_build_messages_appends_extra_blocks(monkeypatch):
    from webui import chat_runtime

    monkeypatch.setattr(chat_runtime, "extra_prompt_blocks", lambda: "[FLOOD SCENE] x")
    messages = chat_runtime.build_messages("hi", [], "SCHEMA")
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].endswith("SCHEMA\n\n[FLOOD SCENE] x")


def test_flood_hint_carries_sql_examples_with_exclusions(monkeypatch):
    block = llm_utils.extra_prompt_blocks()
    assert "Flood examples" in block and "305.0 AS flood_level_m" in block
    assert "NOT EXISTS" not in block  # no exclusion configured
    monkeypatch.setenv("FLOOD_EXCLUDE_FUNCTIONS", "53001_1800")
    block = llm_utils.extra_prompt_blocks()
    assert "x.feature_id = f.id AND x.name = 'function' AND x.val_string IN ('53001_1800')" in block
    assert "x.feature_id = d.id" in block  # nearest-dry example excludes too
    assert "{L}" not in block and "{X" not in block


# ── route view (an address and the nearest dry buildings) ─────────────────────

def test_focus_positions_parses_rows():
    db = FakeDb([
        {"objectid": "HOME", "lon": 13.47, "lat": 48.57, "zmin": 295.31, "zmax": 307.7, "distance_m": 0.0},
        {"objectid": "DRY", "lon": 13.46, "lat": 48.57, "zmin": 299.59, "zmax": 311.9, "distance_m": 135.573},
        {"objectid": "NOPOS", "lon": None, "lat": None, "zmin": None, "zmax": None, "distance_m": None},
    ])
    out = highlight.get_focus_positions(db, ["HOME", "DRY", "NOPOS"], "HOME")
    assert set(out) == {"HOME", "DRY"}
    assert out["DRY"]["distance_m"] == 135.6
    assert db.calls[0][1] == (["HOME", "DRY", "NOPOS"], "HOME")


def test_focus_positions_survive_query_errors():
    class Broken:
        def execute(self, query, params=None):
            raise RuntimeError("boom")

    assert highlight.get_focus_positions(Broken(), ["A"], "A") == {}


def _route_payload(monkeypatch, focus, level=299.33):
    seen = {}

    def fake_positions(db, ids, anchor):
        seen["ids"], seen["anchor"] = list(ids), anchor
        return {i: {"lon": 13.4, "lat": 48.5, "zmin": 295.0, "zmax": 300.0, "distance_m": 1.0} for i in ids}

    monkeypatch.setattr(highlight, "get_flood_buildings", lambda db, exclude: {
        "buildings": [["DRY", 300.1], ["HOME", 295.3], ["X", 296.0]], "extra_ids": {}, "truncated": False})
    monkeypatch.setattr(highlight, "get_dataset_extent", lambda db: None)
    monkeypatch.setattr(highlight, "get_focus_positions", fake_positions)
    monkeypatch.setattr(llm_utils, "_get_resolver_db", lambda: object())
    payload = llm_utils.build_flood_payload({"level": level, "protection": 0.0, "focus": focus})
    return payload, seen


def test_route_anchor_is_first_flooded_focus_building(monkeypatch):
    payload, seen = _route_payload(monkeypatch, ["DRY", "HOME"])
    assert payload["anchor"] == "HOME" and seen["anchor"] == "HOME"
    assert set(payload["focus_positions"]) == {"DRY", "HOME"}


def test_no_route_for_long_focus_lists(monkeypatch):
    payload, seen = _route_payload(monkeypatch, [f"B{i}" for i in range(llm_utils.FLOOD_ROUTE_MAX_FOCUS + 1)])
    assert payload["anchor"] is None and payload["focus_positions"] == {}
    assert seen == {}
