"""Static tools - called once and cached for session lifetime."""

import json
from ..db import DatabaseConnection
from ..models import DatabaseSchema, QueryGuidelines


def get_database_schema(db: DatabaseConnection) -> DatabaseSchema:
    """
    Returns 3DCityDB v5 table structures and relationships.
    Maps to UML: DatabaseSchema
    """
    # Get all tables in the citydb schema
    tables = db.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = %s 
        ORDER BY table_name
    """, (db.schema,))

    table_names = [t["table_name"] for t in tables]

    
    core_tables = {"feature", "property", "objectclass", "geometry_data",
                   "address", "codelist", "codelist_entry", "namespace",
                   "implicit_geometry"}
    # Get column details for key tables
    key_tables = ["feature", "property", "objectclass", "geometry_data",
                  "address", "codelist", "codelist_entry"]
    # Only include important columns per table
    ESSENTIAL_COLUMNS = {
        "feature": ["id", "objectclass_id", "objectid", "identifier", "envelope",
                    "creation_date", "termination_date"],
        "property": ["id", "feature_id", "parent_id", "datatype_id", "namespace_id",
                     "name", "val_int", "val_double", "val_string", "val_timestamp",
                     "val_uri", "val_codespace", "val_uom", "val_lod",
                     "val_geometry_id", "val_address_id", "val_feature_id",
                     "val_relation_type", "val_implicitgeom_id"],
        "objectclass": ["id", "superclass_id", "classname", "is_abstract",
                        "is_toplevel", "namespace_id", "schema"],
        "geometry_data": ["id", "geometry", "implicit_geometry", "geometry_properties", "feature_id"],
        "address": ["id", "street", "house_number", "zip_code", "city",
                    "state", "country", "multi_point"],
        "codelist": ["id", "codelist_type"],
        "codelist_entry": ["id", "codelist_id", "code", "definition"],
    }
    table_details = {}
    for table in key_tables:
        if table in table_names:
            cols = db.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (db.schema, table))
            # Get primary keys for this table
            pks = db.execute("""
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                AND tc.table_schema = %s AND tc.table_name = %s
            """, (db.schema, table))
            pk_columns = {pk["column_name"] for pk in pks}

            # Get foreign keys for this table
            fks = db.execute("""
                SELECT kcu.column_name, ccu.table_name AS ref_table, ccu.column_name AS ref_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                AND tc.table_schema = %s AND tc.table_name = %s
            """, (db.schema, table))
            fk_map = {fk["column_name"]: f"FK → {fk['ref_table']}.{fk['ref_column']}" for fk in fks}
            # Map PostgreSQL USER-DEFINED to actual type names
            TYPE_MAP = {}
            for c in cols:
                if c["data_type"] == "USER-DEFINED":
                    # Query actual type
                    udt = db.execute("""
                        SELECT udt_name FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s AND column_name = %s
                    """, (db.schema, table, c["column_name"]))
                    if udt:
                        TYPE_MAP[c["column_name"]] = udt[0]["udt_name"]

            table_details[table] = [
                {
                    "column": c["column_name"],
                    "type": TYPE_MAP.get(c["column_name"], c["data_type"]),
                    "pk": c["column_name"] in pk_columns,
                    "fk": fk_map.get(c["column_name"], ""),
                    "not_null": c["is_nullable"] == "NO",
                }
                for c in cols
                if table not in ESSENTIAL_COLUMNS
                or c["column_name"] in ESSENTIAL_COLUMNS[table]
            ]
    # Build relationships from per-table FK data
    relationships = []
    for table, columns in table_details.items():
        for col in columns:
            if col.get("fk"):
                relationships.append(f"{table}.{col['column']} {col['fk']}")

    # Only keep core relationships useful for query construction
    core_relationship_prefixes = {
        "property.feature_id", "property.val_geometry_id", "property.val_address_id",
        "property.val_feature_id", "property.parent_id", "property.val_implicitgeom_id",
        "geometry_data.feature_id", "codelist_entry.codelist_id",
        "objectclass.superclass_id", "objectclass.namespace_id",
    }
    relationships = [
        r for r in relationships
        if any(cr in r for cr in core_relationship_prefixes)
    ]
    return DatabaseSchema(
        tables=table_names,
        relationships=json.dumps({
            "table_details": table_details,
            "foreign_keys": relationships
        }, indent=2),
        version="5.0",
        schema_hash=""
    )


def get_query_guidelines(db: DatabaseConnection) -> QueryGuidelines:
    """
    Returns SQL best practices and performance hints for 3DCityDB.
    Maps to UML: QueryGuidelines
    """
    # Get indexed columns
    indexes = db.execute("""
        SELECT
            tablename,
            indexname,
            indexdef
        FROM pg_indexes
        WHERE schemaname = %s
        ORDER BY tablename, indexname
    """, (db.schema,))

    indexed_columns = [
        f"{idx['tablename']}: {idx['indexname']}"
        for idx in indexes
    ]

    return QueryGuidelines(
        rules=[
            "Always filter by objectclass_id when querying features to avoid full table scans.",
            "Use namespace_id to distinguish between schema properties (namespace_id != 3) and generic attributes (namespace_id = 3).",
            "For Code-type properties (datatype_id = 14), values are stored in val_string with optional val_codespace.",
            "Always use the property name AND namespace_id together for unambiguous property identification.",
            "For spatial queries, use PostGIS functions on the envelope column in the feature table for fast filtering.",
            "Use val_lod in property table to filter geometry by Level of Detail.",
            "When counting or aggregating, always include objectclass_id in GROUP BY for clarity.",
            "Feature properties (datatype_id = 10) link features via val_feature_id — use this for relationships like building→buildingPart.",
            "Buildings link to their boundary surfaces (roof, wall, ground) via property table: val_relation_type = 1 and val_feature_id points to the surface feature.",
            "For hierarchical/nested properties like height, use parent_id chain: JOIN property parent ON parent.name = 'height' then JOIN property child ON child.parent_id = parent.id AND child.name = 'value'. The actual value is in child.val_double.",
            "For 3D volume calculations, use CG_Volume(CG_MakeSolid(geometry)). Geometry must be a closed PolyhedralSurface.",
            "For true 3D surface area (accounting for tilted surfaces), use CG_3DArea(geometry). ST_Area only gives 2D projected area.",
            "Always check ST_IsClosed(geometry) = true before volume/solid calculations to avoid errors.",
            "Filter geometry_data by type using (g.geometry_properties->>'type')::int: use IN (8,9) for Solid/CompositeSolid (volume), IN (3,4) for MultiSurface/CompositeSurface (area). A feature can have multiple geometry_data rows — always filter by type to avoid duplicates.",
            "CG_3DDistance(geomA, geomB) gives true 3D distance between geometries.",
        ], 
        category="3dcitydb_v5",
        severity="recommended",
        indexed_columns=indexed_columns,
        materialized_views=[],
        query_optimization_tips=[
            "Prefer envelope-based spatial filtering before expensive geometry operations.",
            "Use EXISTS instead of IN for subqueries on large feature sets.",
            "Limit result sets when exploring data — use LIMIT clause.",
            "For property queries, always include feature_id index condition.",
        ],
        expensive_operations=[
            "Full geometry intersection without envelope pre-filter",
            "Unfiltered JOIN between feature and property without objectclass_id",
            "SELECT * on property table without namespace_id filter",
            "Recursive queries on parent_id without depth limit",
        ],
        recommended_batch_size=1000,
    )
