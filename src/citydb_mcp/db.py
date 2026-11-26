import os
import re
import threading
import psycopg2
from psycopg2 import pool as _pg_pool
from psycopg2.extras import RealDictCursor
from psycopg2 import sql as _pg_sql
from dotenv import load_dotenv, find_dotenv

# Search upward from the current working directory so the server finds .env
# regardless of which directory it was launched from.
_dotenv_path = find_dotenv(usecwd=True)
if _dotenv_path:
    load_dotenv(_dotenv_path, override=False)


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DEFAULT_STATEMENT_TIMEOUT_MS = 600000  # 10 minutes; large datasets need long queries


def _extra_schemas() -> list[str]:
    """CITYDB_EXTRA_SCHEMAS (comma-separated) as a list of entries, each a
    schema (``poi``) or one table of it (``energy.heat_demand``), every
    part validated like CITYDB_SCHEMA. Empty when the variable is unset."""
    entries = [e.strip() for e in os.getenv("CITYDB_EXTRA_SCHEMAS", "").split(",") if e.strip()]
    for entry in entries:
        parts = entry.split(".")
        if len(parts) > 2 or not all(_SCHEMA_RE.match(part) for part in parts):
            raise ValueError(
                f"Invalid CITYDB_EXTRA_SCHEMAS entry {entry!r}: must be schema or "
                f"schema.table, each matching {_SCHEMA_RE.pattern}"
            )
    return entries


def _statement_timeout_ms() -> int:
    """CITYDB_STATEMENT_TIMEOUT_MS as a non-negative int (0 = no timeout)."""
    raw = os.getenv("CITYDB_STATEMENT_TIMEOUT_MS", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_STATEMENT_TIMEOUT_MS
    except ValueError:
        value = _DEFAULT_STATEMENT_TIMEOUT_MS
    return max(value, 0)


class DatabaseConnection:
    """Manages PostgreSQL connections to 3DCityDB v5.

    Uses a thread-safe connection pool so concurrent callers (SSE clients,
    parallel tool calls) never share a cursor on the same connection.
    """

    def __init__(self, user: str | None = None, password: str | None = None,
                 read_only: bool = True):
        """``user``/``password`` override the CITYDB_* login (e.g. the
        scratchpad's query role). ``read_only=False`` is only for the
        scratchpad writer, which touches nothing but the scratchpad table."""
        self.conn_params = {
            "host": os.getenv("CITYDB_HOST"),
            "port": int(os.getenv("CITYDB_PORT", "5432")),
            "dbname": os.getenv("CITYDB_NAME"),
            "user": user or os.getenv("CITYDB_USER"),
            "password": password if user else os.getenv("CITYDB_PASSWORD"),
        }
        self.read_only = read_only
        schema = os.getenv("CITYDB_SCHEMA", "citydb")
        if not _SCHEMA_RE.match(schema):
            raise ValueError(
                f"Invalid CITYDB_SCHEMA {schema!r}: must match {_SCHEMA_RE.pattern}"
            )
        self.schema = schema
        # Schemas and tables beside 3DCityDB that assemble_prompt describes.
        self.extra_schemas = _extra_schemas()
        self._pool: _pg_pool.ThreadedConnectionPool | None = None
        self._pool_lock = threading.Lock()

    def _get_pool(self) -> _pg_pool.ThreadedConnectionPool:
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = _pg_pool.ThreadedConnectionPool(
                        minconn=1,
                        maxconn=int(os.getenv("CITYDB_POOL_MAX", "8")),
                        **self.conn_params,
                    )
        return self._pool

    def _configure_connection(self, conn) -> None:
        """Configure a borrowed connection for read-only query work.

        - autocommit: each statement runs in its own implicit transaction.
        - default_transaction_read_only: hard, DB-level guarantee that no
          data-modifying statement (DML or DDL) can succeed on this session,
          regardless of what the application sends. This is the security
          boundary; application-level SQL string checks (e.g. in run_query)
          are only fast-fail conveniences on top of it.
        - statement_timeout: caps how long one statement may run
          (CITYDB_STATEMENT_TIMEOUT_MS, default 600000 = 10 min; 0 disables), so a
          runaway query cannot hold a pooled connection indefinitely.
        """
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = "
                        + ("on" if self.read_only else "off"))
            cur.execute("SET statement_timeout = %s", (_statement_timeout_ms(),))

    def connect(self):
        """Back-compat: return a borrowed connection.

        Caller MUST NOT hold this across requests — use execute()/execute_single()
        which manage the lifecycle correctly. Retained only for callers that
        need a raw connection for an introspection one-shot.
        """
        pool = self._get_pool()
        conn = pool.getconn()
        self._configure_connection(conn)
        return conn

    def _release(self, conn) -> None:
        if self._pool is not None and conn is not None:
            try:
                self._pool.putconn(conn)
            except Exception:
                # Pool may be closed during shutdown; drop the connection.
                try:
                    conn.close()
                except Exception:
                    pass

    def execute(self, query: str, params: tuple = None,
                settings: dict | None = None) -> list[dict]:
        """Run one statement. ``settings`` (e.g. ``{"citydb.user_id": uid}``)
        are set with ``set_config(..., is_local => true)`` in the same
        transaction, so they never leak to the next user of the pooled
        connection."""
        pool = self._get_pool()
        conn = pool.getconn()
        try:
            self._configure_connection(conn)
            if settings:
                conn.autocommit = False
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Identifier-quote the schema name; defence-in-depth on top of
                # the regex validation in __init__.
                cur.execute(
                    _pg_sql.SQL("SET search_path TO {schema}, public").format(
                        schema=_pg_sql.Identifier(self.schema)
                    )
                )
                for name, value in (settings or {}).items():
                    cur.execute("SELECT set_config(%s, %s, true)", (name, str(value)))
                cur.execute(query, params)
                rows = [dict(row) for row in cur.fetchall()] if cur.description else []
            if settings:
                conn.commit()
            return rows
        except Exception:
            if settings:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            self._release(conn)

    def execute_single(self, query: str, params: tuple = None) -> dict | None:
        results = self.execute(query, params)
        return results[0] if results else None

    def close(self):
        with self._pool_lock:
            if self._pool is not None:
                try:
                    self._pool.closeall()
                finally:
                    self._pool = None
