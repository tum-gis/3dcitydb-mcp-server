"""Unit tests for run_query's SQL comment handling.

The LLM sometimes prefixes its SQL with an explanatory `-- comment` line, or
leaves a trailing one after the last clause. Uses a fake database object, so
no PostgreSQL connection is needed.

Usage:
    pytest tests/test_run_query.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.tools.runtime_tools import run_query


class FakeDb:
    """Records every SQL string passed to execute() and returns canned rows."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        return self.rows


def test_leading_line_comment_no_longer_rejected():
    db = FakeDb([{"objectid": "a"}])
    out = run_query(db, "-- fetch buildings\nSELECT objectid FROM feature")
    assert out["success"] is True
    assert out["error"] == ""


def test_leading_block_comment_no_longer_rejected():
    db = FakeDb([{"objectid": "a"}])
    out = run_query(db, "/* fetch buildings */\nSELECT objectid FROM feature")
    assert out["success"] is True


def test_with_cte_preceded_by_comment_is_accepted():
    db = FakeDb([{"objectid": "a"}])
    out = run_query(db, "-- a CTE\nWITH t AS (SELECT 1) SELECT * FROM t")
    assert out["success"] is True


def test_non_select_is_still_rejected_after_stripping_comments():
    db = FakeDb()
    out = run_query(db, "-- oops\nDELETE FROM feature")
    assert out["success"] is False
    assert "Only SELECT and WITH" in out["error"]
    assert db.queries == []  # never reached execute()


def test_trailing_comment_does_not_swallow_the_appended_limit():
    db = FakeDb([{"objectid": "a"}])
    run_query(db, "SELECT objectid FROM feature -- all buildings", row_limit=500)
    (sent,) = db.queries
    # The LIMIT clause must be a real clause, not text inside the comment.
    assert "-- all buildings" not in sent
    assert "LIMIT 500" in sent


def test_comment_mentioning_limit_does_not_fool_the_has_limit_check():
    db = FakeDb([{"objectid": "a"}])
    run_query(db, "SELECT objectid FROM feature -- no limit needed here", row_limit=500)
    (sent,) = db.queries
    assert "LIMIT 500" in sent


def test_existing_limit_is_not_duplicated():
    db = FakeDb([{"objectid": "a"}])
    run_query(db, "SELECT objectid FROM feature LIMIT 10", row_limit=500)
    (sent,) = db.queries
    assert sent.count("LIMIT") == 1
    assert "LIMIT 10" in sent


def test_block_comment_inside_the_query_is_removed_before_execution():
    db = FakeDb([{"objectid": "a"}])
    run_query(db, "SELECT objectid /* the gml id */ FROM feature")
    (sent,) = db.queries
    assert "/*" not in sent and "*/" not in sent
