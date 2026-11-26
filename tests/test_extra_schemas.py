"""Unit tests for CITYDB_EXTRA_SCHEMAS: tables beside the 3DCityDB schema
described in the assembled prompt.

The database is never contacted: the variable is parsed by DatabaseConnection
without connecting, and the section is rendered from catalog rows.

Usage:
    pytest tests/test_extra_schemas.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.db import DatabaseConnection
from citydb_mcp.tools import assembly


def _row(table_ref, column_ref, col_type, table_comment=None, column_comment=None):
    return {
        "table_ref": table_ref, "column_ref": column_ref, "col_type": col_type,
        "table_comment": table_comment, "column_comment": column_comment,
    }


ROWS = [
    _row("energy.heat_demand", "building_objectid", "text",
         table_comment="Annual heat demand per building, in kWh.",
         column_comment="citydb.feature.objectid"),
    _row("energy.heat_demand", "geom", "geometry(MultiPolygon,25832)"),
    _row("energy.heat_demand", "tags", "text[]"),
    _row('poi."Schools View"', '"Name"', "text"),
]


def test_unset_names_no_schemas(monkeypatch):
    monkeypatch.delenv("CITYDB_EXTRA_SCHEMAS", raising=False)

    assert DatabaseConnection().extra_schemas == []


def test_schemas_and_single_tables_are_parsed(monkeypatch):
    monkeypatch.setenv("CITYDB_EXTRA_SCHEMAS", " energy.heat_demand, poi ,")

    assert DatabaseConnection().extra_schemas == ["energy.heat_demand", "poi"]


@pytest.mark.parametrize("value", ["poi;drop", "a.b.c", "poi.", "Schools View"])
def test_invalid_entry_is_refused(monkeypatch, value):
    monkeypatch.setenv("CITYDB_EXTRA_SCHEMAS", value)

    with pytest.raises(ValueError, match="CITYDB_EXTRA_SCHEMAS"):
        DatabaseConnection()


def test_section_lists_tables_columns_and_comments():
    section = assembly._render_extra_schemas(ROWS)

    assert section.startswith("## Other Schemas\n")
    assert "schema-qualified" in section
    assert (
        "- `energy.heat_demand`: Annual heat demand per building, in kWh.\n"
        "  `building_objectid` text (citydb.feature.objectid), "
        "`geom` geometry(MultiPolygon,25832), `tags` text[]"
    ) in section
    assert '- `poi."Schools View"`\n  `"Name"` text' in section


def test_progress_steps_count_the_extra_schemas_step():
    without = assembly._assembly_steps(True, True)
    with_extra = assembly._assembly_steps(True, True, extra_schemas=True)

    assert len(with_extra) == len(without) + 1
