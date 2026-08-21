"""Unit tests for the qualified codelist key mechanism (no DB required).

Run with:  python -m pytest tests/test_codelists.py -v
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.tools.dynamic_tools import (
    COUNTRY_CODELISTS,
    _find_static_codelist,
    _get_country_from_epsg,
    _qual_key,
    get_static_codelists,
)

QUALIFIED_KEY_RE = re.compile(r"^[a-z0-9]+:[A-Za-z0-9]+\.[A-Za-z0-9_]+$")


# ── _qual_key: normalization ──────────────────────────────────────────

class TestQualKey:
    def test_basic(self):
        assert _qual_key("bldg", "Building", "function") == "bldg:building.function"

    def test_case_insensitive(self):
        assert _qual_key("BLDG", "BUILDING", "FUNCTION") == "bldg:building.function"
        assert _qual_key("Bldg", "Building", "Function") == "bldg:building.function"

    def test_trims_whitespace(self):
        assert _qual_key("  bldg ", " Building ", " function ") == "bldg:building.function"

    def test_different_classes_differ(self):
        assert _qual_key("bldg", "Building", "function") != _qual_key("bldg", "BuildingPart", "function")

    def test_different_namespaces_differ(self):
        assert _qual_key("bldg", "Building", "function") != _qual_key("brid", "Bridge", "function")


# ── _find_static_codelist: lookup semantics ───────────────────────────

class TestFindStaticCodelist:
    def test_exact_hit_de_function(self):
        result = _find_static_codelist(get_static_codelists(25832), "bldg", "Building", "function")
        assert result is not None
        key, code_map = result
        assert key == "bldg:Building.function"
        assert "31001_1000" in code_map

    def test_hit_case_insensitive(self):
        result = _find_static_codelist(get_static_codelists(25832), "BLDG", "BUILDING", "FUNCTION")
        assert result is not None

    def test_no_bare_name_fallback(self):
        # 'function' exists as a key component but the class is Bridge → no match,
        # even though a DE function codelist exists.
        assert _find_static_codelist(get_static_codelists(25832), "brid", "Bridge", "function") is None

    def test_no_match_unknown_class(self):
        assert _find_static_codelist(get_static_codelists(25832), "bldg", "BuildingPart", "function") is None

    def test_default_block_only_rooftype(self):
        block = get_static_codelists(4326)
        assert _find_static_codelist(block, "bldg", "Building", "roofType") is not None
        assert _find_static_codelist(block, "bldg", "Building", "function") is None

    def test_jp_block(self):
        block = get_static_codelists(6668)
        assert _find_static_codelist(block, "bldg", "Building", "class") is not None
        assert _find_static_codelist(block, "bldg", "Building", "usage") is not None
        assert _find_static_codelist(block, "bldg", "Building", "roofType") is not None


# ── COUNTRY_CODELISTS: structural invariants ──────────────────────────

class TestCodelistStructure:
    def test_all_keys_qualified(self):
        unqualified = [
            key
            for country, block in COUNTRY_CODELISTS.items()
            for key in block
            if not QUALIFIED_KEY_RE.match(key)
        ]
        assert unqualified == [], f"Unqualified keys found: {unqualified}"

    def test_expected_keys(self):
        assert set(COUNTRY_CODELISTS["DE"]) == {
            "bldg:Building.function", "bldg:Building.usage", "bldg:Building.roofType",
        }
        assert set(COUNTRY_CODELISTS["JP"]) == {
            "bldg:Building.class", "bldg:Building.roofType", "bldg:Building.usage",
        }
        assert set(COUNTRY_CODELISTS["DEFAULT"]) == {"bldg:Building.roofType"}

    def test_no_buildingpart_keys(self):
        # BuildingPart codelists are intentionally not maintained (may be added later).
        for country, block in COUNTRY_CODELISTS.items():
            for key in block:
                assert "BuildingPart" not in key, f"Unexpected BuildingPart key in {country}: {key}"

    def test_all_entries_nonempty_strings(self):
        for country, block in COUNTRY_CODELISTS.items():
            for key, code_map in block.items():
                assert code_map, f"Empty codelist: {country}/{key}"
                for code, label in code_map.items():
                    assert isinstance(code, str) and code
                    assert isinstance(label, str) and label

    def test_de_function_and_usage_identical(self):
        # usage is a deliberate copy of function (ALKIS).
        assert COUNTRY_CODELISTS["DE"]["bldg:Building.function"] == COUNTRY_CODELISTS["DE"]["bldg:Building.usage"]


# ── EPSG → country → block selection ──────────────────────────────────

class TestCountrySelection:
    @pytest.mark.parametrize("epsg,expected", [
        (25831, "DE"), (25832, "DE"), (25833, "DE"), (5650, "DE"),
        (6668, "JP"), (2443, "JP"),
        (28992, "NL"),
        (3414, "SG"),
        (2056, "CH"),
        (4326, "UNKNOWN"),
        (0, "UNKNOWN"),
    ])
    def test_epsg_mapping(self, epsg, expected):
        assert _get_country_from_epsg(epsg) == expected

    def test_unknown_falls_back_to_default_block(self):
        assert get_static_codelists(4326) is COUNTRY_CODELISTS["DEFAULT"]
        assert get_static_codelists(0) is COUNTRY_CODELISTS["DEFAULT"]

    def test_de_returns_de_block(self):
        assert get_static_codelists(25832) is COUNTRY_CODELISTS["DE"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
