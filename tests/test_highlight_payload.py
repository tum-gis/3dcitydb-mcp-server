"""Tests for the chatbot-side highlight payload extraction (webui.llm_utils).

The database resolver is stubbed, so no PostgreSQL connection is needed.

Usage:
    pytest tests/test_highlight_payload.py -v
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

from webui import llm_utils  # noqa: E402


def _fake_resolver(monkeypatch, known: dict[str, list[str]]):
    """Stub resolve_highlight_targets: `known` maps objectid -> tile_ids."""
    import citydb_mcp.tools.highlight as highlight

    calls = []

    def fake(db, ids):
        calls.append(list(ids))
        resolved = [{"objectid": i, "classname": "X", "tile_ids": known[i],
                     "implicit_only": False} for i in ids if i in known]
        return {
            "resolved": resolved,
            "missing": [i for i in ids if i not in known],
            "not_tileable": [r["objectid"] for r in resolved if not r["tile_ids"]],
            "tile_ids": [t for r in resolved for t in r["tile_ids"]],
            "centroid": {"lat": 48.1, "long": 11.5, "radius_m": 5.0, "height_m": 520.0},
            "truncated": False,
        }

    monkeypatch.setattr(highlight, "resolve_highlight_targets", fake)
    monkeypatch.setattr(llm_utils, "_get_resolver_db", lambda: object())
    return calls


def _result(rows):
    return [("tool_result", {"error": None, "all_rows": rows})]


def test_payload_uses_resolved_tile_ids(monkeypatch):
    _fake_resolver(monkeypatch, {"room-1": ["wall-a", "floor-b"]})
    payload = llm_utils.extract_highlight_payload(_result([{"objectid": "room-1"}]))
    assert payload["buildings"] == [{"gmlid": "room-1", "tile_ids": ["wall-a", "floor-b"]}]
    assert payload["centroid"]["radius_m"] == 5.0
    assert payload["missing"] == [] and payload["not_tileable"] == []


def test_plain_objectid_column_is_preferred_over_other_id_columns(monkeypatch):
    calls = _fake_resolver(monkeypatch, {"b": ["b"], "r": ["r"]})
    llm_utils.extract_highlight_payload(_result([{"room_objectid": "r", "objectid": "b"}]))
    assert calls == [["b"]]


def test_id_like_column_names_are_recognised(monkeypatch):
    calls = _fake_resolver(monkeypatch, {"r": ["r"]})
    llm_utils.extract_highlight_payload(_result([{"name": "Küche", "room_objectid": "r"}]))
    assert calls == [["r"]]


def test_no_id_column_gives_empty_payload(monkeypatch):
    calls = _fake_resolver(monkeypatch, {})
    payload = llm_utils.extract_highlight_payload(_result([{"name": "x", "area": 3}]))
    assert payload["buildings"] == [] and payload["centroid"] is None
    assert calls == []


def test_errored_or_empty_results_give_empty_payload(monkeypatch):
    _fake_resolver(monkeypatch, {})
    assert llm_utils.extract_highlight_payload([]) ["buildings"] == []
    assert llm_utils.extract_highlight_payload(
        [("tool_result", {"error": "boom", "all_rows": [{"objectid": "a"}]})]
    )["buildings"] == []


def test_ids_are_deduplicated_and_capped(monkeypatch):
    calls = _fake_resolver(monkeypatch, {})
    rows = [{"objectid": f"id{i % 300}"} for i in range(600)]
    llm_utils.extract_highlight_payload(_result(rows))
    assert len(calls[0]) == llm_utils._MAX_HIGHLIGHT_IDS
    assert len(set(calls[0])) == len(calls[0])


def test_unknown_ids_are_reported_missing(monkeypatch):
    _fake_resolver(monkeypatch, {"a": ["a"]})
    payload = llm_utils.build_highlight_payload(["a", "ghost"])
    assert [b["gmlid"] for b in payload["buildings"]] == ["a"]
    assert payload["missing"] == ["ghost"]


def test_resolver_failure_degrades_to_the_ids_as_given(monkeypatch):
    import citydb_mcp.tools.highlight as highlight

    def boom(db, ids):
        raise RuntimeError("db down")

    monkeypatch.setattr(highlight, "resolve_highlight_targets", boom)
    monkeypatch.setattr(llm_utils, "_get_resolver_db", lambda: object())
    payload = llm_utils.build_highlight_payload(["a", "b"])
    assert payload["buildings"] == [
        {"gmlid": "a", "tile_ids": ["a"]}, {"gmlid": "b", "tile_ids": ["b"]}
    ]
    assert payload["centroid"] is None


def test_fallback_regex_finds_gml_ids_of_any_shape():
    text = (
        "| UUID_eedde887-eede-4c85-b50c-c3723402a8af | DEBY_LOD2_4967586 | "
        "`2O2Fr$t4X7Zf8NOew3FLOH` | buildings | Röblingweg |"
    )
    assert llm_utils.fallback_regex_extract(text) == [
        "UUID_eedde887-eede-4c85-b50c-c3723402a8af",
        "DEBY_LOD2_4967586",
        "2O2Fr$t4X7Zf8NOew3FLOH",
    ]


def test_fallback_regex_ignores_ordinary_words():
    assert llm_utils.fallback_regex_extract("These buildings have residential usage.") == []
    assert llm_utils.fallback_regex_extract("") == []
