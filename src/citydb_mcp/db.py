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


class DatabaseConnection:
    """Manages PostgreSQL connections to 3DCityDB v5.

    Uses a thread-safe connection pool so concurrent callers (SSE clients,
    parallel tool calls) never share a cursor on the same connection.
    """

    def __init__(self):
        self.conn_params = {
            "host": os.getenv("CITYDB_HOST"),
            "port": int(os.getenv("CITYDB_PORT", "5432")),
            "dbname": os.getenv("CITYDB_NAME"),
            "user": os.getenv("CITYDB_USER"),
            "password": os.getenv("CITYDB_PASSWORD"),
        }
        schema = os.getenv("CITYDB_SCHEMA", "citydb")
        if not _SCHEMA_RE.match(schema):
            raise ValueError(
                f"Invalid CITYDB_SCHEMA {schema!r}: must match {_SCHEMA_RE.pattern}"
            )
        self.schema = schema
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

    def connect(self):
        """Back-compat: return a borrowed connection.

        Caller MUST NOT hold this across requests — use execute()/execute_single()
        which manage the lifecycle correctly. Retained only for callers that
        need a raw connection for an introspection one-shot.
        """
        pool = self._get_pool()
        conn = pool.getconn()
        conn.autocommit = True
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

    def execute(self, query: str, params: tuple = None) -> list[dict]:
        pool = self._get_pool()
        conn = pool.getconn()
        try:
            conn.autocommit = True
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Identifier-quote the schema name; defence-in-depth on top of
                # the regex validation in __init__.
                cur.execute(
                    _pg_sql.SQL("SET search_path TO {schema}, public").format(
                        schema=_pg_sql.Identifier(self.schema)
                    )
                )
                cur.execute(query, params)
                if cur.description:
                    return [dict(row) for row in cur.fetchall()]
                return []
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
