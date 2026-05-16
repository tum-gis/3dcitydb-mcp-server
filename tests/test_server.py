"""Quick smoke test -- runs key MCP tools directly against the database.

Usage (from mcp-server/ directory):
    python tests/test_server.py

Requires a populated .env file with CITYDB_* variables.
"""

import sys
import os
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.db import DatabaseConnection
from citydb_mcp.tools.dynamic_tools import scan_objectclasses, get_db_context_snapshot
from citydb_mcp.tools.runtime_tools import run_query

PASS = "\033[32m[ OK ]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"
SEP  = "-" * 50


def check(label: str, fn):
    try:
        result = fn()
        print(f"  {PASS} {label}")
        return result
    except Exception as e:
        print(f"  {FAIL} {label}")
        print(f"         {e}")
        return None


def main():
    print("\nMCP tool smoke test")
    print(SEP)

    db = DatabaseConnection()

    # 1. DB connection
    check("Database connects", lambda: db.connect())

    # 2. Scan object classes
    catalog = check("scan_objectclasses", lambda: scan_objectclasses(db))
    if catalog:
        classes = catalog.object_classes
        names = [c.classname for c in classes[:5]]
        extra = "..." if len(classes) > 5 else ""
        print(f"         {len(classes)} classes: {', '.join(names)}{extra}")

    # 3. DB context snapshot
    ctx = check("get_db_context_snapshot", lambda: get_db_context_snapshot(db))
    if ctx:
        print(f"         EPSG:{ctx.epsg_code}  SRS: {ctx.srs_name}")

    # 4. run_query -- basic SELECT
    result = check(
        "run_query (SELECT 5 features)",
        lambda: run_query(db, "SELECT id, objectclass_id FROM feature LIMIT 5"),
    )
    if result:
        rows = result.get("results", [])
        print(f"         {result['row_count']} row(s) in {result['execution_time_ms']}ms, first: {rows[0] if rows else 'none'}")

    # 5. run_query -- must block writes (returns success:False, does NOT raise)
    write_result = run_query(db, "INSERT INTO feature(id) VALUES (99999)")
    if not write_result.get("success") and "Only SELECT" in write_result.get("error", ""):
        print(f"  {PASS} run_query blocks INSERT")
        print(f"         error: {write_result['error']}")
    else:
        print(f"  {FAIL} run_query did not block INSERT -- check the guard in run_query()")

    db.close()
    print(SEP)
    print("Done.\n")


if __name__ == "__main__":
    main()
