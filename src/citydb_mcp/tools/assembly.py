"""Prompt assembly - orchestrates all tools into a system prompt."""

import json
from datetime import datetime
from ..db import DatabaseConnection
from ..models import (
    ObjectClassCatalog, DBContextSnapshot, LoDConfig,
    GenericAttribute, ExamplesLibrary, DatabaseSchema, QueryGuidelines,
    PropertyDefinition, CodeListDefinition
)
from .static_tools import get_database_schema, get_query_guidelines
from .dynamic_tools import (
    get_spatial_capabilities, scan_objectclasses, resolve_properties, get_generic_attributes,
    get_db_context_snapshot, get_lod_config, get_examples, get_geometry_types_per_class,
    get_vocabulary, synthesize_examples, get_static_codelists,
)

# Compact schema constant — 5 core tables, only query-relevant columns
_COMPACT_SCHEMA = """\
## Database Schema (core tables)

| table | columns used in queries |
|-------|------------------------|
| feature | id, objectclass_id (→objectclass.id), objectid, envelope |
| property | id, feature_id (→feature.id), parent_id (→property.id), name, namespace_id, val_string, val_int, val_double, val_timestamp, val_address_id (→address.id), val_feature_id (→feature.id), val_relation_type |
| address | id, street, house_number, zip_code, city |
| geometry_data | id, feature_id (→feature.id), geometry, geometry_properties (JSON — type code: 6=CompositeSurface, 8=MultiSurface → CG_3DArea; 9=Solid, 10=CompositeSolid, 11=MultiSolid → CG_Volume) |
| objectclass | id, classname, is_toplevel, namespace_id |"""

# Semantic hints for non-toplevel CityGML classes so the LLM can map
# natural-language terms (e.g. "balcony") to the correct objectclass.
_CLASS_SEMANTIC_HINTS = {
    # ── Building boundary surfaces ──────────────────────────────────────────
    "ClosureSurface":        "⚠️ NO GEOMETRY in this dataset. Virtual face closing an open solid.",
    "WallSurface":           "exterior wall faces. Query geometry_data directly; also links to Window/DoorSurface children via rel=1.",
    "GroundSurface":         "✅ HAS GEOMETRY. Footprint / base of building touching the ground.",
    "RoofSurface":           "✅ HAS GEOMETRY. Roof faces.",
    "OuterCeilingSurface":   "✅ HAS GEOMETRY. Underside of overhanging parts (e.g. balcony soffit).",
    "OuterFloorSurface":     "✅ HAS GEOMETRY. Top face of protruding parts (e.g. balcony floor from outside).",
    "CeilingSurface":        "interior ceiling faces.",
    "FloorSurface":          "interior floor faces.",
    "InteriorWallSurface":   "interior wall faces.",
    # ── Openings ────────────────────────────────────────────────────────────
    "WindowSurface":         "✅ HAS GEOMETRY. Windows, skylights, glass facades. Child of WallSurface via rel=1.",
    "DoorSurface":           "✅ HAS GEOMETRY. Doors, gates, garage doors. Child of WallSurface via rel=1.",
    # ── Installations ───────────────────────────────────────────────────────
    "BuildingInstallation":  "balconies, chimneys, dormers, bay windows, outside staircases, antennae. ✅ mostly HAS GEOMETRY. Can have own WallSurface children via rel=1.",
    "BuildingPart":          "✅ HAS GEOMETRY. Sub-volume of a building (e.g. annex, tower).",
    "IntBuildingInstallation": "interior installations (stairs, elevators, fixed furniture).",
    # ── Building rooms / subdivisions ───────────────────────────────────────
    "BuildingRoom":          "interior rooms.",
    "BuildingUnit":          "apartments, office units, condominiums.",
    "Storey":                "building floors / storeys.",
    # ── Transportation ──────────────────────────────────────────────────────
    # Confirmed relationship map (all hops rel=1 unless noted):
    #   Road→Section/Intersection→TrafficSpace→TrafficArea(geometry)
    #   Road→Section/Intersection→AuxiliaryTrafficSpace→AuxiliaryTrafficArea(geometry)
    #   Road→Section/Intersection→Marking(geometry)
    #   TrafficSpace→[rel=0]→TrafficSpace, CityFurniture, SolitaryVegetationObject, AuxiliaryTrafficSpace
    "Road":                  "⚠️ NO GEOMETRY. Container. Chain to geometry: Road→Section/Intersection[rel=1]→TrafficSpace[rel=1]→TrafficArea[rel=1].",
    "Section":               "⚠️ NO GEOMETRY. Road segment. Chain: Section→TrafficSpace(610)[rel=1]→TrafficArea(613)[rel=1]→geometry_data. Also links to AuxTrafficSpace(608)[rel=1] and Marking(614)[rel=1].",
    "Intersection":          "⚠️ NO GEOMETRY. Road junction. Chain: Intersection→TrafficSpace(610)[rel=1]→TrafficArea(613)[rel=1]→geometry_data. Also links to AuxTrafficSpace(608)[rel=1] and Marking(614)[rel=1].",
    "TrafficSpace":          "✅ mostly HAS GEOMETRY (18/1460 missing). Drivable lane. Boundary: →TrafficArea(613)[rel=1]. Also contains CityFurniture/SolitaryVegetation/AuxTrafficSpace via rel=0.",
    "AuxiliaryTrafficSpace": "✅ mostly HAS GEOMETRY (2/1501 missing). Sidewalk/cycle lane/shoulder. Boundary: →AuxiliaryTrafficArea(612)[rel=1].",
    "TrafficArea":           "✅ HAS GEOMETRY. Actual lane surface polygon. Direct geometry_data join.",
    "AuxiliaryTrafficArea":  "✅ HAS GEOMETRY. Sidewalk/cycle-lane surface. Direct geometry_data join.",
    "Marking":               "✅ HAS GEOMETRY. Road markings (lines, arrows, zebra crossings). Direct geometry_data join.",
    "ClearanceSpace":        "vertical clearance envelope above a traffic space.",
    "Hole":                  "opening/gap in a traffic space surface.",
    "HoleSurface":           "surface geometry of a hole in the road.",
    # ── Vegetation ──────────────────────────────────────────────────────────
    "SolitaryVegetationObject": "✅ HAS GEOMETRY. Individual trees or shrubs. Also found inside TrafficSpace via rel=0.",
    "PlantCover":            "vegetation area (meadow, forest patch).",
    # ── Relief ──────────────────────────────────────────────────────────────
    "TINRelief":             "terrain surface as a triangulated irregular network.",
    "MassPointRelief":       "terrain represented by mass points.",
    "BreaklineRelief":       "terrain breaklines (ridges, valleys).",
    # ── Bridge / Tunnel ─────────────────────────────────────────────────────
    "BridgeConstructiveElement": "structural elements of bridges (girders, piers, decks).",
    "BridgeInstallation":    "bridge installations (railings, lamps, signs).",
    "TunnelInstallation":    "tunnel installations (ventilation, signage).",
}


def assemble_prompt(
    db: DatabaseConnection,
    include_query_agent_extras: bool = True,
    compact: bool = False,
) -> str:
    """Assembles the complete system prompt from all components.

    Args:
        db: Database connection
        include_query_agent_extras: Include SQL examples and query guidelines.
        compact: Use compact rendering for local models with small context windows.
                 Skips full property trees and verbose schema (~200 lines vs 600-1000).
    """
    # ── Gather components ────────────────────────────────────────────────────
    schema = get_database_schema(db) if not compact else None
    guidelines = get_query_guidelines(db) if include_query_agent_extras else None

    catalog = scan_objectclasses(db)
    db_context = get_db_context_snapshot(db)
    spatial_caps = get_spatial_capabilities(db)
    lod_config = get_lod_config(db)
    geom_types = get_geometry_types_per_class(db)

    toplevel_ids = {oc.id for oc in catalog.object_classes if oc.is_toplevel}
    generic_attrs = get_generic_attributes(db, filter_objectclass_ids=toplevel_ids)

    epsg_code = db_context.epsg_code

    # Resolve full property trees only in full mode
    if not compact:
        for oc in catalog.object_classes:
            if oc.is_toplevel:
                oc.resolved_properties = resolve_properties(db, oc.id, epsg_code=epsg_code)

    # Vocabulary: street names + generic attr values (TTL-cached)
    vocab = get_vocabulary(db)

    # Static codelists for quick-ref and example synthesizer
    static_cl = get_static_codelists(epsg_code)

    available_ids = [oc.id for oc in catalog.object_classes]
    available_classnames = {oc.classname for oc in catalog.object_classes}
    examples = get_examples(available_ids, classnames=available_classnames) if include_query_agent_extras else None

    # Concrete synthesized examples using real DB values
    synth_examples = synthesize_examples(db, catalog, static_cl, vocab)

    # ── Render sections ──────────────────────────────────────────────────────
    sections = []

    # 1. Quick reference — always first for maximum attention weight
    sections.append(_render_quickref(db, db_context, catalog, static_cl))

    # 2. Known values: streets + generic attribute vocabulary.
    #    In compact mode, numeric generic attrs are merged here so everything
    #    is in one place (string attrs come from vocab, numeric from generic_attrs).
    if vocab.street_names or vocab.generic_attr_values or (compact and generic_attrs):
        sections.append(_render_vocabulary(vocab, numeric_generic_attrs=generic_attrs if compact else None))

    # 3. Schema
    if compact:
        sections.append(_COMPACT_SCHEMA)
    else:
        sections.append(_render_database_schema(schema))

    # 4. DB context
    sections.append(_render_db_context(db_context, toplevel_ids))

    # 5. LoD
    sections.append(_render_lod_config(lod_config))

    # 6. Object classes
    sections.append(_render_objectclasses(catalog, compact=compact))

    # 7. Spatial functions
    sections.append(_render_spatial_capabilities(spatial_caps))

    # 8. Geometry type guide
    #    Compact: one-liner only — the full table and per-class breakdown are dropped.
    #    Full: complete reference table + dataset-specific types.
    if compact:
        sections.append(
            "## Geometry Type Reference\n\n"
            "`geometry_properties->>'type'` codes: **9, 10, 11** → volume (Solid/CompositeSolid/MultiSolid, use `CG_Volume`); "
            "**6, 8** → surface area (CompositeSurface/MultiSurface, use `CG_3DArea`). "
            "Always filter by type code — a feature can have multiple geometry_data rows."
        )
    elif geom_types:
        sections.append(_render_geometry_type_guide(geom_types))

    # 9. Generic attributes
    #    Compact: already merged into section 2 (Known Values) — not repeated here.
    #    Full: complete table with all attrs, value columns, and ranges.
    if not compact and generic_attrs:
        sections.append(_render_generic_attributes(generic_attrs))

    # 10. Synthesized examples (concrete, real values)
    if synth_examples:
        sections.append(_render_synthesized_examples(synth_examples))

    # 11. Query guidelines
    #     Compact: 3 essential rules only.
    #     Full: complete guidelines.
    if guidelines:
        if compact:
            sections.append(_render_query_guidelines_compact())
        else:
            sections.append(_render_query_guidelines(guidelines))

    # 12. Abstract SQL patterns — omitted in compact (concrete examples cover this).
    if not compact and examples:
        sections.append(_render_examples(examples))

    return "\n\n".join(sections)


# ============================================================
# Render functions for each component
# ============================================================

def _distinct_property_codes(db: DatabaseConnection, name: str, namespace_id: int) -> list[str]:
    """Return sorted list of distinct val_string codes present in the DB for a property."""
    try:
        rows = db.execute(
            "SELECT DISTINCT val_string FROM property "
            "WHERE name = %s AND namespace_id = %s AND val_string IS NOT NULL "
            "ORDER BY val_string",
            (name, namespace_id),
        )
        return [r["val_string"] for r in rows]
    except Exception:
        return []


def _lookup_codelist_entries(db: DatabaseConnection, property_name: str, codes: list[str]) -> dict:
    """Return {code: definition} for codes found in codelist_entry, matched by property name."""
    if not codes:
        return {}
    try:
        codelists = db.execute("SELECT id, codelist_type FROM codelist")
        matched_id = None
        prop_lower = property_name.lower()
        for cl in codelists:
            if prop_lower in cl["codelist_type"].lower():
                matched_id = cl["id"]
                break
        if matched_id is None:
            return {}
        placeholders = ",".join(["%s"] * len(codes))
        entries = db.execute(
            f"SELECT code, definition FROM codelist_entry "
            f"WHERE codelist_id = %s AND code IN ({placeholders}) AND definition IS NOT NULL",
            (matched_id, *codes),
        )
        return {str(e["code"]): e["definition"] for e in entries}
    except Exception:
        return {}


def _render_quickref(db: DatabaseConnection, db_context, catalog, static_cl: dict) -> str:
    """Quick-reference block rendered first — highest attention weight for local models."""
    lines = ["## Quick Reference", ""]

    # Only show codes that are actually present in the database AND have a known label.
    all_func_codes = static_cl.get("function", {})
    db_func_codes = _distinct_property_codes(db, "function", 10)
    if db_func_codes:
        # Codes not in the static list: try codelist_entry table
        missing = [c for c in db_func_codes if c not in all_func_codes]
        db_func_labels = _lookup_codelist_entries(db, "function", missing) if missing else {}
        resolved = []
        for code in db_func_codes:
            label = all_func_codes.get(code) or db_func_labels.get(code)
            if label:
                resolved.append((code, label))
        if resolved:
            lines.append("")
            lines.append("### Building function codes (property.name='function', namespace_id=10, val_string)")
            for code, label in resolved:
                lines.append(f"- {code} = {label}")

    all_roof_codes = static_cl.get("roofType", {})
    db_roof_codes = _distinct_property_codes(db, "roofType", 8)
    if db_roof_codes:
        missing_roof = [c for c in db_roof_codes if c not in all_roof_codes]
        db_roof_labels = _lookup_codelist_entries(db, "roofType", missing_roof) if missing_roof else {}
        resolved_roof = []
        for code in db_roof_codes:
            label = all_roof_codes.get(code) or db_roof_labels.get(code)
            if label:
                resolved_roof.append((code, label))
        if resolved_roof:
            lines.append("")
            lines.append("### Roof type codes (property.name='roofType', namespace_id=8, val_string)")
            for code, label in resolved_roof:
                lines.append(f"- {code} = {label}")

    return "\n".join(lines)


def _render_vocabulary(vocab, numeric_generic_attrs: dict | None = None) -> str:
    """Street names and generic attribute values — frequency ordered.

    numeric_generic_attrs: optional dict from _collect_numeric_generic_attrs(),
    merged into the Generic attribute vocabulary list so numeric attrs
    (val_int / val_double) appear alongside the string ones in compact mode.
    """
    _STRING_COLS = {"val_string", "val_uri"}
    lines = ["## Known Values in This Database", ""]

    if vocab.street_names:
        lines.append("### Street names (max. 20 values, ordered by frequency)")
        parts = [f"{name} ({cnt})" for name, cnt in vocab.street_names]
        lines.append(", ".join(parts))
        lines.append("")
        lines.append("Use `ILIKE '%street%'` for matching (handles umlauts and partial names).")

    # Merge string attrs (from vocab) and numeric attrs (from full generic_attrs scan).
    # Build a combined dict keyed by attr name so we can sort them together.
    combined: dict[str, str] = {}
    if vocab.generic_attr_values:
        for attr_name, vals in vocab.generic_attr_values.items():
            top = ", ".join(f"`{v}`" for v, _ in vals[:10])
            combined[attr_name] = top

    if numeric_generic_attrs:
        for _oc_id, info in numeric_generic_attrs.items():
            for attr in info["attrs"]:
                if attr.value_column in _STRING_COLS or attr.value_column in (None, "various"):
                    continue
                if attr.min_value is not None and attr.max_value is not None:
                    combined[attr.name] = f"{attr.value_column}, {attr.min_value}–{attr.max_value}"
                else:
                    combined[attr.name] = attr.value_column

    if combined:
        lines.append("")
        lines.append("### Generic attribute vocabulary (namespace_id = 3)")
        lines.append("Query: `JOIN property p ON p.feature_id = f.id AND p.namespace_id = 3 AND p.name = '<attr>'`")
        for attr_name, detail in sorted(combined.items()):
            lines.append(f"- **{attr_name}**: {detail}")

    return "\n".join(lines)


def _render_synthesized_examples(examples: list) -> str:
    """Concrete SQL examples built from real DB values."""
    lines = ["## Example Queries (Built from This Database)", ""]
    lines.append("Real objectclass_ids, function codes, and street names from this database.")
    lines.append("")
    for i, sql in enumerate(examples, 1):
        lines.append(f"### Example {i}")
        lines.append(f"\n```sql\n{sql}\n```")
        lines.append("")
    return "\n".join(lines)


def _render_spatial_capabilities(caps: dict) -> str:
    lines = ["## Spatial Functions", ""]
    lines.append("### PostGIS")
    lines.append(", ".join(caps["postgis_functions"]))

    if caps["sfcgal"]:
        lines.append("")
        lines.append("### SFCGAL (3D Operations)")
        for func in caps["sfcgal_functions"]:
            lines.append(f"  - {func}")

    return "\n".join(lines)


def _render_geometry_type_guide(geom_types: dict) -> str:
    """
    Renders a section explaining geometry_properties type codes and showing
    which geometry types are available per objectclass in this dataset.
    """
    lines = ["## Geometry Type Reference", ""]
    lines.append("The `geometry_properties` column in `geometry_data` is a JSON object that describes")
    lines.append("the outermost geometry type. Use `(g.geometry_properties->>'type')::int` to filter")
    lines.append("geometry_data rows to the right kind for your query:")
    lines.append("")
    lines.append("| type code | GML geometry kind       | Use for                           |")
    lines.append("|-----------|-------------------------|-----------------------------------|")
    lines.append("| 1         | Point                   | —                                 |")
    lines.append("| 2         | MultiPoint              | —                                 |")
    lines.append("| 3         | LineString              | —                                 |")
    lines.append("| 4         | MultiLineString         | —                                 |")
    lines.append("| 5         | Polygon (single face)   | Leaf surface — usually not targeted directly |")
    lines.append("| 6         | CompositeSurface        | Surface area (CG_3DArea)          |")
    lines.append("| 7         | TriangulatedSurface     | Surface area (CG_3DArea)          |")
    lines.append("| 8         | MultiSurface            | Surface area (CG_3DArea)          |")
    lines.append("| 9         | Solid                   | Volume (CG_Volume + CG_MakeSolid) |")
    lines.append("| 10        | CompositeSolid          | Volume (CG_Volume + CG_MakeSolid) |")
    lines.append("| 11        | MultiSolid              | Volume (CG_Volume + CG_MakeSolid) |")
    lines.append("")
    lines.append("**IMPORTANT:** A single feature may have multiple geometry_data rows (e.g. one Solid for")
    lines.append("volume AND one MultiSurface for surface area). Always filter by type code to avoid")
    lines.append("processing the wrong geometry or joining duplicate rows.")
    lines.append("")
    lines.append("**Example filter patterns:**")
    lines.append("```sql")
    lines.append("-- Volume query: target Solid / CompositeSolid / MultiSolid rows")
    lines.append("JOIN geometry_data g ON g.feature_id = f.id")
    lines.append("WHERE (g.geometry_properties->>'type')::int IN (9, 10, 11)")
    lines.append("  AND g.geometry IS NOT NULL")
    lines.append("")
    lines.append("-- Surface area query: target CompositeSurface / MultiSurface rows")
    lines.append("JOIN geometry_data g ON g.feature_id = f.id")
    lines.append("WHERE (g.geometry_properties->>'type')::int IN (6, 8)")
    lines.append("  AND g.geometry IS NOT NULL")
    lines.append("```")
    lines.append("")
    lines.append("### Geometry Types Present in This Dataset (per objectclass)")
    lines.append("")
    lines.append("| objectclass_id | classname | type code | geometry kind | row count |")
    lines.append("|----------------|-----------|-----------|---------------|-----------|")

    TYPE_LABELS = {
        1: "Point",
        2: "MultiPoint",
        3: "LineString",
        4: "MultiLineString",
        5: "Polygon (leaf)",
        6: "CompositeSurface",
        7: "TriangulatedSurface",
        8: "MultiSurface",
        9: "Solid",
        10: "CompositeSolid",
        11: "MultiSolid",
    }

    for oc_id, info in sorted(geom_types.items()):
        classname = info["classname"]
        for t in info["types"]:
            code = t["code"]
            label = TYPE_LABELS.get(code, f"type {code}")
            lines.append(f"| {oc_id} | {classname} | {code} | {label} | {t['count']} |")

    lines.append("")
    return "\n".join(lines)


_SCHEMA_NARRATIVE = """\
3DCityDB v5 organises its tables into five logical modules:

**Feature module** — the core of the schema. Every city object (building, road, \
vegetation, etc.) is a row in `feature`, identified by `objectclass_id`. \
Semantic attributes (height, function, address link, geometry link, …) are stored \
as rows in `property`, linked back to `feature` via `feature_id`. \
The `objectclass` table defines the class hierarchy (e.g. Building → AbstractBuilding \
→ AbstractCityObject). Relationships between features (e.g. Building → WallSurface) \
are encoded as `property` rows where `val_feature_id` points to the child feature and \
`val_relation_type` indicates the relationship kind (0 = space, 1 = boundary).

**Geometry module** — explicit 3D geometry lives in `geometry_data`, one row per \
geometry object, linked to its owning feature via `feature_id`. The \
`geometry_properties` JSON column encodes the outermost geometry type (9=Solid, \
6=CompositeSurface, …). Implicit (template-based) geometry is stored in \
`implicit_geometry` and referenced from `property.val_implicitgeom_id`.

**Appearance module** — textures, materials, and surface colour information. These \
tables (`appearance`, `surface_data`, …) are present in the schema but are not \
relevant for analytical queries and are excluded from this reference.

**Metadata module** — the `namespace` table maps namespace IDs to their URI prefixes \
(e.g. namespace_id=1 → CityGML core, namespace_id=3 → generic attributes, \
namespace_id=8 → building module). Always use `namespace_id` together with \
`property.name` to unambiguously identify a property.

**Codelist module** — the `codelist` table registers named codelists \
(e.g. `bldg:RoofTypeValue`). `codelist_entry` holds the individual code–definition \
pairs. Code-type properties (datatype_id=14) store their value in \
`property.val_string`; join `codelist_entry` on `code` to obtain the human-readable \
definition.
"""


_TABLE_DESCRIPTIONS = {
    "feature": (
        "One row per city object (building, road, tree, …). "
        "`objectclass_id` identifies the class; `objectid` is the GML identifier used for "
        "map highlighting; `envelope` is the 2D/3D bounding box used for spatial pre-filtering."
    ),
    "property": (
        "One row per attribute value of a feature. Every semantic attribute — height, function, "
        "address link, geometry link, relationship to a child feature — is stored here. "
        "Use `name` + `namespace_id` to identify a property unambiguously. "
        "`val_relation_type` encodes feature-to-feature relationships "
        "(0 = space/composition, 1 = boundary surface). "
        "Nested properties (e.g. height → value) are linked via `parent_id`."
    ),
    "objectclass": (
        "Class registry for all CityGML object types. "
        "`superclass_id` encodes the inheritance chain (e.g. Building → AbstractBuilding). "
        "`is_toplevel=1` marks root-level objects that can exist independently; "
        "`schema` (JSON) carries the property definitions for that class."
    ),
    "geometry_data": (
        "Explicit 3D geometry storage. Each row holds one geometry object (solid, surface, etc.) "
        "linked to its owning feature via `feature_id`. "
        "`geometry_properties` (JSON) encodes the outermost geometry type code "
        "(9=Solid, 10=CompositeSolid, 6=CompositeSurface, 8=MultiSurface, …) — "
        "always filter by this to avoid joining the wrong geometry rows."
    ),
    "address": (
        "Postal addresses linked to features via `property.val_address_id`. "
        "Use `ILIKE '%street%'` on the `street` column for fuzzy name matching."
    ),
    "codelist": (
        "Registry of named codelists (e.g. `bldg:RoofTypeValue`, `bldg:BuildingFunctionValue`). "
        "Join with `codelist_entry` on `id` to resolve code strings to human-readable definitions."
    ),
    "codelist_entry": (
        "Individual code–definition pairs for each codelist. "
        "Join on `codelist_id` and match `code` against `property.val_string` "
        "for Code-type properties (datatype_id = 14)."
    ),
}


def _render_database_schema(schema: DatabaseSchema) -> str:
    rel_data = json.loads(schema.relationships) if schema.relationships else {}
    table_details = rel_data.get("table_details", {})

    lines = ["## Database Schema (3DCityDB v5)", ""]
    lines.append(_SCHEMA_NARRATIVE)
    lines.append("### Key Tables")
    for table_name, columns in table_details.items():
        desc = _TABLE_DESCRIPTIONS.get(table_name, "")
        lines.append(f"\n**{table_name}**:{('  ' + desc) if desc else ''}")
        for col in columns:
            flags = []
            if col.get("pk"):
                flags.append("PK")
            if col.get("fk"):
                flags.append(col["fk"])
            if col.get("not_null") and not col.get("pk"):
                flags.append("NOT NULL")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"  - {col['column']}: {col['type']}{flag_str}")

    return "\n".join(lines)


def _render_db_context(ctx, toplevel_ids: set = None) -> str:
    lines = ["## Database Context", ""]
    lines.append(f"- EPSG Code: {ctx.epsg_code}")
    sc = ctx.spatial_context
    if sc.srid_is_2d and sc.coord_dim == 3:
        z_ref = sc.z_reference or "meters above sea level"
        lines.append(
            f"- ⚠️ **Note:** EPSG:{ctx.epsg_code} is a 2D coordinate reference system, "
            f"but all geometries in this database carry Z coordinates "
            f"(height values in {z_ref}). "
        )
    lines.append(f"- Bounding Box: {sc.bounding_box}")

    lines.append("")
    lines.append("### Feature Counts (toplevel classes only)")
    for oc_id, info in ctx.statistics.features_per_class.items():
        if toplevel_ids and oc_id not in toplevel_ids:
            continue
        if isinstance(info, dict):
            lines.append(f"  - {info['classname']} (objectclass_id: {oc_id}): {info['count']} features")
        else:
            lines.append(f"  - objectclass_id {oc_id}: {info} features")

    return "\n".join(lines)


def _render_lod_config(lod_config) -> str:
    lines = ["## Level of Detail (LoD)", ""]
    multi_lod = len(lod_config.supported_lods) > 1

    if not multi_lod:
        # Single-LoD dataset — one line is sufficient, no agent guidance needed.
        lines.append(f"This dataset uses **LoD {lod_config.default_lod}** only. No LoD filtering is needed.")
        return "\n".join(lines)

    # Multi-LoD dataset — full explanation + agent instructions.
    lines.append(
        "In CityGML, **Level of Detail** (LoD0–LoD4) describes the geometric complexity "
        "of a feature. LoD0 is the coarsest (2D footprint); LoD1 adds a block extrusion; "
        "LoD2 introduces roof structures; LoD3 adds windows and doors; LoD4 adds interiors. "
        "Higher LoD means more geometry and more accurate area/volume results."
    )
    lines.append("")
    lines.append(
        "⚠️ **Critical — this dataset has multiple LoDs.** A single feature can have geometry "
        "at several LoDs simultaneously in `geometry_data`. Aggregating geometric calculations "
        "(area, volume, distance) without a LoD filter will **double- or triple-count** the "
        "same feature. Always restrict to exactly one LoD:"
    )
    lines.append("")
    lines.append("```sql")
    lines.append("-- Restrict geometry_data to a single LoD via the linking property row")
    lines.append("JOIN property lod_p")
    lines.append("  ON lod_p.feature_id = f.id")
    lines.append(f"  AND lod_p.val_lod = '{lod_config.default_lod}'   -- replace with desired LoD")
    lines.append("  AND lod_p.val_geometry_id = g.id")
    lines.append("```")
    lines.append("")
    lines.append("### LoDs Present in This Dataset")
    lines.append("")

    lod_notes = {
        "0": "2D footprint / roof edge — no volume",
        "1": "Block model — approximate volume",
        "2": "Roof structure — recommended for most calculations",
        "3": "Architectural detail — windows, doors",
        "4": "Interior — rarely available",
    }
    lines.append("| LoD | Property rows | Notes |")
    lines.append("|-----|--------------|-------|")
    for lod in sorted(lod_config.lod_counts.keys()):
        cnt = lod_config.lod_counts[lod]
        note = lod_notes.get(str(lod), "")
        default_marker = " ✅ **(default)**" if str(lod) == str(lod_config.default_lod) else ""
        lines.append(f"| {lod} | {cnt:,} | {note}{default_marker} |")

    lines.append("")
    lines.append(
        f"**Default LoD: `{lod_config.default_lod}`** (most common in this dataset). "
        "When the user does not specify a LoD, use the default and state it in your answer, "
        f'e.g. *"Calculated using LoD{lod_config.default_lod} geometry."*'
    )
    return "\n".join(lines)


def _render_objectclasses(catalog: ObjectClassCatalog, compact: bool = False) -> str:
    lines = ["## Available Object Classes and Properties", ""]

    toplevel = [oc for oc in catalog.object_classes if oc.is_toplevel]
    non_toplevel = [oc for oc in catalog.object_classes if not oc.is_toplevel]

    if compact:
        lines.append("| ID | Class | Toplevel | Notes |")
        lines.append("|----|-------|----------|-------|")
        for oc in sorted(catalog.object_classes, key=lambda x: x.id):
            tl = "✅" if oc.is_toplevel else ""
            hint = _CLASS_SEMANTIC_HINTS.get(oc.classname, "")
            lines.append(f"| {oc.id} | {oc.classname} | {tl} | {hint} |")
        lines.append("")
        lines.append("**Boundary surfaces join (val_relation_type=1):**")
        lines.append("```sql")
        lines.append("JOIN property rel ON rel.feature_id = f.id AND rel.val_relation_type = 1")
        lines.append("JOIN feature s ON s.id = rel.val_feature_id AND s.objectclass_id = <id>")
        lines.append("```")
        return "\n".join(lines)

    # --- Full detail for toplevel classes ---
    for oc in toplevel:
        prefix = oc.identifier if oc.identifier else oc.classname
        lines.append(f"### {prefix} (ID: {oc.id}, Namespace ID: {oc.namespace_id})")
        lines.append("")

        if oc.resolved_properties:
            lines.append("**Properties:**")
            for prop in oc.resolved_properties:
                lines.append(_render_property(prop))
            lines.append("")

        if oc.classname in ("Building", "BuildingPart"):
            lines.append("**Geometry:**")
            lines.append("  Always query geometry_data directly (JOIN geometry_data g ON g.feature_id = f.id).")
            lines.append("  A single building may have multiple geometry_data rows (one solid, one surface).")
            lines.append("  Filter by geometry type using: (g.geometry_properties->>'type')::int")
            lines.append("    - Volume queries:       WHERE (g.geometry_properties->>'type')::int IN (9, 10, 11)  -- Solid / CompositeSolid / MultiSolid")
            lines.append("    - Surface area queries: WHERE (g.geometry_properties->>'type')::int IN (6, 8)  -- CompositeSurface / MultiSurface")
            lines.append("  If geometry_data is empty/null, fall back to boundary surfaces (GroundSurface, RoofSurface, etc.).")
            lines.append("  Volume: CG_Volume(CG_MakeSolid(g.geometry)) — geometry must be closed (ST_IsClosed = true).")
            lines.append("")
            lines.append("**Boundary Surfaces & Installations (1-hop, all rel=1):**")
            lines.append("  WallSurface(709), RoofSurface(712), GroundSurface(710), OuterCeilingSurface(716),")
            lines.append("  OuterFloorSurface(714), ClosureSurface(15)⚠️NO GEOM, BuildingInstallation(905), BuildingPart(902)")
            lines.append("    JOIN property p ON p.feature_id = f.id AND p.val_relation_type = 1")
            lines.append("    JOIN feature s ON s.id = p.val_feature_id AND s.objectclass_id = <ID>")
            lines.append("")
            lines.append("**Window/Door Surfaces (2-hop, both rel=1):**")
            lines.append("  WindowSurface(719) and DoorSurface(718) are children of WallSurface, not Building.")
            lines.append("    JOIN property p1 ON p1.feature_id = f.id AND p1.val_relation_type = 1")
            lines.append("    JOIN feature wall ON wall.id = p1.val_feature_id AND wall.objectclass_id = 709")
            lines.append("    JOIN property p2 ON p2.feature_id = wall.id AND p2.val_relation_type = 1")
            lines.append("    JOIN feature win ON win.id = p2.val_feature_id AND win.objectclass_id = 719")
            lines.append("")
            lines.append("**BuildingInstallation own surfaces (2-hop, both rel=1):**")
            lines.append("  BuildingInstallation can have its own WallSurface children.")
            lines.append("    JOIN property p1 ON p1.feature_id = f.id AND p1.val_relation_type = 1")
            lines.append("    JOIN feature inst ON inst.id = p1.val_feature_id AND inst.objectclass_id = 905")
            lines.append("    JOIN property p2 ON p2.feature_id = inst.id AND p2.val_relation_type = 1")
            lines.append("    JOIN feature ws ON ws.id = p2.val_feature_id AND ws.objectclass_id = 709")
            lines.append("")

    # --- Compact summary for non-toplevel classes ---
    if non_toplevel:
        lines.append("### Non-Toplevel Classes")
        lines.append("")
        lines.append("⚠️ IMPORTANT: These are FEATURE ROWS in the `feature` table, identified by `objectclass_id`.")
        lines.append("Do NOT look for them as property values (val_string, val_int, etc.) — they do not appear in the `property` table as values.")
        lines.append("To find parent features (e.g. buildings) that HAVE a related non-toplevel feature, use the relationship join:")
        lines.append("")
        lines.append("```sql")
        lines.append("-- Example: buildings that have a BuildingInstallation (e.g. balcony)")
        lines.append("SELECT DISTINCT b.objectid")
        lines.append("FROM feature b")
        lines.append("JOIN property p  ON p.feature_id = b.id AND p.val_relation_type = 1")
        lines.append("JOIN feature  inst ON inst.id = p.val_feature_id AND inst.objectclass_id = <ID>")
        lines.append("WHERE b.objectclass_id = <Building_ID>;")
        lines.append("```")
        lines.append("")
        lines.append("Replace `<ID>` with the objectclass_id from the table below. Never search for class names in val_string.")
        lines.append("")
        lines.append("| ID | Class | Identifier | Namespace ID | Typical real-world features |")
        lines.append("|----|-------|------------|--------------|----------------------------|")
        for oc in non_toplevel:
            identifier = oc.identifier if oc.identifier else oc.classname
            hint = _CLASS_SEMANTIC_HINTS.get(oc.classname, "")
            lines.append(f"| {oc.id} | {oc.classname} | {identifier} | {oc.namespace_id} | {hint} |")
        lines.append("")

    return "\n".join(lines)


def _render_property(prop: PropertyDefinition) -> str:
    # Geometry properties: render as a single compact line — no need for full detail
    if prop.type == "core:GeometryProperty":
        return f"  - **{prop.name}** → geometry_data (JOIN via val_geometry_id)"

    parts = [f"  - **{prop.name}** ({prop.type}) ns:{prop.namespace_id}"]

    if prop.value_column:
        parts.append(f"    - col: `{prop.value_column}`")

    if prop.description:
        parts.append(f"    - {prop.description}")

    if prop.join_table:
        parts.append(
            f"    - JOIN: `{prop.join_table}` via "
            f"`{prop.join_from_column}` → `{prop.join_to_column}`"
        )

    # Flag composite/nested types that need parent_id access
    if prop.type in ("con:Height", "con:Elevation", "core:Occupancy",
                     "core:QualifiedArea", "core:QualifiedVolume",
                     "core:ExternalReference", "core:CityObjectRelation"):
        parts.append(f"    - ⚠️ NESTED TYPE: Access via parent_id chain.")
        parts.append(f"      JOIN property parent ON parent.feature_id = f.id AND parent.name = '{prop.name}'")
        parts.append(f"      JOIN property child ON child.parent_id = parent.id AND child.name = 'value'")

    if prop.is_deprecated:
        parts.append(f"    - ⚠️ DEPRECATED")

    if prop.codelist and prop.codelist.entries:
        # Check if this was flagged as free text (too many distinct values)
        if len(prop.codelist.entries) == 1 and "distinct values" in prop.codelist.entries[0].code:
            parts.append(f"    - Free text ({prop.codelist.entries[0].code})")
        else:
            # Skip codelist section if ALL entries are unresolved (code == value)
            # and there are very few entries (not a real classification)
            all_unresolved = all(e.code == e.value for e in prop.codelist.entries)
            if all_unresolved and len(prop.codelist.entries) <= 3:
                # Not worth showing — just a few raw values, not a real codelist
                pass
            else:
                has_unresolved = any(e.code == e.value for e in prop.codelist.entries)
                parts.append(f"    - CodeList ({prop.codelist.codelist_name}):")
                if has_unresolved:
                    parts.append(f"      ⚠️ Some codes lack definitions. Update codelist_entry table with country-specific mappings.")
                for entry in prop.codelist.entries:
                    parts.append(f"      - `{entry.code}` → {entry.value}")

    return "\n".join(parts)


def _render_generic_attributes(attrs_by_class: dict) -> str:
    lines = ["## Generic Attributes", ""]
    lines.append("Stored in `property` table with `namespace_id = 3`. Always filter by `f.objectclass_id`.")
    lines.append("")

    for oc_id, info in sorted(attrs_by_class.items()):
        classname = info["classname"]
        attrs = info["attrs"]

        lines.append(f"### {classname} (objectclass_id: {oc_id})")
        lines.append("")
        lines.append("| attribute | col | values / range |")
        lines.append("|-----------|-----|----------------|")

        for attr in attrs:
            if attr.value_column == "various":
                # Grouped metadata prefix
                prefix_raw = attr.name.split("*")[0].strip()
                sub = ", ".join(attr.sample_values) if attr.sample_values else ""
                detail = f"group — LIKE '{prefix_raw}%'; sub-attrs: {sub}" if sub else f"group — LIKE '{prefix_raw}%'"
                lines.append(f"| {attr.name} | various | {detail} |")
                continue

            if attr.is_categorical and attr.distinct_values:
                detail = ", ".join(f"`{v}`" for v in attr.distinct_values)
            elif attr.sample_values:
                cnt = f" ({attr.distinct_value_count} distinct)" if attr.distinct_value_count else ""
                detail = ", ".join(f"`{v}`" for v in attr.sample_values) + cnt
            elif not attr.is_categorical and attr.min_value is not None and attr.max_value is not None:
                detail = f"{attr.min_value} – {attr.max_value}"
            else:
                detail = ""

            # Range on its own line only for non-categorical attrs that also have samples
            range_str = ""
            if not attr.is_categorical and attr.min_value is not None and attr.max_value is not None and attr.sample_values:
                range_str = f" | range {attr.min_value}–{attr.max_value}"

            lines.append(f"| {attr.name} | `{attr.value_column}` | {detail}{range_str} |")

        lines.append("")

    return "\n".join(lines)


def _render_generic_attributes_compact(attrs_by_class: dict) -> str:
    """Compact listing of generic attributes — numeric/non-string attrs only.

    String attributes are already visible via the vocabulary section.
    Numeric attrs (val_int, val_double) are invisible there, so we must list
    them here even in compact mode so the model can query them correctly.
    """
    _STRING_COLS = {"val_string", "val_uri"}
    lines = ["## Generic Attributes (namespace_id = 3, compact)", ""]
    lines.append("Query pattern: `JOIN property p ON p.feature_id = f.id AND p.namespace_id = 3 AND p.name = '<attr>'`")
    lines.append("")

    any_written = False
    for oc_id, info in sorted(attrs_by_class.items()):
        classname = info["classname"]
        # Numeric / timestamp attrs not covered by the vocabulary section.
        non_string = [a for a in info["attrs"] if a.value_column not in _STRING_COLS and a.value_column not in (None, "various")]
        if not non_string:
            continue
        any_written = True
        parts = []
        for attr in non_string:
            if attr.min_value is not None and attr.max_value is not None:
                parts.append(f"`{attr.name}` ({attr.value_column}, {attr.min_value}–{attr.max_value})")
            else:
                parts.append(f"`{attr.name}` ({attr.value_column})")
        lines.append(f"**{classname}** (objectclass_id: {oc_id}): {', '.join(parts)}")

    if not any_written:
        return ""  # nothing to add — all attrs are string/categorical
    return "\n".join(lines)


def _render_query_guidelines_compact() -> str:
    """Three non-obvious rules that the examples alone don't make clear."""
    return "\n".join([
        "## Query Guidelines",
        "",
        "- Always filter by `objectclass_id` — never scan the full feature table without it.",
        "- Nested properties (e.g. `height`, `elevation`) store the numeric value in a **child** row: "
        "`JOIN property child ON child.parent_id = parent.id AND child.name = 'value'` — "
        "the parent row's `val_*` columns are NULL.",
        "- A single feature can have multiple `geometry_data` rows (one Solid, one MultiSurface). "
        "Always add `WHERE (g.geometry_properties->>'type')::int IN (9,10,11)` for volume or `IN (6,8)` for surface area to avoid duplicate rows in aggregations.",
    ])


def _render_query_guidelines(guidelines: QueryGuidelines) -> str:
    lines = ["## Query Guidelines", ""]

    lines.append("### Rules")
    for rule in guidelines.rules:
        lines.append(f"- {rule}")

    lines.append("")
    lines.append("### Optimization Tips")
    for tip in guidelines.query_optimization_tips:
        lines.append(f"- {tip}")

    lines.append("")
    lines.append("### Expensive Operations (Avoid)")
    for op in guidelines.expensive_operations:
        lines.append(f"- {op}")

    return "\n".join(lines)


def _render_examples(examples: ExamplesLibrary) -> str:
    lines = ["## SQL Query Patterns", ""]
    lines.append("These patterns cover every query shape in 3DCityDB v5.")
    lines.append("Substitute <PLACEHOLDER> values with objectclass_ids from the non-toplevel class table above.")
    lines.append("Patterns 0 and 1 show the geometry_properties type filter — always use it to avoid processing wrong/duplicate geometry rows.")
    lines.append("")

    pattern_labels = {
        "0_volume_query":    "Pattern 0 — Volume query (Solid geometry, type IN (9,10,11))",
        "1_direct_query":    "Pattern 1 — Surface area query (CompositeSurface/MultiSurface, type IN (6,8))",
        "2_boundary_1hop":   "Pattern 2 — 1-hop boundary relationship (val_relation_type = 1)",
        "3_space_1hop":      "Pattern 3 — 1-hop space relationship (val_relation_type = 0)",
        "4_chain_2hop":      "Pattern 4 — 2-hop chain (grandparent → intermediate → leaf)",
        "5_exists_filter":   "Pattern 5 — EXISTS filter (parent has at least one child of type X)",
        "6_cte_arithmetic":  "Pattern 6 — CTE arithmetic (subtract / compare two aggregations)",
        "7_intersection_surface": "Pattern 7 — Intersection/Section surface area (confirmed 2-hop, both rel=1)",
        "8_containment":          "Pattern 8 — Spatial containment (X inside Y) — fallback when no explicit relationship exists",
    }

    if examples.examples_by_pattern:
        for key, query in examples.examples_by_pattern.items():
            label = pattern_labels.get(key, key)
            lines.append(f"### {label}")
            lines.append(f"\n```sql\n{query}\n```")
            lines.append("")
    else:
        # Fallback: legacy class-specific examples
        for oc_id, queries in examples.examples_by_objectclass.items():
            lines.append(f"### Examples for ObjectClass {oc_id}")
            for query in queries:
                lines.append(f"\n```sql\n{query}\n```")
            lines.append("")

    return "\n".join(lines)