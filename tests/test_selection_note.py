"""Unit tests for _format_selection_note and its injection into chat_stream.

Requires the webui package (production/webui) on sys.path, same as the other
webui-side tests in this directory.

Usage:
    pytest tests/test_selection_note.py -v
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

from webui.app import _format_selection_note, _on_selection_sink, _MAX_SELECTION_FEATURES  # noqa: E402


def test_note_lists_every_feature_and_the_in_clause():
    features = [
        {"objectid": "DEBY_LOD2_4965683", "classname": "Building"},
        {"objectid": "DEBY_LOD2_4965796", "classname": "Building"},
    ]
    note = _format_selection_note(features)
    assert "DEBY_LOD2_4965683" in note and "DEBY_LOD2_4965796" in note
    assert "Building" in note
    assert "WHERE f.objectid IN ('DEBY_LOD2_4965683', 'DEBY_LOD2_4965796')" in note
    assert "[VIEWER SELECTION — 2 feature(s)" in note


def test_note_handles_missing_classname():
    note = _format_selection_note([{"objectid": "abc", "classname": None}])
    assert "abc" in note
    assert "'abc'" in note


def test_note_truncates_and_says_so():
    features = [{"objectid": f"id{i}", "classname": "Building"} for i in range(_MAX_SELECTION_FEATURES + 10)]
    note = _format_selection_note(features)
    assert f"truncated to the first {_MAX_SELECTION_FEATURES} of {len(features)}" in note
    # The IN(...) clause itself must also respect the cap.
    assert note.count("'id") == _MAX_SELECTION_FEATURES


# ── _on_selection_sink: defensive parsing of untrusted browser input ────────

def test_on_selection_sink_parses_valid_payload():
    raw = '{"type":"selection","features":[{"objectid":"a","classname":"Building"}],"count":1}'
    state, badge = _on_selection_sink(raw)
    assert state == {"features": [{"objectid": "a", "classname": "Building"}], "count": 1}
    assert "1 feature" in badge and "selected" in badge


def test_on_selection_sink_empty_selection_clears_badge():
    state, badge = _on_selection_sink('{"type":"selection","features":[],"count":0}')
    assert state == {"features": [], "count": 0}
    assert badge == ""


def test_on_selection_sink_rejects_malformed_input():
    for raw in ["not json", "", "null", "[1,2,3]", '{"features": "not-a-list"}']:
        state, badge = _on_selection_sink(raw)
        assert state == {"features": [], "count": 0}
        assert badge == ""


def test_on_selection_sink_drops_features_without_an_objectid():
    raw = '{"features":[{"classname":"Building"},{"objectid":"ok"}]}'
    state, _ = _on_selection_sink(raw)
    assert state["features"] == [{"objectid": "ok", "classname": None}]


def test_on_selection_sink_caps_feature_count():
    many = [{"objectid": f"id{i}"} for i in range(_MAX_SELECTION_FEATURES + 20)]
    raw = __import__("json").dumps({"features": many})
    state, _ = _on_selection_sink(raw)
    assert state["count"] == _MAX_SELECTION_FEATURES
