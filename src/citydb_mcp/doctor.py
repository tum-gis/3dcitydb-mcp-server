"""Diagnostic command — verifies installation, configuration, and database state.

Run via: 3dcitydb-doctor

Exits with status 0 if all critical checks pass; 1 otherwise.
Warnings (e.g., missing optional providers) do not cause non-zero exit.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import sys

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ANSI color codes; degrade gracefully on terminals that don't support them
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# Unicode box-drawing and symbols; fall back to ASCII on narrow encodings (e.g. cp1252)
_enc = getattr(sys.stdout, "encoding", "ascii") or "ascii"
_USE_UNICODE = _enc.lower().replace("-", "") in ("utf8", "utf16", "utf32")

GREEN = "\033[32m" if _USE_COLOR else ""
YELLOW = "\033[33m" if _USE_COLOR else ""
RED = "\033[31m" if _USE_COLOR else ""
DIM = "\033[2m" if _USE_COLOR else ""
RESET = "\033[0m" if _USE_COLOR else ""

CHECK = f"{GREEN}{'✓' if _USE_UNICODE else 'OK'}{RESET}"
WARN = f"{YELLOW}{'⚠' if _USE_UNICODE else '!!'}{RESET}"
FAIL = f"{RED}{'✗' if _USE_UNICODE else 'XX'}{RESET}"
HLINE = "\u2500" * 60 if _USE_UNICODE else "-" * 60


class DoctorReport:
    """Accumulates check results so we can summarize at the end."""

    def __init__(self) -> None:
        self.critical_failures = 0
        self.warnings = 0

    def ok(self, label: str, detail: str = "") -> None:
        suffix = f" {DIM}({detail}){RESET}" if detail else ""
        print(f"  {CHECK} {label}{suffix}")

    def warn(self, label: str, detail: str = "") -> None:
        self.warnings += 1
        suffix = f" {DIM}({detail}){RESET}" if detail else ""
        print(f"  {WARN} {label}{suffix}")

    def fail(self, label: str, detail: str = "") -> None:
        self.critical_failures += 1
        suffix = f" {DIM}({detail}){RESET}" if detail else ""
        print(f"  {FAIL} {label}{suffix}")

    def section(self, title: str) -> None:
        print(f"\n{title}")


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

def check_python_version(report: DoctorReport) -> None:
    major, minor = sys.version_info[:2]
    version_str = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) >= (3, 10):
        report.ok("Python version", version_str)
    else:
        report.fail(
            "Python version",
            f"got {version_str}, requires >=3.10",
        )


def check_package_versions(report: DoctorReport) -> None:
    packages = [
        ("3dcitydb-mcp-server", True),
        ("mcp", True),
        ("psycopg2-binary", True),
        ("starlette", True),
        ("uvicorn", True),
        ("sqlglot", False),  # only required when SQL guard is in use
    ]
    for name, required in packages:
        try:
            version = importlib.metadata.version(name)
            report.ok(f"{name}", version)
        except importlib.metadata.PackageNotFoundError:
            if required:
                report.fail(f"{name}", "not installed")
            else:
                report.warn(f"{name}", "not installed (optional)")


def check_database_config(report: DoctorReport) -> dict | None:
    """Reads CITYDB_* env vars (same as db.py) and returns conn params, or None."""
    # Load .env — search from CWD upward, report what was found
    try:
        from dotenv import load_dotenv, find_dotenv
        dotenv_path = find_dotenv(usecwd=True)
        if dotenv_path:
            load_dotenv(dotenv_path, override=False)
            report.ok(".env file", dotenv_path)
        else:
            report.warn(".env file", "not found — relying on exported environment variables")
    except ImportError:
        pass  # python-dotenv not available; rely on already-exported vars

    host = os.environ.get("CITYDB_HOST")
    port_str = os.environ.get("CITYDB_PORT", "5432")
    dbname = os.environ.get("CITYDB_NAME")
    user = os.environ.get("CITYDB_USER")
    password = os.environ.get("CITYDB_PASSWORD")
    schema = os.environ.get("CITYDB_SCHEMA", "citydb")
    if not _SCHEMA_RE.match(schema):
        report.fail(
            "CITYDB_SCHEMA",
            f"invalid identifier {schema!r}: must match {_SCHEMA_RE.pattern}",
        )
        return None

    missing = [k for k, v in {
        "CITYDB_HOST": host, "CITYDB_NAME": dbname,
        "CITYDB_USER": user, "CITYDB_PASSWORD": password,
    }.items() if not v]

    if missing:
        report.warn(
            "Database config",
            f"{', '.join(missing)} not set — DB checks will be skipped",
        )
        return None

    try:
        port = int(port_str)
    except ValueError:
        report.fail("CITYDB_PORT", f"not a valid integer: {port_str!r}")
        return None

    report.ok(
        "Database config",
        f"{user}@{host}:{port}/{dbname} (schema: {schema})",
    )
    return {
        "host": host, "port": port, "dbname": dbname,
        "user": user, "password": password, "schema": schema,
    }


def check_postgres_connection(report: DoctorReport, params: dict) -> "psycopg2.extensions.connection | None":  # type: ignore[name-defined]
    try:
        import psycopg2  # type: ignore[import]
    except ImportError:
        report.fail("psycopg2 import", "package missing")
        return None

    conn_params = {k: v for k, v in params.items() if k != "schema"}
    try:
        conn = psycopg2.connect(**conn_params, connect_timeout=5)
    except Exception as e:
        report.fail("PostgreSQL connection", str(e).strip())
        return None

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]  # type: ignore[index]
            short_version = version.split(",")[0]
            report.ok("PostgreSQL connection", short_version)
    except Exception as e:
        report.fail("PostgreSQL version query", str(e).strip())
        conn.close()
        return None

    return conn


def check_postgis(report: DoctorReport, conn) -> None:  # type: ignore[no-untyped-def]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT PostGIS_Version()")
            version = cur.fetchone()[0]
            report.ok("PostGIS extension", version.strip())
    except Exception as e:
        report.fail("PostGIS extension", "not available — required for spatial queries")


def check_sfcgal(report: DoctorReport, conn) -> None:  # type: ignore[no-untyped-def]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT postgis_sfcgal_version()")
            version = cur.fetchone()[0]
            report.ok("SFCGAL extension", version.strip())

            # Verify the specific functions used by example queries
            cur.execute("""
                SELECT proname FROM pg_proc
                WHERE proname IN ('cg_volume', 'cg_3darea', 'cg_makesolid')
            """)
            funcs = {row[0] for row in cur.fetchall()}
            missing = {"cg_volume", "cg_3darea", "cg_makesolid"} - funcs
            if missing:
                report.warn(
                    "SFCGAL functions",
                    f"missing: {', '.join(sorted(missing))}",
                )
            else:
                report.ok("SFCGAL functions", "CG_Volume, CG_3DArea, CG_MakeSolid available")
    except Exception:
        report.fail(
            "SFCGAL extension",
            "not available — volume/3D-area queries will fail",
        )


def check_3dcitydb_schema(report: DoctorReport, conn, schema: str = "citydb") -> None:  # type: ignore[no-untyped-def]
    required_tables = [
        "objectclass",
        "feature",
        "property",
        "geometry_data",
        "datatype",
        "namespace",
        "database_srs",
    ]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = ANY(%s)
            """, (schema, required_tables))
            present = {row[0] for row in cur.fetchall()}
    except Exception as e:
        report.fail("3DCityDB schema", f"could not query schema: {e}")
        return

    missing = set(required_tables) - present
    if missing:
        report.fail(
            "3DCityDB v5 schema",
            f"missing tables in schema '{schema}': {', '.join(sorted(missing))}",
        )
        return

    report.ok("3DCityDB v5 schema", f"all {len(required_tables)} core tables present in '{schema}'")


def check_data_population(report: DoctorReport, conn, schema: str = "citydb") -> None:  # type: ignore[no-untyped-def]
    try:
        from psycopg2 import sql as _pg_sql
        with conn.cursor() as cur:
            cur.execute(
                _pg_sql.SQL("SET search_path TO {s}, public").format(
                    s=_pg_sql.Identifier(schema)
                )
            )
            cur.execute("SELECT COUNT(*) FROM feature")
            feature_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT objectclass_id) FROM feature")
            class_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM datatype")
            datatype_count = cur.fetchone()[0]
    except Exception as e:
        report.warn("Data population", f"could not count: {e}")
        return

    if feature_count == 0:
        report.warn(
            "feature table",
            "empty — agent will work but won't have data to query",
        )
    else:
        report.ok(
            "feature table",
            f"{feature_count:,} rows across {class_count} object classes",
        )

    if datatype_count == 0:
        report.fail("datatype table", "empty — required for property resolution")
    else:
        report.ok("datatype table", f"{datatype_count} rows")


def check_mixed_crs(report: DoctorReport, conn, schema: str = "citydb") -> None:  # type: ignore[no-untyped-def]
    try:
        from psycopg2 import sql as _pg_sql
        with conn.cursor() as cur:
            cur.execute(
                _pg_sql.SQL("SET search_path TO {s}, public").format(
                    s=_pg_sql.Identifier(schema)
                )
            )
            cur.execute("""
                SELECT DISTINCT ST_SRID(envelope) AS srid
                FROM feature
                WHERE envelope IS NOT NULL
            """)
            srids = sorted(row[0] for row in cur.fetchall() if row[0] is not None)
    except Exception:
        # Non-fatal; some installations may not have ST_SRID accessible
        return

    if not srids:
        return  # No envelopes; nothing to check
    if len(srids) == 1:
        report.ok("Coordinate reference system", f"single CRS, EPSG:{srids[0]}")
    else:
        report.warn(
            "Coordinate reference system",
            f"MIXED CRS detected: {srids} — spatial queries may produce wrong results",
        )




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"\n{DIM}3dcitydb-mcp-server diagnostic{RESET}")
    print(f"{DIM}{HLINE}{RESET}")

    report = DoctorReport()

    report.section("Installation")
    check_python_version(report)
    check_package_versions(report)

    report.section("Database")
    db_params = check_database_config(report)
    conn = None
    if db_params:
        schema = db_params["schema"]
        conn = check_postgres_connection(report, db_params)
        if conn is not None:
            check_postgis(report, conn)
            check_sfcgal(report, conn)
            check_3dcitydb_schema(report, conn, schema)
            check_data_population(report, conn, schema)
            check_mixed_crs(report, conn, schema)
            conn.close()

    # Summary
    print(f"\n{DIM}{HLINE}{RESET}")
    if report.critical_failures == 0:
        if report.warnings == 0:
            print(f"{GREEN}All checks passed.{RESET} Ready to run 3dcitydb-mcp.")
        else:
            print(
                f"{GREEN}Critical checks passed{RESET} "
                f"({report.warnings} warning{'s' if report.warnings != 1 else ''}). "
                f"Ready to run 3dcitydb-mcp."
            )
        return 0
    else:
        print(
            f"{RED}{report.critical_failures} critical failure"
            f"{'s' if report.critical_failures != 1 else ''}{RESET}, "
            f"{report.warnings} warning{'s' if report.warnings != 1 else ''}. "
            f"See details above."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())