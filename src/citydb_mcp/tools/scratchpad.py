"""Spatial scratchpad: per-user saved results that the 3D viewer can show.

The viewer's item list can export all items of a user as a JSON file and
import such a file (``export_items`` / ``import_items``).

An item is either a *featureset* (a list of objectids, coloured in the tiles)
or a *geometry* (pin, line, area, solid). Clients create and manage items with
the MCP tools in ``TOOL_SCHEMAS`` (see production/docs/scratchpad.md);
``save_item`` re-runs the given SELECT itself, so the geometry never passes
through the model.

Isolation: ``run_query`` connects as a read-only role (``citydb_query``) that
may read the city model and the view ``my_scratchpad`` but not the table. The
view only returns rows of ``current_setting('citydb.user_id')``, which the
server sets per call from the web UI's ``_ctx``. Writes go through a separate
owner connection that only ever touches the scratchpad table.
"""

import hashlib
import json
import os
import re
import sys
import threading

from psycopg2 import sql as _pg_sql

from ..db import DatabaseConnection

QUERY_ROLE = os.getenv("CITYDB_QUERY_USER", "citydb_query")
USER_SETTING = "citydb.user_id"

MAX_ITEMS = 100
MAX_VERTICES = 20_000
MAX_FEATURESET = 100_000
ACTIONS = ("show", "hide", "delete", "goto")

# The class vocabulary of the original design (idea-for-future-spatial-scratchpad.md)
# plus FeatureSet for saved sets of city-model features. It says how the
# viewer draws an item; save_item derives it from the geometry unless one is
# asked for explicitly.
GEOMETRY_CLASSES = (
    "2Dmarker", "3Dmarker",
    "Point2D", "LineString2D", "MultiLineString2D", "Polygon2D", "MultiPolygon2D",
    "Point3D", "LineString3D", "MultiLineString3D", "Polygon3D", "MultiPolygon3D",
    "Surface3D", "Solid3D", "MultiSolid3D",
)
CLASSES = GEOMETRY_CLASSES + ("FeatureSet",)
# Which geometry types each class accepts (PostGIS GeometryType names).
_CLASS_TYPES = {
    "2Dmarker": {"POINT", "MULTIPOINT"}, "3Dmarker": {"POINT", "MULTIPOINT"},
    "Point2D": {"POINT", "MULTIPOINT"}, "Point3D": {"POINT", "MULTIPOINT"},
    "LineString2D": {"LINESTRING"}, "LineString3D": {"LINESTRING"},
    "MultiLineString2D": {"MULTILINESTRING", "LINESTRING"},
    "MultiLineString3D": {"MULTILINESTRING", "LINESTRING"},
    "Polygon2D": {"POLYGON"}, "Polygon3D": {"POLYGON"},
    "MultiPolygon2D": {"MULTIPOLYGON", "POLYGON"}, "MultiPolygon3D": {"MULTIPOLYGON", "POLYGON"},
    "Surface3D": {"POLYHEDRALSURFACE", "TIN", "TRIANGLE", "POLYGON", "MULTIPOLYGON", "GEOMETRYCOLLECTION"},
    "Solid3D": {"POLYHEDRALSURFACE", "TIN"},
    "MultiSolid3D": {"GEOMETRYCOLLECTION", "POLYHEDRALSURFACE", "TIN"},
}
# Classes drawn flat (on the terrain): their geometry is stored without heights.
FLAT_CLASSES = ("2Dmarker", "Point2D", "LineString2D", "MultiLineString2D", "Polygon2D", "MultiPolygon2D")
_SOLID_CLASSES = ("Solid3D", "MultiSolid3D")
_EXTRUDABLE = ("POLYGON", "MULTIPOLYGON", "LINESTRING", "MULTILINESTRING")


_MULTI_OF = {"Polygon": "MultiPolygon", "LineString": "MultiLineString"}


def classify(gtype: str, ndims: int, member_types=(), all_closed=False, clamp=False,
             requested: str | None = None) -> str:
    """The class of a geometry item: the requested one if it fits the geometry,
    else derived (2D when it has no height or is draped)."""
    gtype = (gtype or "").upper()
    if requested:
        if requested not in GEOMETRY_CLASSES:
            raise ValueError(f"unknown class {requested!r} (allowed: {', '.join(GEOMETRY_CLASSES)})")
        # Polygon2D asked, MULTIPOLYGON returned (an alpha shape always is one):
        # same kind and dimension, so take the multi class instead of failing.
        multi = _MULTI_OF.get(requested[:-2])
        if multi and gtype == multi.upper():
            requested = multi + requested[-2:]
        if gtype not in _CLASS_TYPES[requested]:
            raise ValueError(f"class {requested} does not fit a {gtype} geometry")
        if (ndims or 2) < 3 and requested not in FLAT_CLASSES:
            raise ValueError(f"class {requested} needs a geometry with heights (give height_m)")
        return requested
    d = "2D" if clamp or (ndims or 2) < 3 else "3D"
    members = {str(m).upper() for m in member_types or ()}
    if gtype in ("POINT", "MULTIPOINT"):
        return d + "marker"
    if gtype == "LINESTRING":
        return "LineString" + d
    if gtype == "MULTILINESTRING":
        return "MultiLineString" + d
    if gtype == "POLYGON":
        return "Polygon" + d
    if gtype == "MULTIPOLYGON":
        return "MultiPolygon" + d
    if gtype in ("POLYHEDRALSURFACE", "TIN"):
        return "Solid3D" if all_closed else "Surface3D"
    if gtype == "GEOMETRYCOLLECTION" and members and members <= {"POLYHEDRALSURFACE", "TIN"}:
        return "MultiSolid3D" if all_closed else "Surface3D"
    if gtype == "GEOMETRYCOLLECTION" and members and members <= {"POLYGON", "MULTIPOLYGON"}:
        return "MultiPolygon" + d
    raise ValueError(f"cannot store a {gtype} with mixed parts ({', '.join(sorted(members))}); "
                     "save the parts as separate items")
DEFAULT_COLOR = "#ff5a00"
DEFAULT_ALPHA = 0.8  # opacity; transparency = 1 - alpha


def _check_alpha(value) -> float:
    """Opacity 0-1. Also accepts "0.7", "70%" or 70 (percent of opacity)."""
    if isinstance(value, bool):
        raise ValueError("alpha must be a number between 0 and 1 (opacity)")
    text = str(value).strip()
    percent = text.endswith("%")
    try:
        alpha = float(text.rstrip("%").strip().replace(",", "."))
    except ValueError:
        raise ValueError(f"alpha must be a number between 0 and 1 (opacity), not {value!r}") from None
    if percent or 1 < alpha <= 100:
        alpha /= 100
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be between 0 and 1 (opacity; transparency = 1 - alpha)")
    return alpha

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Blocks direct access to the table and to the setting that selects the user.
# Only needed when run_query cannot use the query role (BYOD without the
# rights to create it); with the role, the database itself refuses.
_GUARD_RE = re.compile(r"\bscratchpad\b|\bset_config\b|\bcurrent_setting\b|\bpg_settings\b",
                       re.IGNORECASE)


# ── Audit log ────────────────────────────────────────────────────────────────
# One JSON line per change (save, update, show/hide, delete, purge), so "the
# model changed/removed X" can be traced afterwards. Saves run in the MCP
# server and in the web UI; both append to the same file.

_audit_lock = threading.Lock()


def audit_path() -> str:
    explicit = os.getenv("CITYDB_MCP_AUDIT_LOG")
    if explicit is not None:
        return explicit  # "" switches it off
    return "/app/data/audit/scratchpad.jsonl" if os.path.isdir("/app/data") else ""


def _audit(op: str, user_id: str, **details) -> None:
    path = audit_path()
    if not path:
        return
    import datetime

    line = json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                       "op": op, "user_id": user_id, **details}, ensure_ascii=False, default=str)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _audit_lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"[scratchpad] audit log not written ({path}): {exc}", flush=True)


def enabled() -> bool:
    return os.getenv("CITYDB_SCRATCHPAD", "false").strip().lower() in ("1", "true", "yes", "on")


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def safe_name(name) -> str:
    """A valid item name made from a data value: umlauts transliterated
    ("Röblingweg" -> "Roeblingweg"), other characters -> "_"."""
    import unicodedata

    text = unicodedata.normalize("NFKD", str(name or "").translate(_TRANSLIT))
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text.encode("ascii", "ignore").decode()).strip("_")
    if text[:1].isdigit():
        text = "_" + text
    return text[:64]


def user_settings(user_id) -> dict:
    """The per-call setting the view filters on ({} without a valid user)."""
    if isinstance(user_id, str) and _UUID_RE.match(user_id):
        return {USER_SETTING: user_id}
    return {}


def guard_sql(sql: str) -> str | None:
    """Error message if the SQL touches the scratchpad table or the user setting."""
    if _GUARD_RE.search(sql or ""):
        return ("Use the view my_scratchpad for saved items; the scratchpad table and "
                "set_config/current_setting are not available in queries.")
    return None


# ── Connections ───────────────────────────────────────────────────────────────

def query_password() -> str:
    """CITYDB_QUERY_PASSWORD, or one derived from the owner login, so the web UI
    and every MCP subprocess agree without storing a secret."""
    explicit = os.getenv("CITYDB_QUERY_PASSWORD")
    if explicit:
        return explicit
    seed = f"citydb_query:{os.getenv('CITYDB_PASSWORD', '')}:{os.getenv('CITYDB_NAME', '')}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


_lock = threading.Lock()
_query_db: DatabaseConnection | None = None
_writer_db: DatabaseConnection | None = None
_owner_ro_db: DatabaseConnection | None = None
_query_role_ok: bool | None = None


def query_db() -> DatabaseConnection:
    global _query_db
    with _lock:
        if _query_db is None:
            _query_db = DatabaseConnection(user=QUERY_ROLE, password=query_password())
        return _query_db


def writer_db() -> DatabaseConnection:
    global _writer_db
    with _lock:
        if _writer_db is None:
            _writer_db = DatabaseConnection(read_only=False)
        return _writer_db


def _owner_reader() -> DatabaseConnection:
    global _owner_ro_db
    with _lock:
        if _owner_ro_db is None:
            _owner_ro_db = DatabaseConnection()
        return _owner_ro_db


def run_query_target(owner_db: DatabaseConnection) -> tuple[DatabaseConnection, bool]:
    """Connection for run_query and whether the SQL guard is needed.

    With the scratchpad on, run_query uses the query role; if that role cannot
    log in (not provisioned, BYOD), it falls back to the owner + the guard."""
    global _query_role_ok
    if not enabled():
        return owner_db, False
    if _query_role_ok is None:
        try:
            query_db().execute("SELECT 1")
            _query_role_ok = True
        except Exception as exc:
            print(f"[scratchpad] query role unavailable, using guard: {exc}", flush=True)
            _query_role_ok = False
    return (query_db(), True) if _query_role_ok else (owner_db, True)


# ── Provisioning (idempotent, owner connection) ───────────────────────────────

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.scratchpad (
    user_id    text NOT NULL,
    name       text NOT NULL CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]{{0,63}}$'),
    label      text,
    kind       text NOT NULL CHECK (kind IN ('featureset', 'geometry')),
    class      text,
    color      text NOT NULL DEFAULT '#ff5a00',
    alpha      double precision NOT NULL DEFAULT 0.8 CHECK (alpha BETWEEN 0 AND 1),
    clamp      boolean NOT NULL DEFAULT false,
    geom       geometry,
    objectids  text[],
    is_visible boolean NOT NULL DEFAULT true,
    data       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, name),
    CHECK ((kind = 'geometry') = (geom IS NOT NULL)),
    CHECK ((kind = 'featureset') = (objectids IS NOT NULL))
)"""

# Tables created before the class column existed get it here, filled in for
# their rows from the stored geometry.
_CLASS_MIGRATION = [
    "ALTER TABLE {schema}.scratchpad ADD COLUMN IF NOT EXISTS class text",
    """UPDATE {schema}.scratchpad SET class = CASE
         WHEN kind = 'featureset' THEN 'FeatureSet'
         WHEN GeometryType(geom) IN ('POLYHEDRALSURFACE', 'TIN') THEN
              CASE WHEN ST_IsClosed(geom) THEN 'Solid3D' ELSE 'Surface3D' END
         WHEN GeometryType(geom) = 'GEOMETRYCOLLECTION' THEN 'MultiSolid3D'
         ELSE (CASE GeometryType(geom)
                 WHEN 'POINT' THEN '' WHEN 'MULTIPOINT' THEN ''
                 WHEN 'LINESTRING' THEN 'LineString' WHEN 'MULTILINESTRING' THEN 'MultiLineString'
                 WHEN 'POLYGON' THEN 'Polygon' WHEN 'MULTIPOLYGON' THEN 'MultiPolygon' END)
              || (CASE WHEN ST_NDims(geom) > 2 AND NOT clamp THEN '3D' ELSE '2D' END)
              || (CASE WHEN GeometryType(geom) IN ('POINT', 'MULTIPOINT') THEN 'marker' ELSE '' END)
       END
       WHERE class IS NULL""",
]

_INDEX_DDL = ("CREATE INDEX IF NOT EXISTS scratchpad_geom_idx "
              "ON {schema}.scratchpad USING gist (geom gist_geometry_ops_nd)")

_VIEW_DDL = """
CREATE OR REPLACE VIEW {schema}.my_scratchpad WITH (security_barrier) AS
SELECT name, label, kind, color, alpha, clamp, geom, objectids, is_visible, data,
       created_at, updated_at, class
FROM {schema}.scratchpad
WHERE user_id = current_setting('citydb.user_id', true)"""


def _class_check_ddl() -> str:
    allowed = ", ".join("'" + c + "'" for c in CLASSES)  # inside $$ ... $$: no doubling
    return ("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'scratchpad_class_check') THEN "
            "ALTER TABLE {schema}.scratchpad ADD CONSTRAINT scratchpad_class_check "
            f"CHECK (class IN ({allowed})); END IF; END $$")


def provision(owner_db: DatabaseConnection | None = None) -> dict:
    """Create table, index, view and the read-only query role. Safe to repeat.
    The role part needs CREATEROLE (fullstack: the bootstrap superuser); if it
    fails, run_query falls back to the guard."""
    db = owner_db or writer_db()
    schema = _pg_sql.Identifier(db.schema)
    out = {"table": False, "role": False, "error": ""}
    try:
        for ddl in [_TABLE_DDL] + _CLASS_MIGRATION + [_INDEX_DDL, _VIEW_DDL, _class_check_ddl()]:
            db.execute(_pg_sql.SQL(ddl).format(schema=schema))
        out["table"] = True
    except Exception as exc:
        out["error"] = f"table: {exc}"
        return out
    role = _pg_sql.Identifier(QUERY_ROLE)
    try:
        exists = db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (QUERY_ROLE,))
        if not exists:
            db.execute(_pg_sql.SQL("CREATE ROLE {} LOGIN").format(role))
        statements = [
            _pg_sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                role, _pg_sql.Literal(query_password())),
            _pg_sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role),
            _pg_sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                _pg_sql.Identifier(db.conn_params["dbname"]), role),
            _pg_sql.SQL("GRANT USAGE ON SCHEMA {}, public TO {}").format(schema, role),
            _pg_sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, role),
            _pg_sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
                schema, role),
            # After the blanket grant: the table itself stays closed, only the view is open.
            _pg_sql.SQL("REVOKE ALL ON {}.scratchpad FROM {}, PUBLIC").format(schema, role),
            _pg_sql.SQL("GRANT SELECT ON {}.my_scratchpad TO {}").format(schema, role),
        ]
        for stmt in statements:
            db.execute(stmt)
        out["role"] = True
    except Exception as exc:
        out["error"] = f"role: {exc}"
    return out


# ── Save / actions (called by the web UI with the user's id) ──────────────────

def _check_user(user_id) -> str:
    if not user_settings(user_id):
        raise ValueError("no valid user id for this session")
    return user_id


def _inner_sql(sql: str) -> str:
    from .runtime_tools import _strip_sql_comments

    inner = _strip_sql_comments(sql or "").strip().rstrip(";").strip()
    upper = inner.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("only SELECT/WITH queries can be saved")
    if ";" in inner:
        raise ValueError("only a single statement can be saved")
    return inner


_GEOM_SQL = """
WITH q AS ({inner}),
c AS (SELECT count(*) AS n,
             CASE WHEN count(*) = 1 THEN (array_agg(q.geom::geometry))[1]
                  ELSE ST_Collect(q.geom::geometry) END AS g
      FROM q WHERE q.geom IS NOT NULL),
t AS (SELECT n, CASE WHEN ST_SRID(g) = 0 THEN ST_SetSRID(g, s.srid)
                     WHEN ST_SRID(g) <> s.srid THEN ST_Transform(g, s.srid)
                     ELSE g END AS g
      FROM c, (SELECT srid FROM database_srs LIMIT 1) s)
SELECT n, ST_NPoints(g) AS vertices,
       encode(ST_AsEWKB(CASE WHEN ST_HasArc(g) THEN ST_CurveToLine(g) ELSE g END), 'hex') AS ewkb
FROM t"""

# Describes a geometry (EWKB hex), optionally after the expression {expr} of g.
_SHAPE_SQL = """
WITH s AS (SELECT ST_GeomFromEWKB(decode(%(ewkb)s, 'hex')) AS g),
o AS (SELECT {expr} AS g FROM s)
SELECT ST_NPoints(g) AS vertices, GeometryType(g) AS gtype, ST_NDims(g) AS ndims,
       (SELECT array_agg(DISTINCT GeometryType(ST_GeometryN(g, i)))
        FROM generate_series(1, ST_NumGeometries(g)) i) AS members,
       (SELECT bool_and(GeometryType(ST_GeometryN(g, i)) IN ('POLYHEDRALSURFACE', 'TIN')
                        AND ST_IsClosed(ST_GeometryN(g, i)))
        FROM generate_series(1, ST_NumGeometries(g)) i) AS closed,
       encode(ST_AsEWKB(g), 'hex') AS shape_ewkb
FROM o"""

# The database has no terrain model; the ground under an item is taken as the
# lowest point of the nearest city objects (their envelopes carry heights).
GROUND_RADIUS_M = 500
_GROUND_SQL = """
WITH s AS (SELECT ST_GeomFromEWKB(decode(%(ewkb)s, 'hex')) AS g)
SELECT min(n.z) AS ground FROM (
    SELECT ST_ZMin(f.envelope) AS z FROM {schema}.feature f, s
    WHERE f.envelope IS NOT NULL AND ST_NDims(f.envelope) > 2
      AND ST_DWithin(f.envelope, s.g, %(radius)s)
    ORDER BY f.envelope <-> s.g LIMIT 3) n"""

# Raise g so that its lowest point is at lift_to (2D geometries get heights).
_LIFT = ("ST_Translate(ST_Force3DZ(g), 0, 0, %(lift_to)s - "
         "CASE WHEN ST_NDims(g) > 2 THEN ST_ZMin(g) ELSE 0 END)")


def _describe(db: DatabaseConnection, ewkb: str, expr: str = "g", params: dict | None = None) -> dict:
    rows = db.execute(_SHAPE_SQL.format(expr=expr), {"ewkb": ewkb, **(params or {})})
    if not rows or not rows[0].get("shape_ewkb"):
        raise ValueError("the geometry could not be read")
    return rows[0]


def _ground_z(db: DatabaseConnection, ewkb: str) -> float | None:
    rows = db.execute(_pg_sql.SQL(_GROUND_SQL).format(schema=_pg_sql.Identifier(db.schema)),
                      {"ewkb": ewkb, "radius": GROUND_RADIUS_M})
    z = rows[0].get("ground") if rows else None
    return float(z) if z is not None else None


def type_label(row: dict) -> str:
    """e.g. "POLYGON Z": GeometryType alone does not say whether there are heights."""
    return (row.get("gtype") or "") + (" Z" if (row.get("ndims") or 2) > 2 else "")


def _shape(db: DatabaseConnection, ewkb: str, *, clamp=False, item_class=None, height_m=None,
           extrude_m=None, base_z=None, sphere_r=None):
    """Bring a geometry in line with its class, on the server.

    A flat class (2Dmarker, ...2D) stores the geometry without heights, a 3D
    class with them: a 2D geometry asked to be 3D is lifted onto the ground.
    ``height_m`` raises the item's lowest point to that height above the
    ground (or above ``base_z``, an absolute height); ``extrude_m`` turns an
    area into a solid (a line into a wall) of that height. Returns
    (description, class, clamp, geometry changed)."""
    if item_class and item_class not in GEOMETRY_CLASSES:
        raise ValueError(f"unknown class {item_class!r} (allowed: {', '.join(GEOMETRY_CLASSES)})")
    flat = item_class in FLAT_CLASSES
    raising = height_m is not None or extrude_m is not None or base_z is not None
    if raising and flat:
        raise ValueError(f"class {item_class} is flat; height_m/extrude_m/base_z make a 3D item "
                         "(leave the class out or pick a 3D one)")
    if raising or (item_class and not flat):
        clamp = False
    meta = _describe(db, ewkb)
    gtype = meta["gtype"]
    if item_class in _SOLID_CLASSES and extrude_m is None and gtype in ("POLYGON", "MULTIPOLYGON"):
        raise ValueError(f"an area becomes a {item_class} with extrude_m (the solid's height in m)")
    expr, params = "g", {}
    if raising or (meta["ndims"] < 3 and item_class and not flat):
        ground = float(base_z) if base_z is not None else _ground_z(db, ewkb)
        if ground is None:
            raise ValueError(f"no city objects within {GROUND_RADIUS_M} m to take the ground height "
                             "from; give base_z (absolute height in m)")
        lift_to = ground + float(height_m or 0)
        if item_class == "Point3D" and height_m is None and meta["ndims"] < 3:
            lift_to += float(sphere_r or 1)  # a sphere put on the ground rests on it
        params["lift_to"] = lift_to
        expr = _LIFT
    if extrude_m is not None:
        if gtype not in _EXTRUDABLE:
            raise ValueError(f"only areas and lines can be extruded, not a {gtype}; raise a point "
                             "with height_m (3Dmarker) or draw it as a sphere (class Point3D, size_m)")
        if float(extrude_m) <= 0:
            raise ValueError("extrude_m must be positive")
        params["extrude"] = float(extrude_m)
        expr = f"CG_Extrude({expr}, 0, 0, %(extrude)s)"
        if item_class not in _SOLID_CLASSES + ("Surface3D",):
            item_class = None  # the extruded shape decides (Solid3D, MultiSolid3D, Surface3D)
    if expr == "g" and meta["ndims"] > 2:
        if classify(gtype, meta["ndims"], meta.get("members") or (), bool(meta.get("closed")),
                    clamp, item_class) in FLAT_CLASSES:
            expr = "ST_Force2D(g)"
    if expr != "g":
        meta = _describe(db, ewkb, expr, params)
    cls = classify(meta["gtype"], meta["ndims"], meta.get("members") or (), bool(meta.get("closed")),
                   clamp, item_class)
    return meta, cls, clamp, expr != "g"

_FEATURESET_SQL = """
WITH q AS ({inner})
SELECT count(DISTINCT q.objectid) AS n, array_agg(DISTINCT q.objectid::text) AS ids
FROM q WHERE q.objectid IS NOT NULL"""

_UPSERT_SQL = """
INSERT INTO {schema}.scratchpad
    (user_id, name, label, kind, class, color, alpha, clamp, geom, objectids, is_visible, data)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
        CASE WHEN %s::text IS NULL THEN NULL ELSE ST_GeomFromEWKB(decode(%s, 'hex')) END,
        %s, true, %s::jsonb)
ON CONFLICT (user_id, name) DO UPDATE SET
    label = EXCLUDED.label, kind = EXCLUDED.kind, class = EXCLUDED.class, color = EXCLUDED.color,
    alpha = EXCLUDED.alpha, clamp = EXCLUDED.clamp, geom = EXCLUDED.geom, objectids = EXCLUDED.objectids,
    is_visible = true, data = EXCLUDED.data, updated_at = now()"""


_SET_HAS_NO_GEOMETRY = ("a set of features has no geometry of its own to raise or extrude; save e.g. "
                        "its 3D box as a new item: SELECT ST_3DExtent(f.envelope) AS geom FROM feature f "
                        "JOIN my_scratchpad p ON f.objectid = ANY(p.objectids) WHERE p.name = '<set>'")


def _check_vertices(row: dict) -> None:
    if (row.get("vertices") or 0) > MAX_VERTICES:
        raise ValueError(f"geometry too large ({row['vertices']} vertices, max {MAX_VERTICES}); "
                         "simplify it (e.g. ST_Simplify, CG_3DConvexHull) first")


MAX_GROUPS = 50  # items one save call may create through an item_name column
SPLIT_COLUMN = "item_name"


def _group_item_name(prefix: str, value) -> str:
    """Item name for one item_name value: the value itself when it already
    starts with the prefix, else prefix_value ("energy" + "A" -> "energy_A")."""
    text = safe_name(value).lstrip("_")
    if not text:
        raise ValueError(f"item_name {value!r} has no letters or digits")
    if text.lower().startswith(prefix.lower()):
        return text[:64]
    return f"{prefix}_{text}"[:64]


def _save_groups(user_id: str, prefix: str, inner: str, columns: set, settings: dict,
                 qdb: DatabaseConnection, wdb: DatabaseConnection, color, **kwargs) -> dict:
    """One item per distinct item_name of the SELECT, each saved by save_item.
    A color column gives each item its own colour, a label column its own
    label; geom / objectid work per item as in a single save. The items are
    saved one after another (not all-or-nothing); the report lists each."""
    color_expr = "min(q.color::text)" if "color" in columns else "NULL"
    groups = qdb.execute(
        f"SELECT q.{SPLIT_COLUMN}::text AS v, {color_expr} AS c FROM ({inner}) q "
        f"WHERE q.{SPLIT_COLUMN} IS NOT NULL GROUP BY 1 ORDER BY 1 LIMIT {MAX_GROUPS + 1}",
        settings=settings)
    if not groups:
        raise ValueError(f"the {SPLIT_COLUMN} column is empty")
    if len(groups) > MAX_GROUPS:
        raise ValueError(f"more than {MAX_GROUPS} different {SPLIT_COLUMN} values; "
                         "group the rows further or save in several calls")
    names = [_group_item_name(prefix, g["v"]) for g in groups]
    if len(set(names)) < len(names):
        raise ValueError(f"different {SPLIT_COLUMN} values give the same item name; "
                         "use values that differ in letters or digits")
    items = []
    for i, (group, item) in enumerate(zip(groups, names), 1):
        # The i-th group by the same ordering as above; no data value goes into the SQL text.
        sub = (f"SELECT * FROM (SELECT q.*, dense_rank() OVER (ORDER BY q.{SPLIT_COLUMN}::text) "
               f"AS item_group FROM ({inner}) q WHERE q.{SPLIT_COLUMN} IS NOT NULL) z "
               f"WHERE z.item_group = {i}")
        own = group.get("c")
        items.append(save_item(user_id, item, sub, split=False, qdb=qdb, wdb=wdb,
                               color=own if isinstance(own, str) and _COLOR_RE.match(own) else color,
                               **kwargs))
    saved = [r["name"] for r in items if r.get("ok")]
    return {"ok": bool(saved), "name": prefix, "items": items, "saved": len(saved),
            "failed": len(items) - len(saved)}


def save_item(user_id: str, name: str, sql: str, *, label: str | None = None,
              color: str | None = None, clamp: bool = False, item_class: str | None = None,
              alpha=None, size_m: float | None = None, height_m: float | None = None,
              extrude_m: float | None = None, base_z: float | None = None,
              split: bool = True,
              qdb: DatabaseConnection | None = None,
              wdb: DatabaseConnection | None = None) -> dict:
    """Re-run ``sql`` read-only as the user and store its result under ``name``.

    A ``geom`` column makes a geometry item (rows are collected into one), an
    ``objectid`` column a featureset. Saving an existing name replaces it.
    ``height_m`` / ``extrude_m`` / ``base_z`` make the geometry 3D (``_shape``).
    ``alpha`` is the opacity (0-1, default DEFAULT_ALPHA; transparency = 1 - alpha).

    Optional columns: ``label`` (a computed label, e.g. a height, wins over the
    argument) and ``item_name``: one item per distinct value, named
    ``<name>_<value>``, with its own label and (``color`` column) colour; the
    report then lists the items (``_save_groups``)."""
    try:
        _check_user(user_id)
        if not valid_name(name):
            raise ValueError(f"invalid name {name!r}: letters, digits, _ (no spaces), max 64")
        color = color if isinstance(color, str) and _COLOR_RE.match(color) else DEFAULT_COLOR
        alpha = DEFAULT_ALPHA if alpha is None or alpha == "" else _check_alpha(alpha)
        inner = _inner_sql(sql)
        blocked = guard_sql(inner)
        if blocked:
            raise ValueError(blocked)
        qdb = qdb or run_query_target(_owner_reader())[0]
        wdb = wdb or writer_db()
        settings = user_settings(user_id)

        probe = qdb.execute(f"SELECT * FROM ({inner}) q LIMIT 1", settings=settings)
        if not probe:
            raise ValueError("the query returned no rows, nothing to save")
        columns = set(probe[0])
        if split and SPLIT_COLUMN in columns:
            return _save_groups(user_id, name, inner, columns, settings, qdb, wdb, color,
                                label=label, clamp=clamp, item_class=item_class, alpha=alpha,
                                size_m=size_m, height_m=height_m, extrude_m=extrude_m, base_z=base_z)
        notes = []
        if "label" in columns:
            # A computed label (height, distance) wins: it matches the saved geometry.
            rows = qdb.execute(f"SELECT string_agg(DISTINCT q.label::text, ', ') AS label, "
                               f"count(DISTINCT q.label) AS labels FROM ({inner}) q", settings=settings)
            computed = rows[0].get("label") if rows else None
            if computed:
                label = computed[:200]
            if rows and int(rows[0].get("labels") or 0) > 1:
                notes.append(f"{rows[0]['labels']} different labels were joined into one item label; "
                             "for one item per row, save again with an item_name column (one item per value)")
        if "geom" in columns:
            row = qdb.execute(_GEOM_SQL.format(inner=inner), settings=settings)[0]
            if not row.get("ewkb"):
                raise ValueError("the geom column is empty")
            _check_vertices(row)
            meta, cls, clamp, _ = _shape(qdb, row["ewkb"], clamp=bool(clamp), item_class=item_class or None,
                                         height_m=height_m, extrude_m=extrude_m, base_z=base_z,
                                         sphere_r=size_m)
            _check_vertices(meta)
            kind, ewkb, ids, count = "geometry", meta["shape_ewkb"], None, int(row.get("n") or 1)
            detail = type_label(meta)
        elif "objectid" in columns:
            if any(v is not None for v in (height_m, extrude_m, base_z)):
                raise ValueError(_SET_HAS_NO_GEOMETRY)
            row = qdb.execute(_FEATURESET_SQL.format(inner=inner), settings=settings)[0]
            count = int(row.get("n") or 0)
            if count > MAX_FEATURESET:
                raise ValueError(f"too many objects ({count}, max {MAX_FEATURESET})")
            if item_class and item_class != "FeatureSet":
                raise ValueError(f"class {item_class} needs a geom column, not objectid rows")
            kind, ewkb, ids, detail, cls = "featureset", None, list(row.get("ids") or []), "objectids", "FeatureSet"
        else:
            raise ValueError("the query needs a geom column (geometry) or an objectid column (building set)")

        schema = _pg_sql.Identifier(wdb.schema)
        others = wdb.execute(
            _pg_sql.SQL("SELECT count(*) AS n FROM {}.scratchpad WHERE user_id = %s AND name <> %s").format(schema),
            (user_id, name))
        if others and int(others[0]["n"]) >= MAX_ITEMS:
            raise ValueError(f"scratchpad full ({MAX_ITEMS} items); delete some first")
        data = {"sql": inner[:4000], "count": count}
        if isinstance(size_m, (int, float)) and size_m > 0:
            data["size_m"] = float(size_m)  # sphere radius / pipe radius in the viewer
        wdb.execute(_pg_sql.SQL(_UPSERT_SQL).format(schema=schema),
                    (user_id, name, label, kind, cls, color, alpha, bool(clamp), ewkb, ewkb, ids,
                     json.dumps(data)))
        _audit("save", user_id, name=name, kind=kind, **{"class": cls}, count=count, geometry_type=detail)
        # color and alpha as stored, so the model can report what was really applied
        out = {"ok": True, "name": name, "kind": kind, "class": cls, "count": count, "type": detail,
               "color": color, "alpha": alpha}
        if label:
            out["label"] = label
        if notes:
            out["note"] = " ".join(notes)
        return out
    except Exception as exc:
        return {"ok": False, "name": name, "error": str(exc)}


def apply_action(user_id: str, action: str, names: list,
                 wdb: DatabaseConnection | None = None, confirm: bool = False) -> dict:
    """show / hide / delete / goto for the user's own items. Deleting several
    items at once needs ``confirm`` (the model has to ask the user first)."""
    try:
        _check_user(user_id)
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r} (allowed: {', '.join(ACTIONS)})")
        names = [n for n in dict.fromkeys(names or []) if valid_name(n)]
        if not names:
            raise ValueError("no valid item names")
        if action == "delete" and len(names) > 1 and not confirm:
            return {"ok": False, "action": action, "needs_confirmation": True, "names": names,
                    "error": (f"deleting {len(names)} items ({', '.join(names)}) needs confirmation: "
                              "ask the user, and only after a yes call again with confirm=true")}
        wdb = wdb or writer_db()
        schema = _pg_sql.Identifier(wdb.schema)
        if action == "delete":
            query = "DELETE FROM {}.scratchpad WHERE user_id = %s AND name = ANY(%s) RETURNING name"
            params = (user_id, names)
        else:
            query = ("UPDATE {}.scratchpad SET is_visible = %s, updated_at = now() "
                     "WHERE user_id = %s AND name = ANY(%s) RETURNING name")
            params = (action != "hide", user_id, names)
        found = [r["name"] for r in wdb.execute(_pg_sql.SQL(query).format(schema), params)]
        if found and action != "goto":
            _audit(action, user_id, names=found)
        return {"ok": True, "action": action, "found": found,
                "missing": [n for n in names if n not in found]}
    except Exception as exc:
        return {"ok": False, "action": action, "error": str(exc)}


_ITEM_SQL = """
SELECT kind, clamp, (data->>'size_m')::float8 AS size_m, encode(ST_AsEWKB(geom), 'hex') AS ewkb
FROM {}.scratchpad WHERE user_id = %s AND name = %s"""


def update_item(user_id: str, name: str, *, label=None, color=None, alpha=None, item_class=None,
                size_m=None, clamp=None, height_m=None, extrude_m=None, base_z=None,
                wdb: DatabaseConnection | None = None) -> dict:
    """Change an item in place. Only given fields change. A new class, clamp,
    ``height_m``, ``extrude_m`` or ``base_z`` reshapes the stored geometry to
    match (``_shape``); other changes to the geometry or the objects are made
    by saving again under the same name. An empty label removes it (the map
    then shows the name)."""
    try:
        _check_user(user_id)
        if not valid_name(name):
            raise ValueError(f"invalid name {name!r}")
        wdb = wdb or writer_db()
        schema = _pg_sql.Identifier(wdb.schema)
        rows = wdb.execute(_pg_sql.SQL(_ITEM_SQL).format(schema), (user_id, name))
        if not rows:
            return {"ok": False, "name": name, "error": f"no saved item named {name!r}"}
        item = rows[0]
        height_m, extrude_m, base_z = (None if v is None else float(v) for v in (height_m, extrude_m, base_z))
        sets, params, changed = [], [], {}
        if label is not None:
            sets.append("label = %s"); params.append(label or None); changed["label"] = label or None
        if color is not None:
            if not (isinstance(color, str) and _COLOR_RE.match(color)):
                raise ValueError("color must be #rrggbb")
            sets.append("color = %s"); params.append(color); changed["color"] = color
        if alpha is not None:
            alpha = _check_alpha(alpha)
            sets.append("alpha = %s"); params.append(alpha); changed["alpha"] = alpha
        if size_m is not None:
            size_m = float(size_m)
            if size_m <= 0:
                raise ValueError("size_m must be positive")
        new_clamp = bool(item.get("clamp")) if clamp is None else bool(clamp)
        raising = any(v is not None for v in (height_m, extrude_m, base_z))
        if item["kind"] == "featureset":
            if item_class not in (None, "FeatureSet"):
                raise ValueError(f"{name} is a set of features; its class stays FeatureSet")
            if raising:
                raise ValueError(_SET_HAS_NO_GEOMETRY)
        elif item_class is not None or clamp is not None or raising:
            meta, cls, new_clamp, reshaped = _shape(
                wdb, item["ewkb"], clamp=new_clamp, item_class=item_class or None, height_m=height_m,
                extrude_m=extrude_m, base_z=base_z, sphere_r=size_m or item.get("size_m"))
            _check_vertices(meta)
            sets.append("class = %s"); params.append(cls); changed["class"] = cls
            if reshaped:
                sets.append("geom = ST_GeomFromEWKB(decode(%s, 'hex'))")
                params.append(meta["shape_ewkb"]); changed["geometry"] = type_label(meta)
        if clamp is not None or new_clamp != bool(item.get("clamp")):
            sets.append("clamp = %s"); params.append(new_clamp); changed["clamp"] = new_clamp
        if size_m is not None:
            sets.append("data = data || jsonb_build_object('size_m', %s::float8)")
            params.append(size_m); changed["size_m"] = size_m
        if not sets:
            raise ValueError("nothing to change (label, color, alpha, class, size_m, clamp, "
                             "height_m, extrude_m, base_z)")
        wdb.execute(_pg_sql.SQL("UPDATE {}.scratchpad SET " + ", ".join(sets) +
                                ", updated_at = now() WHERE user_id = %s AND name = %s").format(schema),
                    tuple(params) + (user_id, name))
        _audit("update", user_id, name=name, changed=changed)
        return {"ok": True, "name": name, "updated": changed}
    except Exception as exc:
        return {"ok": False, "name": name, "error": str(exc)}


# ── Export / import (the viewer's item list: a JSON file per user) ────────────
# Plain JSON, not GeoJSON: geometries stay in the database's CRS with their
# heights (EWKT), so another person with the same dataset gets exactly the
# same items. The file is untrusted on import: every field is checked, the
# geometry only ever reaches PostGIS as a bound parameter, and all rows are
# written in one statement (one transaction).

FILE_FORMAT = "3dcitydb-scratchpad"
FILE_VERSION = 1
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_LABEL_CHARS = 500
MAX_OBJECTID_CHARS = 256

_EXPORT_SQL = """
SELECT name, label, kind, class, color, alpha, clamp, is_visible, data, created_at, updated_at,
       ST_AsEWKT(geom) AS geom, objectids
FROM {schema}.scratchpad WHERE user_id = %s ORDER BY created_at, name"""

_DB_SRID_SQL = "SELECT srid FROM {schema}.database_srs LIMIT 1"

# Reads a geometry from the file (EWKT, bound parameter), gives it the file's
# SRID if it has none and transforms it to the database's; describes the result.
_IMPORT_SHAPE_SQL = """
WITH i AS (SELECT ST_GeomFromEWKT(%(ewkt)s) AS g),
s AS (SELECT CASE WHEN ST_SRID(i.g) = 0 THEN ST_SetSRID(i.g, %(file_srid)s) ELSE i.g END AS g FROM i),
t AS (SELECT CASE WHEN ST_SRID(s.g) <> %(db_srid)s THEN ST_Transform(s.g, %(db_srid)s) ELSE s.g END AS g
      FROM s)
SELECT ST_NPoints(g) AS vertices, GeometryType(g) AS gtype, ST_NDims(g) AS ndims,
       (SELECT array_agg(DISTINCT GeometryType(ST_GeometryN(g, k)))
        FROM generate_series(1, ST_NumGeometries(g)) k) AS members,
       (SELECT bool_and(GeometryType(ST_GeometryN(g, k)) IN ('POLYHEDRALSURFACE', 'TIN')
                        AND ST_IsClosed(ST_GeometryN(g, k)))
        FROM generate_series(1, ST_NumGeometries(g)) k) AS closed,
       encode(ST_AsEWKB(g), 'hex') AS shape_ewkb
FROM t"""

_FOUND_SQL = "SELECT count(DISTINCT objectid) AS n FROM {schema}.feature WHERE objectid = ANY(%s::text[])"

_IMPORT_ROW = ("(%s, %s, %s, %s, %s, %s, %s, %s, "
               "CASE WHEN %s::text IS NULL THEN NULL ELSE ST_GeomFromEWKB(decode(%s, 'hex')) END, "
               "%s::text[], %s, %s::jsonb, COALESCE(%s::timestamptz, now()))")

_IMPORT_SQL = """
INSERT INTO {{schema}}.scratchpad
    (user_id, name, label, kind, class, color, alpha, clamp, geom, objectids, is_visible, data, created_at)
VALUES {rows}
ON CONFLICT (user_id, name) DO UPDATE SET
    label = EXCLUDED.label, kind = EXCLUDED.kind, class = EXCLUDED.class, color = EXCLUDED.color,
    alpha = EXCLUDED.alpha, clamp = EXCLUDED.clamp, geom = EXCLUDED.geom,
    objectids = EXCLUDED.objectids, is_visible = EXCLUDED.is_visible, data = EXCLUDED.data,
    updated_at = now()
RETURNING name"""


def _db_srid(db: DatabaseConnection) -> int:
    rows = db.execute(_pg_sql.SQL(_DB_SRID_SQL).format(schema=_pg_sql.Identifier(db.schema)))
    if not rows or rows[0].get("srid") is None:
        raise ValueError("the database has no SRID (database_srs is empty)")
    return int(rows[0]["srid"])


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


GENERATOR_NAME = "3DCityDB MCP Server"
GENERATOR_URL = "https://github.com/tum-gis/3dcitydb-mcp-server"


def export_items(user_id: str, *, chatbot_version: str,
                 db: DatabaseConnection | None = None) -> dict:
    """All items of the user as the import/export file (a dict, see
    production/docs/scratchpad.md). Raises ValueError without a valid user.

    The file always says what wrote it and when: ``generator`` (name, MCP
    server and chatbot versions, URL) and ``exported_at`` (UTC). A later
    version that changes the format can tell old files apart by them.
    ``chatbot_version`` comes from the web UI (this package does not know it)."""
    import datetime

    from citydb_mcp import __version__ as mcp_version

    _check_user(user_id)
    db = db or _owner_reader()
    rows = db.execute(_pg_sql.SQL(_EXPORT_SQL).format(schema=_pg_sql.Identifier(db.schema)), (user_id,))
    items = []
    for r in rows:
        item = {"name": r["name"], "label": r.get("label"), "kind": r["kind"], "class": r.get("class"),
                "color": r.get("color") or DEFAULT_COLOR,
                "alpha": float(r["alpha"]) if r.get("alpha") is not None else 0.8,
                "clamp": bool(r.get("clamp")), "is_visible": bool(r.get("is_visible")),
                "data": r.get("data") if isinstance(r.get("data"), dict) else {},
                "created_at": _iso(r.get("created_at")), "updated_at": _iso(r.get("updated_at"))}
        if r["kind"] == "geometry":
            item["geom"] = r.get("geom")
        else:
            item["objectids"] = list(r.get("objectids") or [])
        items.append(item)
    out = {"format": FILE_FORMAT, "version": FILE_VERSION,
           "comment": (f"Scratchpad items exported from the {GENERATOR_NAME} chatbot. Import the file "
                       "in the item list of the 3D viewer (same dataset and coordinate system)."),
           "generator": {"name": GENERATOR_NAME, "mcp_server_version": mcp_version,
                         "chatbot_version": str(chatbot_version), "url": GENERATOR_URL},
           "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "srid": _db_srid(db), "items": items}
    _audit("export", user_id, count=len(items))
    return out


def _parse_file(payload) -> dict:
    """The file as a dict with format/version/items checked (ValueError otherwise)."""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_IMPORT_BYTES:
            raise ValueError(f"file too large (max {MAX_IMPORT_BYTES // (1024 * 1024)} MB)")
        payload = payload.decode("utf-8", errors="strict")
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError(f"file too large (max {MAX_IMPORT_BYTES // (1024 * 1024)} MB)")
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            raise ValueError("this is not a JSON file; only files exported from this list "
                             "can be imported") from None
    if not isinstance(payload, dict):
        raise ValueError("not a scratchpad export file (expected a JSON object)")
    if payload.get("format") != FILE_FORMAT:
        if payload.get("type") in ("FeatureCollection", "Feature", "GeometryCollection"):
            raise ValueError("this is a GeoJSON file; only scratchpad export files can be imported "
                             "(to add GeoJSON, paste it into the chat)")
        raise ValueError("not a scratchpad export file (it was not exported from this list)")
    if payload.get("version") != FILE_VERSION:
        raise ValueError(f"unsupported file version {payload.get('version')!r} (supported: {FILE_VERSION})")
    srid = payload.get("srid")
    if srid is not None and (isinstance(srid, bool) or not isinstance(srid, int) or not 0 < srid < 1_000_000):
        raise ValueError("srid must be a positive integer")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("the file has no item list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"too many items in the file ({len(items)}, max {MAX_ITEMS})")
    return payload


def _has_nul(value) -> bool:
    return "\x00" in value if isinstance(value, str) else False


def _check_item(it) -> dict:
    """One item of the file, checked and normalised (ValueError with the reason).
    The geometry is checked later in the database."""
    if not isinstance(it, dict):
        raise ValueError("not an object")
    name = it.get("name")
    if not valid_name(name):
        raise ValueError("invalid name (letters, digits, _; max 64)")
    kind = it.get("kind")
    if kind not in ("geometry", "featureset"):
        raise ValueError("kind must be geometry or featureset")
    label = it.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_CHARS or _has_nul(label)):
        raise ValueError(f"label must be text (max {MAX_LABEL_CHARS} characters)")
    color = it.get("color")
    color = color if isinstance(color, str) and _COLOR_RE.match(color) else DEFAULT_COLOR
    alpha = it.get("alpha", 0.8)
    if alpha is None:
        alpha = 0.8
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
        raise ValueError("alpha must be a number between 0 and 1")
    clamp, visible = it.get("clamp", False), it.get("is_visible", True)
    if not isinstance(clamp, bool) or not isinstance(visible, bool):
        raise ValueError("clamp and is_visible must be true or false")
    data = it.get("data")
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    data = dict(data)
    size_m = data.get("size_m")
    if size_m is not None and (isinstance(size_m, bool) or not isinstance(size_m, (int, float))
                               or not 0 < size_m < 1e6):
        data.pop("size_m")  # list_items casts it to a number
    data_json = json.dumps(data, ensure_ascii=False)
    if "\\u0000" in data_json:
        raise ValueError("data contains a NUL character")
    created = it.get("created_at")
    if created is not None:
        import datetime
        try:
            created = datetime.datetime.fromisoformat(str(created)).isoformat()
        except ValueError:
            created = None
    cls = it.get("class")
    if cls is not None and cls not in CLASSES:
        raise ValueError(f"unknown class {cls!r}")
    out = {"name": name, "label": label or None, "kind": kind, "class": cls, "color": color,
           "alpha": float(alpha), "clamp": clamp, "is_visible": visible, "data": data_json,
           "created_at": created, "geom": None, "objectids": None}
    if kind == "featureset":
        if cls not in (None, "FeatureSet"):
            raise ValueError(f"class {cls} does not fit a featureset (FeatureSet)")
        out["class"] = "FeatureSet"
        ids = it.get("objectids")
        if not isinstance(ids, list):
            raise ValueError("a featureset needs an objectids list")
        if len(ids) > MAX_FEATURESET:
            raise ValueError(f"too many objects ({len(ids)}, max {MAX_FEATURESET})")
        if not all(isinstance(i, str) and 0 < len(i) <= MAX_OBJECTID_CHARS and not _has_nul(i) for i in ids):
            raise ValueError("objectids must be non-empty strings")
        out["objectids"] = list(dict.fromkeys(ids))
    else:
        if cls == "FeatureSet":
            raise ValueError("class FeatureSet needs kind featureset")
        geom = it.get("geom")
        if not isinstance(geom, str) or not geom.strip() or _has_nul(geom):
            raise ValueError("a geometry item needs geom as EWKT text")
        out["geom"] = geom.strip()
    return out


def import_items(user_id: str, payload, wdb: DatabaseConnection | None = None) -> dict:
    """Add the items of an export file (str, bytes or dict) to the user's
    scratchpad; an existing name is replaced. Items that fail a check are
    skipped with the reason; a file that is not a scratchpad file writes nothing.
    All rows go in with one statement, so either all valid items are stored or none."""
    result = {"ok": False, "imported": 0, "replaced": [], "skipped": []}
    try:
        _check_user(user_id)
        data = _parse_file(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        result["error"] = str(exc)
        return result
    try:
        wdb = wdb or writer_db()
        schema = _pg_sql.Identifier(wdb.schema)
        skipped = result["skipped"]
        checked, seen = [], set()
        for idx, raw in enumerate(data["items"]):
            label = raw.get("name") if isinstance(raw, dict) and isinstance(raw.get("name"), str) else f"#{idx + 1}"
            try:
                item = _check_item(raw)
            except ValueError as exc:
                skipped.append({"name": label[:64], "reason": str(exc)})
                continue
            if item["name"] in seen:
                skipped.append({"name": item["name"], "reason": "duplicate name in the file"})
                continue
            seen.add(item["name"])
            checked.append(item)

        db_srid = _db_srid(wdb) if any(i["kind"] == "geometry" for i in checked) else None
        file_srid = data.get("srid") or db_srid
        missing_objects = {}
        ready = []
        for item in checked:
            try:
                if item["kind"] == "geometry":
                    try:
                        rows = wdb.execute(_IMPORT_SHAPE_SQL, {"ewkt": item["geom"], "file_srid": file_srid,
                                                               "db_srid": db_srid})
                    except Exception as exc:
                        raise ValueError("the geometry could not be read: "
                                         + str(exc).strip().splitlines()[0][:200]) from exc
                    meta = rows[0] if rows else {}
                    if not meta.get("shape_ewkb"):
                        raise ValueError("the geometry could not be read")
                    if not meta.get("vertices"):
                        raise ValueError("the geometry is empty")
                    _check_vertices(meta)
                    gtype = (meta.get("gtype") or "").upper()
                    if item["class"]:
                        # The class must fit the geometry type; heights are not
                        # checked (the viewer draws either way, as it was saved).
                        if gtype not in _CLASS_TYPES[item["class"]]:
                            raise ValueError(f"class {item['class']} does not fit a {gtype} geometry")
                    else:
                        item["class"] = classify(gtype, meta["ndims"], meta.get("members") or (),
                                                 bool(meta.get("closed")), item["clamp"])
                    item["ewkb"] = meta["shape_ewkb"]
                else:
                    item["ewkb"] = None
                    if item["objectids"]:
                        try:
                            found = wdb.execute(_pg_sql.SQL(_FOUND_SQL).format(schema=schema),
                                                (item["objectids"],))
                            n = len(item["objectids"]) - int(found[0]["n"] if found else 0)
                            if n > 0:
                                missing_objects[item["name"]] = n
                        except Exception as exc:  # only information, never fatal
                            print(f"[scratchpad] import: objectid check failed: {exc}", file=sys.stderr, flush=True)
            except ValueError as exc:
                skipped.append({"name": item["name"], "reason": str(exc)})
                continue
            ready.append(item)

        existing = {r["name"] for r in wdb.execute(
            _pg_sql.SQL("SELECT name FROM {}.scratchpad WHERE user_id = %s").format(schema), (user_id,))}
        free = MAX_ITEMS - len(existing)
        to_write = []
        for item in ready:
            if item["name"] not in existing:
                if free <= 0:
                    skipped.append({"name": item["name"], "reason": f"scratchpad full ({MAX_ITEMS} items)"})
                    continue
                free -= 1
            to_write.append(item)

        written = []
        if to_write:
            params = []
            for it in to_write:
                params += [user_id, it["name"], it["label"], it["kind"], it["class"], it["color"], it["alpha"],
                           it["clamp"], it["ewkb"], it["ewkb"], it["objectids"], it["is_visible"], it["data"],
                           it["created_at"]]
            query = _pg_sql.SQL(_IMPORT_SQL.format(rows=", ".join([_IMPORT_ROW] * len(to_write)))).format(
                schema=schema)
            written = [r["name"] for r in wdb.execute(query, tuple(params))]
        result.update(ok=True, imported=len(written),
                      replaced=[n for n in written if n in existing])
        if missing_objects:
            result["objects_not_found"] = missing_objects
        _audit("import", user_id, names=written, replaced=result["replaced"],
               skipped=[s["name"] for s in skipped])
        return result
    except Exception as exc:
        result.update(ok=False, imported=0, replaced=[], error=str(exc))
        return result


def touch_user(user_id: str, wdb: DatabaseConnection | None = None) -> None:
    """Keep the user's items alive (called once per chat turn)."""
    if not user_settings(user_id):
        return
    wdb = wdb or writer_db()
    wdb.execute(_pg_sql.SQL("UPDATE {}.scratchpad SET updated_at = now() WHERE user_id = %s").format(
        _pg_sql.Identifier(wdb.schema)), (user_id,))


def delete_user(user_id: str, wdb: DatabaseConnection | None = None) -> int:
    """Purge hook: remove all items of an expired user."""
    if not user_settings(user_id):
        return 0
    wdb = wdb or writer_db()
    rows = wdb.execute(_pg_sql.SQL("DELETE FROM {}.scratchpad WHERE user_id = %s RETURNING name").format(
        _pg_sql.Identifier(wdb.schema)), (user_id,))
    if rows:
        _audit("purge", user_id, names=[r.get("name") for r in rows])
    return len(rows)


# ── Viewer payload ────────────────────────────────────────────────────────────

_LIST_SQL = """
SELECT s.name, s.label, s.kind, s.class, s.color, s.alpha, s.clamp, s.is_visible,
       (s.data->>'size_m')::float8 AS size_m,
       GeometryType(s.geom) AS gtype,
       CASE WHEN s.is_visible THEN s.objectids END AS objectids,
       cardinality(s.objectids) AS count,
       CASE WHEN s.is_visible AND s.geom IS NOT NULL THEN (
           SELECT json_agg(ST_AsGeoJSON(ST_Transform(d.geom, 4326), 7)::json)
           FROM ST_Dump(s.geom) d) END AS parts,
       box.west, box.south, box.east, box.north, box.zmin, box.zmax
FROM {schema}.scratchpad s
LEFT JOIN LATERAL (
    SELECT ST_XMin(e.b) AS west, ST_YMin(e.b) AS south, ST_XMax(e.b) AS east,
           ST_YMax(e.b) AS north, ST_ZMin(e.g) AS zmin, ST_ZMax(e.g) AS zmax
    FROM (SELECT gg.g, ST_Transform(ST_Envelope(gg.g), 4326) AS b
          FROM (SELECT CASE WHEN s.kind = 'geometry' THEN s.geom
                            ELSE (SELECT ST_Collect(f.envelope) FROM {schema}.feature f
                                  WHERE f.objectid = ANY(s.objectids)) END AS g) gg
          WHERE gg.g IS NOT NULL) e
) box ON true
WHERE s.user_id = %s
ORDER BY s.created_at"""


def list_items(db: DatabaseConnection, user_id: str) -> list[dict]:
    """All items of the user for the viewer. Hidden items come without their
    geometry/ids (the list still shows them); geometry parts are GeoJSON in
    WGS84 with the original heights (the same convention as the 3D tiles)."""
    if not user_settings(user_id):
        return []
    rows = db.execute(_pg_sql.SQL(_LIST_SQL).format(schema=_pg_sql.Identifier(db.schema)), (user_id,))
    items = []
    for r in rows:
        bbox = None
        if r.get("west") is not None:
            bbox = [r["west"], r["south"], r["east"], r["north"], r.get("zmin"), r.get("zmax")]
        items.append({
            "name": r["name"], "label": r.get("label"), "kind": r["kind"],
            "class": r.get("class"), "size_m": r.get("size_m"),
            "color": r.get("color") or DEFAULT_COLOR, "alpha": r.get("alpha", 0.8),
            "clamp": bool(r.get("clamp")), "visible": bool(r.get("is_visible")),
            "type": r.get("gtype"), "count": r.get("count"),
            "objectids": r.get("objectids") or [], "parts": r.get("parts") or [], "bbox": bbox,
        })
    return items


# ── MCP tools (also handed to the web UI's chat backends) ─────────────────────
# One definition for the MCP server, the cloud backend (native tool calling)
# and the local ReAct backend, so all three describe the tools the same way.

_HEIGHT_PROPS = {
    "height_m": {"type": "number", "description":
                 "Height above the ground in metres (optional): makes the item 3D at that height "
                 "(a pin becomes a 3Dmarker, an area a Polygon3D). The ground is the lowest point "
                 "of the nearest city objects."},
    "extrude_m": {"type": "number", "description":
                  "Turn an area into a solid (a line into a wall) of this height in metres, "
                  "standing on the ground (optional)."},
    "base_z": {"type": "number", "description":
               "Absolute height (m, the data's height system) for the item's lowest point instead "
               "of the ground (optional)."},
}

TOOL_SCHEMAS = {
    "save_to_scratchpad": {
        "description": (
            "Save a result under a name in the user's personal scratchpad (shown in the 3D viewer "
            "when there is one, and reusable in later SQL through the view my_scratchpad). Give "
            "EITHER `sql`: a read-only SELECT returning an `objectid` column (saved as a set of "
            "features, e.g. buildings) or a `geom` column (saved as one geometry: point, line, "
            "polygon, solid; several rows are combined), OR `geojson`: a GeoJSON geometry, Feature "
            "or FeatureCollection in WGS84 lon/lat (e.g. points the user pasted). Saving an "
            "existing name replaces it. SEVERAL ITEMS IN ONE CALL: add an `item_name` column; "
            "every distinct value becomes its own item <name>_<value>, with its own `label` and "
            "(optional `color` column, #rrggbb) its own colour, e.g. one group per class, one "
            "marker per building, one line per pair. "
            "Returns whether it was saved, how many objects and the "
            "stored geometry type (\"Z\" = with heights). A 3D bounding box is "
            "`ST_3DExtent(...) AS geom` (stored as a closed box, Solid3D), a 2D one "
            "`ST_Extent(...) AS geom` (ST_MakeEnvelope is 2D only). height_m / extrude_m make "
            "a 2D geometry 3D."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description":
                         "Identifier: letters, digits, underscore, no spaces, e.g. affected_2013. "
                         "With an item_name column it is the prefix (energy -> energy_A, energy_B)."},
                "sql": {"type": "string", "description":
                        "SELECT returning objectid rows or a geom column (not both needed); "
                        "optional columns: label, item_name (one item per value), color."},
                "geojson": {"description": "GeoJSON (object or string) instead of sql."},
                "label": {"type": "string", "description": "Text shown on the map (optional)."},
                "color": {"type": "string", "description": "Colour as #rrggbb (optional)."},
                "alpha": {"type": "number", "description":
                          "Opacity 0-1 (optional, default 0.8). Transparency = 1 - alpha: "
                          "30 % transparency = 0.7."},
                "clamp": {"type": "boolean", "description":
                          "true = drape a 2D shape on the terrain (optional)."},
                "class": {"type": "string", "enum": list(GEOMETRY_CLASSES), "description":
                          "How to draw a geometry (optional, derived from the geometry "
                          "otherwise): 2Dmarker/3Dmarker = pin with label, Point3D = sphere, "
                          "LineString3D = pipe, Solid3D = closed solid, ..."},
                "size_m": {"type": "number", "description":
                           "Radius in metres for Point3D spheres and LineString3D pipes (optional)."},
                **_HEIGHT_PROPS,
            },
            "required": ["name"],
        },
    },
    "scratchpad_action": {
        "description": (
            "Show, hide, delete or fly to saved scratchpad items by name. "
            "action: show | hide | delete | goto. Deleting more than one item needs confirm=true, "
            "which you may only set after the user has confirmed that list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "names": {"type": "array", "items": {"type": "string"}},
                "confirm": {"type": "boolean", "description":
                            "true only after the user confirmed deleting several items."},
            },
            "required": ["action", "names"],
        },
    },
    "update_scratchpad_item": {
        "description": (
            "Change a saved item in place: label, color, alpha (opacity 0-1), class, size_m "
            "(sphere/pipe radius), clamp, and its height. Only the given fields change. To give "
            "an item a height or make an area a solid use height_m / extrude_m: the stored "
            "geometry is converted and the class follows (2Dmarker -> 3Dmarker, Polygon2D -> "
            "Polygon3D or Solid3D). A 3D class on a 2D item puts it on the ground with heights, "
            "a 2D class removes the heights. For other changes save again under the same name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "label": {"type": "string", "description": "New map text; empty removes it."},
                "color": {"type": "string", "description": "#rrggbb"},
                "alpha": {"type": "number", "description": "Opacity of areas and solids, 0-1."},
                "class": {"type": "string", "enum": list(GEOMETRY_CLASSES)},
                "size_m": {"type": "number"},
                "clamp": {"type": "boolean"},
                **_HEIGHT_PROPS,
            },
            "required": ["name"],
        },
    },
    "list_scratchpad": {
        "description": (
            "List the user's saved scratchpad items (name, label, kind, colour, visibility, "
            "number of objects or geometry type). For calculations query the view my_scratchpad "
            "with run_query instead."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
}

_MCP_CLIENT_USER = os.getenv("CITYDB_SCRATCHPAD_MCP_USER", "")


def effective_user_id(ctx_user_id) -> str | None:
    """The web UI's user, or for MCP clients without a session context
    (Claude Desktop, VS Code) one shared scratchpad: CITYDB_SCRATCHPAD_MCP_USER
    or a fixed uuid."""
    if user_settings(ctx_user_id):
        return ctx_user_id
    if user_settings(_MCP_CLIENT_USER):
        return _MCP_CLIENT_USER
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, "citydb-mcp-scratchpad-client"))


MAX_GEOJSON_CHARS = 256 * 1024


def _geojson_sql(geojson) -> str:
    """A SELECT yielding one geom row per GeoJSON geometry. The geometries go in
    as quoted literals (single quotes doubled), never as SQL text."""
    if isinstance(geojson, str):
        if len(geojson) > MAX_GEOJSON_CHARS:
            raise ValueError(f"GeoJSON too large (max {MAX_GEOJSON_CHARS // 1024} kB)")
        try:
            geojson = json.loads(geojson)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid GeoJSON: {exc}") from exc
    if not isinstance(geojson, dict):
        raise ValueError("GeoJSON must be an object")
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        geoms = [f.get("geometry") for f in geojson.get("features") or [] if isinstance(f, dict)]
    elif kind == "Feature":
        geoms = [geojson.get("geometry")]
    else:
        geoms = [geojson]
    geoms = [g for g in geoms if isinstance(g, dict) and g.get("type")]
    if not geoms:
        raise ValueError("GeoJSON contains no geometry")
    literals = ", ".join("'" + json.dumps(g).replace("'", "''") + "'" for g in geoms)
    return (f"SELECT ST_GeomFromGeoJSON(g) AS geom "
            f"FROM unnest(ARRAY[{literals}]::text[]) AS t(g)")


_SUMMARY_SQL = """
SELECT name, label, class, color, is_visible AS visible,
       cardinality(objectids) AS objects,
       GeometryType(geom) || CASE WHEN ST_NDims(geom) > 2 THEN ' Z' ELSE '' END AS geometry_type,
       to_char(created_at, 'YYYY-MM-DD HH24:MI') AS created
FROM {schema}.scratchpad WHERE user_id = %s ORDER BY created_at"""


def _number(args: dict, key: str) -> float | None:
    """A numeric tool argument (models sometimes send "20" or "20 m"); None if absent."""
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*m?\s*", str(value))
    if not match:
        raise ValueError(f"{key} must be a number of metres, not {value!r}")
    return float(match.group(1).replace(",", "."))


def run_tool(name: str, arguments: dict, ctx_user_id=None) -> dict:
    """Execute one of TOOL_SCHEMAS for the effective user. The result carries
    the report under "scratchpad" (save/action) so callers can react to it."""
    user_id = effective_user_id(ctx_user_id)
    args = arguments or {}
    schema = TOOL_SCHEMAS.get(name)
    if schema is not None:
        # An argument the tool does not know used to be dropped without a word,
        # so the model reported e.g. a transparency that was never applied.
        # null values count as absent (some models send every optional field).
        allowed = schema["inputSchema"]["properties"]
        unknown = sorted(k for k, v in args.items() if k not in allowed and v is not None)
        if unknown:
            return {"scratchpad": {"ok": False, "name": args.get("name"),
                                   "error": f"unknown argument(s) {', '.join(unknown)}; {name} takes: "
                                            f"{', '.join(allowed)}. Nothing was changed."}}
    if name == "save_to_scratchpad":
        sql, geojson = args.get("sql"), args.get("geojson")
        if bool(sql) == bool(geojson):
            return {"scratchpad": {"ok": False, "name": args.get("name"),
                                   "error": "give exactly one of sql or geojson"}}
        try:
            query = sql if sql else _geojson_sql(geojson)
        except ValueError as exc:
            return {"scratchpad": {"ok": False, "name": args.get("name"), "error": str(exc)}}
        clamp = args.get("clamp")
        clamp = clamp if isinstance(clamp, bool) else str(clamp).lower() in ("true", "1", "yes")
        try:
            numbers = {k: _number(args, k) for k in ("size_m", "height_m", "extrude_m", "base_z")}
        except ValueError as exc:
            return {"scratchpad": {"ok": False, "name": args.get("name"), "error": str(exc)}}
        return {"scratchpad": save_item(user_id, str(args.get("name") or ""), query,
                                        label=args.get("label"), color=args.get("color"),
                                        alpha=args.get("alpha"), clamp=clamp,
                                        item_class=args.get("class"), **numbers)}
    if name == "scratchpad_action":
        names = args.get("names")
        if isinstance(names, str):
            names = [names]
        confirm = args.get("confirm") is True or str(args.get("confirm")).lower() == "true"
        return {"scratchpad": apply_action(user_id, str(args.get("action") or ""), names or [],
                                           confirm=confirm)}
    if name == "update_scratchpad_item":
        try:
            numbers = {k: _number(args, k) for k in ("size_m", "height_m", "extrude_m", "base_z")}
        except ValueError as exc:
            return {"scratchpad": {"ok": False, "name": args.get("name"), "error": str(exc)}}
        return {"scratchpad": update_item(
            user_id, str(args.get("name") or ""), label=args.get("label"), color=args.get("color"),
            alpha=args.get("alpha"), item_class=args.get("class"), clamp=args.get("clamp"), **numbers)}
    if name == "list_scratchpad":
        db = _owner_reader()
        rows = db.execute(_pg_sql.SQL(_SUMMARY_SQL).format(schema=_pg_sql.Identifier(db.schema)),
                          (user_id,))
        return {"items": rows, "count": len(rows)}
    raise ValueError(f"unknown scratchpad tool {name!r}")
