"""Tests for the address part of the assembled prompt (house numbers inside `street`).

The database is faked, so no PostgreSQL connection is needed.

Usage:
    pytest tests/test_address_vocabulary.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.models import VocabularyData  # noqa: E402
from citydb_mcp.tools import dynamic_tools  # noqa: E402
from citydb_mcp.tools.assembly import _render_address_vocabulary  # noqa: E402


class FakeDb:
    def __init__(self, house_number_row):
        self.house_number_row = house_number_row

    def execute_single(self, query, params=None):
        if "house_number" in query:
            return self.house_number_row
        return {"n": 2119 if "street" in query else 1}

    def execute(self, query, params=None):
        return []


def _vocab(db, monkeypatch):
    monkeypatch.setattr(dynamic_tools, "_vocab_cache", None)
    return dynamic_tools.get_vocabulary(db)


def test_detects_house_numbers_inside_street(monkeypatch):
    v = _vocab(FakeDb({"n": 2119, "in_street": 2119, "example": "Bräugasse 20"}), monkeypatch)
    assert v.numbers_in_street is True and v.street_example == "Bräugasse 20"
    text = _render_address_vocabulary(v)
    assert "House numbers are part of `street`" in text
    assert "a.street = 'Bräugasse 20'" in text


def test_separate_house_numbers_get_only_the_fallback(monkeypatch):
    v = _vocab(FakeDb({"n": 500, "in_street": 10, "example": None}), monkeypatch)
    assert v.numbers_in_street is False and v.street_example == ""
    text = _render_address_vocabulary(v)
    assert "House numbers are part of `street`" not in text
    assert "If `house_number` finds nothing" in text


def test_compact_mode_carries_the_note_too():
    v = VocabularyData(street_count=3, numbers_in_street=True, street_example="Ort 13")
    assert "a.street = 'Ort 13'" in _render_address_vocabulary(v, compact=True)


def test_detection_failure_falls_back(monkeypatch):
    class Broken(FakeDb):
        def execute_single(self, query, params=None):
            if "house_number" in query:
                raise RuntimeError("no address table")
            return super().execute_single(query, params)

    v = _vocab(Broken(None), monkeypatch)
    assert v.numbers_in_street is False
