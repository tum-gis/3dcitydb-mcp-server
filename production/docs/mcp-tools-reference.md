# MCP Tools Reference

All tools are exposed in **read-only mode** (the default). Write tools require `--mode=readwrite --i-understand-the-risks`.

---

## Always available

### `assemble_prompt()`

Builds the complete system prompt for the agent. Orchestrates all other tools and returns a structured Markdown document covering:

- Database metadata (SRS, bounding box, feature counts)
- Object class hierarchy with property definitions
- Codelist values for enumerated attributes
- SQL examples tailored to the present object classes
- Query guidelines and indexed columns

**Call once at the start of every session.** The agent uses this as its primary context.

---

### `run_query(sql)`

Executes a read-only SQL SELECT against the database.

- Parses the SQL with `sqlglot` — rejects anything that isn't `SELECT`, `WITH … SELECT`, `UNION`, or `EXPLAIN`
- Wraps execution in `BEGIN READ ONLY` / `ROLLBACK`
- Results capped at **500 rows**

Returns rows as a list of dicts.

---

### `scan_objectclasses()`

Returns the full object class hierarchy: IDs, names, superclass chains, and which classes have geometry.

---

### `resolve_properties(objectclass_id)`

Returns all properties defined for an object class, including:

- Property name, namespace, datatype
- Value column (`val_string`, `val_double`, `val_int`, `val_timestamp`, etc.)
- Codelist entries (if the property is enumerated)
- Nested child properties (via `parent_id` chain)

---

### `get_generic_attributes()`

Returns generic (user-defined) attributes with:

- Categorical detection (distinct values ≤ threshold → categorical)
- Sample values for categorical attributes
- Min/max ranges for numeric attributes
- Null percentage (when row count ≥ minimum)

---

### `get_db_context_snapshot()`

Returns:

- CRS / SRID
- Bounding box (WGS 84 and native)
- Feature counts by object class
- Available Levels of Detail

---

### `get_lod_config()`

Lists which LoD levels are present in `geometry_data`, grouped by object class.

---

### `get_examples(objectclass_ids)`

Returns 8 curated SQL examples adapted to the object classes present in the database:

1. Volume calculation (SFCGAL `CG_Volume`)
2. Surface area (SFCGAL `CG_3DArea`)
3. 1-hop property join
4. 2-hop nested property join
5. Existence check (EXISTS subquery)
6. CTE with arithmetic
7. Spatial intersection (ST_Intersects)
8. Vertical section / height range

---

### `get_database_schema()`

Returns table definitions, column names/types, and foreign key relationships for the full 3DCityDB v5 schema. Cached for the session lifetime.

---

### `get_query_guidelines()`

Returns SQL best practices specific to 3DCityDB v5:

- Which columns are indexed
- How to join `property` to `feature`
- Geometry access patterns
- Codelist lookup patterns
- Common pitfalls

Cached for the session lifetime.

---

## Read-write mode only

Exposed only when the server is started with `--mode=readwrite --i-understand-the-risks`.

### `update_property(feature_id, property_name, new_value, namespace_id=None)`

Changes a single property's value on a single feature.

- Validates feature and property exist
- Determines correct value column from `datatype_id`
- Wraps in transaction; rolls back on error
- Records change in audit log

Returns old value, new value, and audit ID.

---

### `replace_geometry(feature_id, new_wkt, srid, geometry_type)`

Replaces a feature's geometry.

- Validates WKT via `ST_GeomFromText` before any UPDATE
- Refuses cross-CRS replacement
- For Solid types validates `ST_IsClosed`
- Updates `geometry_data`, `geometry_properties`, and `feature.envelope`
- Wraps in transaction; rolls back on error
- Records change in audit log

Returns geometry summary and audit ID.

---

## Audit log

Every write is appended to `./citydb-mcp-audit.log` (override with `CITYDB_MCP_AUDIT_LOG`). Each entry is a JSONL line with timestamp, audit ID, operation, feature ID, old/new values, and whether the transaction committed.

See [MODES.md](../../MODES.md) for the full risk model.
