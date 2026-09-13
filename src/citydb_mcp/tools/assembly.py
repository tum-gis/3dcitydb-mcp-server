"""Prompt assembly - orchestrates all tools into a system prompt."""

import json
import threading
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
    get_vocabulary, synthesize_examples, get_static_codelists, get_datatypes_reference,
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
    "WallSurface":           "exterior wall faces. also links to Window/DoorSurface children",
    "GroundSurface":         "Footprint / base of building touching the ground.",
    "RoofSurface":           "Roof faces.",
    "OuterCeilingSurface":   "Underside of overhanging parts (e.g. balcony soffit).",
    "OuterFloorSurface":     "Top face of protruding parts (e.g. balcony floor from outside).",
    "CeilingSurface":        "interior ceiling faces.",
    "FloorSurface":          "interior floor faces.",
    "InteriorWallSurface":   "interior wall faces.",
    # ── Openings ────────────────────────────────────────────────────────────
    "WindowSurface":         "Windows, skylights, glass facades. Child of WallSurface.",
    "DoorSurface":           "Doors, gates, garage doors. Child of WallSurface",
    # ── Installations ───────────────────────────────────────────────────────
    "BuildingInstallation":  "balconies, chimneys, dormers, bay windows, outside staircases, antennae.",
    "BuildingPart":          "Sub-volume of a building (e.g. annex, tower).",
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
    "TrafficSpace":          "Drivable lane. Boundary: →TrafficArea(613)[rel=1]. Also contains CityFurniture/SolitaryVegetation/AuxTrafficSpace via rel=0.",
    "AuxiliaryTrafficSpace": "Sidewalk/cycle lane/shoulder. Boundary: →AuxiliaryTrafficArea(612)[rel=1].",
    "TrafficArea":           "Actual lane surface polygon. Direct geometry_data join.",
    "AuxiliaryTrafficArea":  "Sidewalk/cycle-lane surface. Direct geometry_data join.",
    "Marking":               "Road markings (lines, arrows, zebra crossings). Direct geometry_data join.",
    "ClearanceSpace":        "vertical clearance envelope above a traffic space.",
    "Hole":                  "opening/gap in a traffic space surface.",
    "HoleSurface":           "surface geometry of a hole in the road.",
    # ── Vegetation ──────────────────────────────────────────────────────────
    "SolitaryVegetationObject": "Individual trees or shrubs. Also found inside TrafficSpace via rel=0.",
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

# Alternate classnames seen in the wild for the same concept (e.g. an
# IFC-converted 3DCityDB import naming the window-opening class "Window"
# rather than the standard GML-import "WindowSurface"). Keyed by the
# alternate name, mapped to the canonical key in _CLASS_SEMANTIC_HINTS above.
# Confirmed exact-match lookup, not fuzzy — extend one line at a time as more
# aliases are identified against real datasets.
_CLASS_NAME_ALIASES: dict[str, str] = {
    "Window": "WindowSurface",
}


def _class_hint(classname: str) -> str:
    """Look up a class's semantic hint, resolving through the alias table first."""
    return _CLASS_SEMANTIC_HINTS.get(_CLASS_NAME_ALIASES.get(classname, classname), "")


# ---- Prompt cache (never expires; busted via force_refresh or invalidate) ----
# Building the prompt re-runs a battery of full-table aggregate queries over
# feature/property/geometry_data, which is very expensive for large city
# datasets. The assembled prompt is effectively static per database state, so
# we cache it for the process lifetime and only rebuild when asked to.
_prompt_cache: dict = {}
_prompt_cache_lock = threading.Lock()


def _prompt_cache_key(include_query_agent_extras: bool, compact: bool) -> tuple:
    return (
        "extras" if include_query_agent_extras else "no_extras",
        "compact" if compact else "full",
    )


def invalidate_prompt_cache() -> None:
    """Clear the assembled-prompt cache (e.g. after a data re-import)."""
    with _prompt_cache_lock:
        _prompt_cache.clear()


def assemble_prompt(
    db: DatabaseConnection,
    include_query_agent_extras: bool = True,
    compact: bool = False,
    force_refresh: bool = False,
) -> str:
    """Assembles the complete system prompt (cached for the process lifetime).

    The result is cached and never expires on its own, so repeat calls are
    instant. Pass ``force_refresh=True`` to bypass the cache and rebuild the
    prompt (e.g. after importing new data), or call ``invalidate_prompt_cache()``.

    Args:
        db: Database connection
        include_query_agent_extras: Include SQL examples and query guidelines.
        compact: Use compact rendering for local models with small context windows.
                 Skips full property trees and verbose schema (~200 lines vs 600-1000).
        force_refresh: When True, recompute the prompt even if a cached copy exists.
    """
    key = _prompt_cache_key(include_query_agent_extras, compact)
    if not force_refresh:
        cached = _prompt_cache.get(key)
        if cached is not None:
            return cached
    rendered = _assemble_prompt_uncached(db, include_query_agent_extras, compact)
    with _prompt_cache_lock:
        _prompt_cache[key] = rendered
    return rendered


def _assemble_prompt_uncached(
    db: DatabaseConnection,
    include_query_agent_extras: bool = True,
    compact: bool = False,
) -> str:
    """Builds the system prompt from scratch (no caching).

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
    datatypes = get_datatypes_reference(db)

    toplevel_ids = {oc.id for oc in catalog.object_classes if oc.is_toplevel}
    epsg_code = db_context.epsg_code
    generic_attrs = get_generic_attributes(db, epsg_code=epsg_code)

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

    # 2. Known values: generic attribute vocabulary (compact mode only — street
    #    names live in the address table description now, see section 3).
    if compact and (vocab.generic_attr_values or generic_attrs):
        sections.append(_render_vocabulary(vocab, numeric_generic_attrs=generic_attrs if compact else None,
                                           show_generic=compact))

    # 3. Schema
    if compact:
        sections.append(_COMPACT_SCHEMA)
        sections.append(_render_address_vocabulary(vocab, compact=True))
    else:
        sections.append(_render_database_schema(schema, vocab))

    # 4. DB context
    sections.append(_render_db_context(db_context, toplevel_ids, datatypes=datatypes))

    # 5. LoD
    sections.append(_render_lod_config(lod_config))

    # 6. Object classes (generic attributes + sets co-located per class in full mode)
    sections.append(_render_objectclasses(catalog, compact=compact, generic_attrs=generic_attrs))

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
    #    Compact: merged into section 2 (Known Values).
    #    Full: co-located per class inside section 6 (_render_objectclasses) — not a
    #    standalone section anymore, so the LLM sees each class's full attribute set together.

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

def _lookup_datatype_id(attr_name: str, generic_attrs: dict | None) -> int | None:
    """Best-effort datatype_id lookup by attribute name across all classes'
    standalone attrs. Used for compact-mode string bullets, which are sourced
    from the raw vocabulary scan (vocab.generic_attr_values — plain
    (value, count) pairs, no GenericAttribute object attached) rather than
    from generic_attrs directly. First match wins; a same-named attribute
    could in principle carry a different datatype_id in a different class —
    an acceptable edge case for a compact-mode display hint."""
    if not generic_attrs:
        return None
    for info in generic_attrs.values():
        for attr in info.get("attrs", []):
            if attr.name == attr_name:
                return attr.datatype_id
    return None


def _render_vocabulary(vocab, numeric_generic_attrs: dict | None = None, show_generic: bool = True) -> str:
    """Compact-mode generic attribute value vocabulary — frequency ordered.

    Street names no longer live here — see _render_address_vocabulary(),
    co-located with the `address` table description instead. This function
    is generic-attribute-vocabulary-only now (compact mode use only; full
    mode renders generic attributes per-class in the object classes section).

    numeric_generic_attrs: optional dict merged into the Generic attribute vocabulary list
    so numeric attrs (val_int / val_double) appear alongside string ones in compact mode.
    show_generic: when False, this function has nothing left to render.
    """
    _STRING_COLS = {"val_string", "val_uri"}
    lines = ["## Known Values in This Database", ""]

    if not show_generic:
        return "\n".join(lines)

    # Merge string attrs (from vocab) and numeric attrs (from full generic_attrs scan).
    # Build combined dicts keyed by attr name so they can be sorted together.
    combined: dict[str, str] = {}
    combined_dt: dict[str, int | None] = {}
    if vocab.generic_attr_values:
        for attr_name, vals in vocab.generic_attr_values.items():
            top = ", ".join(f"`{v}`" for v, _ in vals[:10])
            combined[attr_name] = top
            combined_dt[attr_name] = _lookup_datatype_id(attr_name, numeric_generic_attrs)

    def _numeric_detail(attr) -> tuple[str, str]:
        """(col, detail) for one numeric GenericAttribute, with [uom] where present."""
        uom_suffix = f"[{attr.uom}]" if attr.uom else ""
        col = f"{attr.value_column}[{attr.uom}]" if attr.uom else attr.value_column
        if attr.min_value is not None and attr.max_value is not None:
            return col, f"{col}, {attr.min_value}–{attr.max_value}{uom_suffix}"
        return col, col

    has_set_member = False
    if numeric_generic_attrs:
        for _oc_id, info in numeric_generic_attrs.items():
            for attr in info["attrs"]:
                if attr.value_column in _STRING_COLS or attr.value_column in (None, "various"):
                    continue
                _, combined[attr.name] = _numeric_detail(attr)
                combined_dt[attr.name] = attr.datatype_id
            # GenericAttributeSet members — label as `Set.attribute` so they aren't lost.
            for set_name, members in info.get("sets", {}).items():
                for attr in members:
                    if attr.value_column in _STRING_COLS or attr.value_column in (None, "various"):
                        continue
                    has_set_member = True
                    label = f"{set_name}.{attr.name}"
                    _, combined[label] = _numeric_detail(attr)
                    combined_dt[label] = attr.datatype_id

    if combined:
        lines.append("### Generic attribute vocabulary (namespace_id = 3)")
        lines.append("Query: `JOIN property p ON p.feature_id = f.id AND p.namespace_id = 3 AND p.name = '<attr>'`")
        lines.append(
            "Numeric attributes always show as a min–max range (doubles rounded to 2 decimals; "
            "a Measure attribute's unit of measure appears in brackets). `(dt:N)` after a name is "
            "its `property.datatype_id`, cross-referenced against the `datatype` table (see "
            "Database Contents)."
        )
        if has_set_member:
            lines.append(
                "Entries shown as `Set.attribute` are members of a GenericAttributeSet — join via "
                "the set's parent_id (set row: namespace_id = 3, datatype_id = 200)."
            )
        for attr_name, detail in sorted(combined.items()):
            dt = combined_dt.get(attr_name)
            dt_suffix = f" (dt:{dt})" if dt is not None else ""
            lines.append(f"- **{attr_name}**{dt_suffix}: {detail}")

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
    lines.append("| 5         | Polygon                 | single face                       |")
    lines.append("| 6         | CompositeSurface        | Surface area (CG_3DArea)          |")
    lines.append("| 7         | TriangulatedSurface     | Surface area (CG_3DArea)          |")
    lines.append("| 8         | MultiSurface            | Surface area (CG_3DArea)          |")
    lines.append("| 9         | Solid                   | Volume (CG_Volume + CG_MakeSolid) |")
    lines.append("| 10        | CompositeSolid          | Volume (CG_Volume + CG_MakeSolid) |")
    lines.append("| 11        | MultiSolid              | Volume (CG_Volume + CG_MakeSolid) |")
    lines.append("")
    lines.append("**IMPORTANT:** A single feature may have multiple geometry_data rows (e.g. one Solid for")
    lines.append("volume AND one MultiSurface for surface area, or the same kind at more than one LoD).")
    lines.append("The type code alone cannot tell these apart — go through `property` (`val_geometry_id`,")
    lines.append("filtered by `val_lod`) to pick one specific LoD's geometry, THEN filter by type code as")
    lines.append("a secondary step. `property.name` also tells you the role of the geometry you got (e.g.")
    lines.append("`lod2Solid`, `lod2MultiSurface`).")
    lines.append("")
    lines.append("**Example filter patterns:**")
    lines.append("```sql")
    lines.append("-- Volume query: join through property to target one specific LoD's geometry, THEN filter")
    lines.append("-- by type — avoids picking up a second Solid the same feature may have at another LoD")
    lines.append("JOIN property gp ON gp.feature_id = f.id AND gp.val_geometry_id IS NOT NULL AND gp.val_lod = '<LoD>'")
    lines.append("JOIN geometry_data g ON g.id = gp.val_geometry_id")
    lines.append("WHERE (g.geometry_properties->>'type')::int IN (9, 10, 11)")
    lines.append("  AND g.geometry IS NOT NULL")
    lines.append("")
    lines.append("-- Surface area query: same property-mediated join, different type codes")
    lines.append("JOIN property gp ON gp.feature_id = f.id AND gp.val_geometry_id IS NOT NULL AND gp.val_lod = '<LoD>'")
    lines.append("JOIN geometry_data g ON g.id = gp.val_geometry_id")
    lines.append("WHERE (g.geometry_properties->>'type')::int IN (6, 8)")
    lines.append("  AND g.geometry IS NOT NULL")
    lines.append("```")
    lines.append("")
    lines.append("### Geometry Types Present in This Dataset (per objectclass)")
    lines.append("")
    lines.append("| objectclass_id | classname | role | LoD | type code | geometry kind | count (features with this role) |")
    lines.append("|----------------|-----------|------|-----|-----------|---------------|----------------------------------|")

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
            lines.append(
                f"| {oc_id} | {classname} | {t.get('role', '')} | {t.get('lod', '')} | "
                f"{code} | {label} | {t['count']} |"
            )

    lines.append("")
    return "\n".join(lines)


_SCHEMA_NARRATIVE = """\
3DCityDB v5 is a **relational storage of the full CityGML 3.0 object-oriented data \
model**. Every CityGML class (Building, Road, WaterBody, …), every attribute \
(height, function, roofType, …), every association (Building→WallSurface, \
TrafficSpace→TrafficArea, …), and the mapping of each to the database table structure \
is fully documented inside the database itself — in the `objectclass` and `datatype` \
tables. You never need to hard-code class names or attribute names; you can always \
derive them from those tables.

**Object-oriented model and inheritance.** CityGML is an object-oriented standard. \
Classes form a deep inheritance hierarchy (e.g. Building → AbstractBuilding → \
AbstractConstruction → AbstractOccupiedSpace → AbstractPhysicalSpace → AbstractSpace → \
AbstractCityObject → AbstractFeatureWithLifespan → AbstractFeature → AbstractObject). \
A class possesses not only the properties that are directly defined by it, but also \
**all properties of all its transitive superclasses** (inherited attributes and \
associations). The `objectclass.superclass_id` column encodes this hierarchy. When \
you resolve properties for a class, you must walk the full superclass chain — this is \
what the `resolve_properties` tool does.

3DCityDB v5 organises its tables into five logical modules:

**Metadata module** — The `objectclass` table defines the class hierarchy \
(e.g. Building → AbstractBuilding → ... → AbstractObject). \
The `namespace` table maps namespace IDs to their URIs and shortned prefixes \
(e.g. namespace_id=1 → CityGML core, namespace_id=3 → generic attributes, \
namespace_id=10 → building module). Always use `namespace_id` together with \
`property.name` to unambiguously identify a property. \
The `datatype` table registers every primitive and complex type used in CityGML \
(e.g. Boolean, Integer, Double, String, Code, Measure, AddressProperty, \
GeometryProperty, FeatureProperty, GenericAttributeSet, …). Each `property` row \
carries a `datatype_id` that points to this table, which in turn determines which \
`val_*` column in `property` holds the actual value (e.g. datatype Integer → \
`val_int`, Code → `val_string`, Measure → `val_double` + `val_uom`, \
GeometryProperty → `val_geometry_id` referencing `geometry_data`). \
The `database_srs` table stores the coordinate reference system used for the dataset. \
It contains a single row with the SRID (EPSG code) and its corresponding URN. \
The `ade` table lists all Application Domain Extension modules present in the database, if any.

**Feature module** — the core of the schema. Every city object (building, road, \
vegetation, etc.) is a row in `feature`, identified by `objectclass_id`. \
Semantic attributes (height, function, address link, geometry link, …) are stored \
as rows in the `property` table, linked to the `feature` table via `feature_id`. \
Relationships between features (e.g. Building → WallSurface) are encoded as \
`property` rows where `val_feature_id` points to the child feature and \
`val_relation_type` indicates the relationship kind (0 = general association, \
1 = contains (a subfeature relationship, where the referenced feature is \
considered a part of the parent feature). The `address` table holds postal \
addresses, linked to features via `property.val_address_id`.

**Geometry module** — explicit 3D geometry lives in `geometry_data`, one row per \
geometry object. Per CityGML 3.0, geometry belongs to the semantic Space/Space Boundary \
concept, not to a free-floating mesh — so a feature's geometry is reached through a named, \
LoD-specific `property` row (`property.val_geometry_id` → `geometry_data.id`; `property.name` \
e.g. `lod2Solid`/`lod2MultiSurface`, `property.val_lod` its LoD), never by joining \
`geometry_data.feature_id` to `feature.id` directly. `geometry_data.feature_id` does record \
the owning feature, but a feature can have several geometry_data rows — one per LoD, or a \
Solid for volume alongside a MultiSurface for surface area at the same LoD — always navigate \
to geometry_data via the property table, which provides the role name and namespace of each geometry (e.g. lod2solid, lod3solid). The`geometry_properties` JSON column provides additional metadata about the geometry, e.g., id values of parts of the geometry as well as more specific classification of the geometry (because CityGML allows many ISO19107 geometry types like Solid, CompositeSurface or TriangulatedSurface, which are not supported as native geometry types by PostGIS). Implicit (template-based) geometry is stored in table `implicit_geometry` and referenced from `property.val_implicitgeom_id`.

**Appearance module** — textures, materials, and surface colour information. These \
tables (`appearance`, `appear_to_surface_data`, `surface_data`, `surface_data_mapping`, \
`tex_image`) are present in the schema but are not relevant for analytical queries \
and are excluded from this reference.

**Codelist module** — the `codelist` table registers named codelists \
(e.g. `bldg:RoofTypeValue`). Table `codelist_entry` holds the individual code–definition \
pairs. Code-type properties (datatype_id=14) store their value in \
`property.val_string`; join `codelist_entry` on `code` to obtain the human-readable \
definition.
"""


_TABLE_DESCRIPTIONS = {
    "feature": (
        "One row per city object (building, road, tree, …). "
        "`objectclass_id` identifies the class. "
        "`objectid` is a string identifier used to uniquely reference this feature within the "
        "database and dataset — recommended to always be populated and globally unique; this is "
        "the column used for map highlighting and result identification, so always include it "
        "when returning features to the user. "
        "`identifier` is a separate, *optional* identifier for distinguishing this feature across "
        "different systems and across versions of the same real-world object; when populated, it "
        "must be paired with `identifier_codespace`, which names the authority responsible for "
        "maintaining it — unlike `objectid`, both are commonly left NULL unless the source dataset "
        "was built with cross-system identity in mind. "
        "`envelope` is the feature's 3D bounding box, computed over the geometries of the feature "
        "itself AND all of its nested sub-features — used for spatial pre-filtering before "
        "expensive geometry operations."
    ),
    "property": (
        "One row per attribute value of a feature. Every semantic attribute — height, function, "
        "address link, geometry link, relationship to a child feature — is stored here. "
        "Use `name` + `namespace_id` to identify a property unambiguously. "
        "`val_relation_type` encodes feature-to-feature relationships "
        "(0=general association, 1=referenced feature is a part of this feature). " 
        "Nested properties (e.g. height → value) are linked via `parent_id`. "
        "Note that attributes and relationships can occur multiple times per feature "
        "(same name, but different values)."
    ),
    "objectclass": (
        "The authoritative registry of the **entire CityGML 3.0 object-oriented data model**. "
        "Every CityGML class is one row. "
        "`superclass_id` encodes the full inheritance hierarchy (e.g. Building → AbstractBuilding "
        "→ … → AbstractObject); walk this chain to collect all inherited properties. "
        "`is_toplevel=1` marks classes whose instances can exist independently as top-level features. "
        "`schema` (JSON) documents every attribute and association of that class — its name, type and the `property` column it maps to — making objectclass the single source "
        "of truth for the DB-to-data-model mapping. "
        "When a user asks about the CityGML data model, query objectclass and follow superclass_id "
        "transitively; do not rely solely on the resolved properties shown in this prompt."
    ),
    "geometry_data": (
        "Explicit 3D geometry storage. Each row holds one geometry object (solid, surface, etc.). "
        "`feature_id` records the feature that directly owns it — accurate, but NOT safe to join "
        "on alone: a feature can carry more than one geometry_data row (different LoDs, or a "
        "Solid alongside a MultiSurface at the same LoD). Reach a feature's geometry via "
        "`property.val_geometry_id` (filtered by `val_lod`) instead — for a **non-toplevel** "
        "feature (e.g. a WallSurface), that's a two-hop path: first reach it via the parent's "
        "relationship property (e.g. property.name = `boundary`), then join through *its own* "
        "`property.val_geometry_id`, not the parent's. "
        "`geometry_properties` (JSON) encodes the outermost geometry type code as a secondary "
        "filter applied *after* the property join, not a substitute for it."
    ),
    "address": (
        "Postal addresses linked to features via `property.val_address_id`. "
        "Use `ILIKE '%street%'` on the `street` column for fuzzy name matching. "
        "`multi_point` holds one or more 3D points marking where the address is located on/in "
        "the associated building (a `GM_MultiPoint`, per the CityGML `Address` type — CityGML "
        "3.0 Core §34)."
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


# Presentation-only abbreviations for verbose Postgres type names — the
# introspected schema (static_tools.py) keeps the accurate/raw names;
# this table only shortens them for display. Unrecognized types pass
# through unchanged.
_TYPE_ABBREVIATIONS = {
    "integer": "int",
    "double precision": "double",
    "geometry": "geom",
    "jsonb": "json",
    "timestamp with time zone": "timestamptz",
    "character varying": "varchar",
    "boolean": "bool",
}


def _render_address_vocabulary(vocab, compact: bool = False) -> str:
    """Street-name count/list plus multi-city disambiguation guidance for the
    `address` table — co-located with the table's own description instead of
    living in a disconnected "Known Values" section.

    Called from _render_database_schema() (full mode, right after the
    address table's column list) and from assemble_prompt() directly
    (compact mode, right after _COMPACT_SCHEMA, which has no per-table prose
    of its own to attach this to).
    """
    lines: list = []
    if compact:
        note = f"**address table:** {vocab.street_count} distinct street name(s)."
        if vocab.street_names:
            parts = ", ".join(f"{n} ({c})" for n, c in vocab.street_names)
            note += f" Most frequent: {parts}."
        lines.append(note)
    else:
        lines.append(f"This dataset contains **{vocab.street_count}** distinct street name(s).")
        if vocab.street_names:
            parts = ", ".join(f"{n} ({c})" for n, c in vocab.street_names)
            lines.append(f"Names (ordered by frequency): {parts}.")
        lines.append("Use `ILIKE '%street%'` for matching (handles umlauts and partial names).")

    if vocab.city_count > 1:
        lines.append(
            f"⚠️ **This dataset spans {vocab.city_count} different cities.** The same street "
            "name can occur in more than one city — when filtering or grouping by street name "
            "you MUST also filter/join on `city` (e.g. `AND a.city = '<city>'`) to avoid mixing "
            "buildings from different cities."
        )

    return "\n".join(lines)


def _render_database_schema(schema: DatabaseSchema, vocab=None) -> str:
    rel_data = json.loads(schema.relationships) if schema.relationships else {}
    table_details = rel_data.get("table_details", {})

    lines = ["## Database Schema (3DCityDB v5)", ""]
    lines.append(_SCHEMA_NARRATIVE)
    lines.append("### Key Tables")
    lines.append("")
    lines.append(
        "Only columns that actually contain data in this database are listed below — "
        "always-empty columns have been omitted. If you need a column that isn't listed "
        "here, query `information_schema.columns` directly for the complete list."
    )
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
            col_type = _TYPE_ABBREVIATIONS.get(col["type"], col["type"])
            lines.append(f"  - {col['column']}: {col_type}{flag_str}")
        if table_name == "address" and vocab is not None:
            lines.append(_render_address_vocabulary(vocab, compact=False))

    return "\n".join(lines)


def _render_datatypes_reference(datatypes: list) -> list:
    """Compact reference table for the datatype_ids actually used by
    `property` in this database — cross-references the `datatype_id` column
    shown on generic attributes (and elsewhere) to a human-readable name and
    description."""
    lines = ["### Datatypes", ""]
    lines.append(
        "Every `datatype_id` value actually used by `property` rows in this database, "
        "resolved against the `datatype` table:"
    )
    lines.append("")
    lines.append("| datatype_id | type | column / join | description |")
    lines.append("|-------------|------|----------------|-------------|")
    for dt in datatypes:
        lines.append(f"| {dt['id']} | {dt['identifier']} | {dt['column_or_join']} | {dt['description']} |")
    return lines


def _render_db_context(ctx, toplevel_ids: set = None, datatypes: list | None = None) -> str:
    lines = ["## Database Contents", ""]
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

    z_ref = sc.z_reference or "meters above sea level"
    if sc.is_cartesian:
        lines.append(
            f"- Coordinate axes: **x (Easting): {sc.xy_unit}**, **y (Northing): {sc.xy_unit}**, "
            f"**z (height): meter** ({z_ref}). This is a **Cartesian/projected** CRS — x, y and "
            "z all share the same linear unit, so distances/areas/volumes computed directly in "
            "this SRID are already in the matching linear units (e.g. meters/m²/m³), no "
            "conversion needed. Relevant if you ever need `ST_Transform(geom, 4326)` to get "
            "WGS84 lat/long."
        )
    else:
        lines.append(
            f"- This is a **Geographic** CRS — x (longitude)/y (latitude) are in "
            f"**{sc.xy_unit}**, not meters. `ST_Area`/`ST_Distance` on raw geometry would "
            f"return {sc.xy_unit}-based results, not meters — transform to a projected CRS "
            "first for any area/volume/distance calculation."
        )

    lines.append("")
    lines.append("### Feature Counts (toplevel classes only)")
    for oc_id, info in ctx.statistics.features_per_class.items():
        if toplevel_ids and oc_id not in toplevel_ids:
            continue
        if isinstance(info, dict):
            lines.append(f"  - {info['classname']} (objectclass_id: {oc_id}): {info['count']} features")
        else:
            lines.append(f"  - objectclass_id {oc_id}: {info} features")

    if datatypes:
        lines.append("")
        lines.extend(_render_datatypes_reference(datatypes))

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


def _first_generic_attribute_set(toplevel: list, generic_attrs: dict | None) -> tuple:
    """Find the first toplevel class (in catalog order) that has any
    GenericAttributeSets, for the one shared, concrete SQL example shown
    once in the intro rather than regenerated per class."""
    if not generic_attrs:
        return None, None
    for oc in toplevel:
        gen = generic_attrs.get(oc.id)
        if gen and gen.get("sets"):
            return oc.id, sorted(gen["sets"])[0]
    return None, None


_ASSOC_JOIN_ALIAS = {"feature": "s", "address": "a", "geometry_data": "g", "implicit_geometry": "ig"}
_ASSOC_GROUP_ORDER = ["address", "feature", "geometry_data", "implicit_geometry"]


def _render_associations(props: list) -> list:
    """Render the Associations list for one class, grouped by shared JOIN
    mechanism (join_table, join_from_column, join_to_column) so the
    mechanical JOIN line is stated once per group, not once per property.

    Without this grouping, a class with several associations of the same
    datatype (e.g. an IFC-converted Building: buildingInstallation,
    buildingRoom, buildingFurniture, buildingSubdivision,
    buildingConstructiveElement — all FeatureProperty) would repeat the
    identical JOIN line once per property, purely because the JOIN mechanics
    come from the *datatype*, not from the individual property.
    """
    groups: dict[tuple, list] = {}
    for p in props:
        groups.setdefault((p.join_table, p.join_from_column, p.join_to_column), []).append(p)

    def _sort_key(key):
        table = key[0]
        return (_ASSOC_GROUP_ORDER.index(table) if table in _ASSOC_GROUP_ORDER else len(_ASSOC_GROUP_ORDER), table)

    lines: list = []
    for key in sorted(groups.keys(), key=_sort_key):
        join_table, join_from, join_to = key
        group = groups[key]
        alias = _ASSOC_JOIN_ALIAS.get(join_table, "x")
        type_name = group[0].type.split(":")[-1] if group[0].type else join_table

        header = (
            f"**{type_name} associations** — `JOIN {join_table} {alias} ON {alias}.{join_to} = "
            f"p.{join_from}` (`p` is this feature's own `property` row: `p.feature_id = f.id`, "
            f"`p.name` = the association name below)"
        )
        if join_table == "geometry_data":
            header += (
                ", filtered by `p.val_lod` to pick one specific LoD (see Level of Detail above) — "
                "a feature can have more than one geometry_data row (different LoDs, or a Solid "
                "alongside a MultiSurface at the same LoD)"
            )
        lines.append(header + ":")

        for p in sorted(group, key=lambda p: p.name):
            target = f" → target `{p.target}`" if p.target else ""
            desc = f" — {p.description}" if p.description else ""
            lines.append(f"- **{p.name}**{target}{desc}")
        lines.append("")

    return lines


def _render_objectclasses(catalog: ObjectClassCatalog, compact: bool = False,
                          generic_attrs: dict | None = None) -> str:
    lines = ["## Available Object Classes and Properties of stored data", ""]

    toplevel = [oc for oc in catalog.object_classes if oc.is_toplevel]
    non_toplevel = [oc for oc in catalog.object_classes if not oc.is_toplevel]

    if compact:
        lines.append("| ID | Class | Toplevel | Notes |")
        lines.append("|----|-------|----------|-------|")
        for oc in sorted(catalog.object_classes, key=lambda x: x.id):
            tl = "✅" if oc.is_toplevel else ""
            hint = _class_hint(oc.classname)
            lines.append(f"| {oc.id} | {oc.classname} | {tl} | {hint} |")
        lines.append("")
        lines.append("**Boundary surfaces join (val_relation_type=1):**")
        lines.append("```sql")
        lines.append("JOIN property rel ON rel.feature_id = f.id AND rel.val_relation_type = 1")
        lines.append("JOIN feature s ON s.id = rel.val_feature_id AND s.objectclass_id = <id>")
        lines.append("```")
        return "\n".join(lines)

    # --- Full detail for toplevel classes ---
    lines.append(
        "Each class below lists its schema **Properties** (scalar attributes) plus, where "
        "present, its **Associations** (relations to other features, geometries, or addresses), "
        "its **Generic Attributes** (namespace_id = 3) and **Generic Attribute Sets** (similar "
        "to IFC PropertySets). Generic attributes are read via "
        "`JOIN property p ON p.feature_id = f.id AND p.namespace_id = 3 AND p.name = '<attr>'`."
    )
    lines.append("")
    lines.append(
        "⚠️ **Transitive inheritance:** CityGML is object-oriented. A class owns not only the "
        "properties listed directly below it, but also **all properties inherited from every "
        "superclass** in its transitive hierarchy (e.g. Building inherits from AbstractBuilding → ...→ AbstractObject). The properties shown below were resolved by walking this full superclass "
        "chain and keeping only those that actually exist in this database. When a user asks "
        "about the CityGML data model or what attributes a class *can* have, remember that the "
        "complete set is defined transitively — consult the `objectclass.schema` JSON and follow "
        "`superclass_id` for the authoritative answer."
    )
    lines.append("")
    lines.append(
        "**Space vs. Space Boundary** (CityGML 3.0 Core §10, §54): a **Space** is an entity with "
        "volumetric extent — e.g. Building, Room, TrafficSpace, WaterBody — and a **Space "
        "Boundary** is an entity with areal extent that bounds or connects spaces — e.g. "
        "WallSurface, RoofSurface, GroundSurface, FloorSurface. A Space's own geometry (its "
        "`lodXSolid`/`lodXMultiSurface` associations, below) represents the volumetric object "
        "itself; its `boundary` association relates it to the surfaces that bound it — each "
        "bounding surface is a *separate* `feature` row with its own independent geometry, "
        "reached the same way, via its own `property.val_geometry_id`."
    )
    lines.append("")
    lines.append(
        "**Generic Attributes convention:** numeric attributes (`val_int`/`val_double`) always "
        "show as a min–max range, rounded to 2 decimals for doubles, even with only two distinct "
        "values; a Measure-typed attribute (`datatype_id=17`) additionally shows its unit of "
        "measure in brackets, e.g. `val_double[m2]` / `1.00 – 110.70[m2]`. A complete string "
        "value list ends with a period (`.`) — every distinct value is shown. A partial string "
        "sample is explicitly marked `(excerpt — 3 of N distinct)`, not just a bare count — "
        "treat it as a sample, not the full set. Long string values are truncated to 30 "
        "characters + `{...}`. The `datatype_id` column cross-references the `datatype` table "
        "(see Database Contents)."
    )
    lines.append("")

    # Generic Attribute Sets: explanation + one shared, concrete SQL example —
    # computed once here, not regenerated per class (see _render_class_generic_attrs()).
    _example_oc_id, _example_set = _first_generic_attribute_set(toplevel, generic_attrs)
    if _example_set:
        lines.append(
            "**Generic Attribute Sets** (similar to IFC PropertySets — members are nested inside a "
            "GenericAttributeSet: a property row with datatype_id = 200, linked via parent_id). "
            "Join through the set to read members (and to disambiguate identically-named members "
            "across different sets), e.g.:"
        )
        lines.append("```sql")
        lines.append("SELECT f.objectid, m.name AS attribute, m.val_double, m.val_string")
        lines.append("FROM feature  f")
        lines.append("JOIN property s ON s.feature_id = f.id AND s.namespace_id = 3")
        lines.append(f"               AND s.datatype_id = 200 AND s.name = '{_example_set}'")
        lines.append("JOIN property m ON m.parent_id = s.id AND m.namespace_id = 3")
        lines.append(f"WHERE f.objectclass_id = {_example_oc_id};")
        lines.append("```")
        lines.append("")

    for oc in toplevel:
        prefix = oc.identifier if oc.identifier else oc.classname
        lines.append(f"### {prefix} (ID: {oc.id}, Namespace ID: {oc.namespace_id})")
        lines.append("")

        scalar_props = [p for p in oc.resolved_properties if p.join_table is None] if oc.resolved_properties else []
        associations = [p for p in oc.resolved_properties if p.join_table] if oc.resolved_properties else []

        if scalar_props:
            lines.append("**Properties:**")
            for prop in scalar_props:
                lines.append(_render_property(prop))
            lines.append("")

        if associations:
            lines.append("**Associations:**")
            lines.extend(_render_associations(associations))

        gen = generic_attrs.get(oc.id) if generic_attrs else None
        if gen:
            lines.extend(_render_class_generic_attrs(gen))

        if oc.classname in ("Building", "BuildingPart"):
            lines.append("**Geometry:**")
            lines.append("  Reach the Building's own geometry via property, not geometry_data.feature_id")
            lines.append("  directly — a building can have geometry at more than one LoD (or a Solid AND a")
            lines.append("  MultiSurface at the same LoD), and geometry_data.feature_id alone can't tell you")
            lines.append("  which one you're joining.")
            lines.append("    JOIN property gp ON gp.feature_id = f.id AND gp.val_geometry_id IS NOT NULL")
            lines.append("                     AND gp.val_lod = '<LoD>'")
            lines.append("    JOIN geometry_data g ON g.id = gp.val_geometry_id")
            lines.append("  gp.name tells you which named geometry property you got (e.g. lod2Solid, lod2MultiSurface).")
            lines.append("  Then filter by type as usual: (g.geometry_properties->>'type')::int")
            lines.append("    - Volume queries:       WHERE (g.geometry_properties->>'type')::int IN (9, 10, 11)  -- Solid / CompositeSolid / MultiSolid")
            lines.append("    - Surface area queries: WHERE (g.geometry_properties->>'type')::int IN (5, 6, 7, 8)  -- Polygon / CompositeSurface / TriangulatedSurface / MultiSurface")
            lines.append("  If no geometry property exists at the desired LoD, fall back to boundary surfaces")
            lines.append("  (the Space's `boundary` property → GroundSurface, RoofSurface, etc.) — each has its")
            lines.append("  own geometry the same way, via its own property.val_geometry_id.")
            lines.append("  Volume: CG_Volume(CG_MakeSolid(g.geometry)) — geometry must be closed (ST_IsClosed = true).")
            lines.append("")
            lines.append("**Boundary Surfaces (via the Space's `boundary` property):**")
            lines.append("  WallSurface(709), RoofSurface(712), GroundSurface(710), OuterCeilingSurface(716),")
            lines.append("  OuterFloorSurface(714), ClosureSurface(15)⚠️NO GEOM")
            lines.append("    JOIN property p ON p.feature_id = f.id AND p.name = 'boundary'")
            lines.append("    JOIN feature s ON s.id = p.val_feature_id AND s.objectclass_id = <ID>")
            lines.append("")
            lines.append("**BuildingInstallation & BuildingPart (direct associations, not Space Boundaries —**")
            lines.append("**grouped with boundary surfaces before only because they shared the old, imprecise**")
            lines.append("**val_relation_type=1 condition; each has its own distinct property name):**")
            lines.append("  BuildingInstallation(905): JOIN property p ON p.feature_id = f.id AND p.name = 'buildingInstallation'")
            lines.append("                             JOIN feature s ON s.id = p.val_feature_id AND s.objectclass_id = 905")
            lines.append("  BuildingPart(902):         JOIN property p ON p.feature_id = f.id AND p.name = 'buildingPart'")
            lines.append("                             JOIN feature s ON s.id = p.val_feature_id AND s.objectclass_id = 902")
            lines.append("")
            lines.append("**Window/Door Surfaces (2-hop):**")
            lines.append("  WindowSurface(719) and DoorSurface(718) are children of WallSurface's own")
            lines.append("  `fillingSurface` property — not Building's `boundary`, and not `boundary` on")
            lines.append("  WallSurface either.")
            lines.append("    JOIN property p1 ON p1.feature_id = f.id AND p1.name = 'boundary'")
            lines.append("    JOIN feature wall ON wall.id = p1.val_feature_id AND wall.objectclass_id = 709")
            lines.append("    JOIN property p2 ON p2.feature_id = wall.id AND p2.name = 'fillingSurface'")
            lines.append("    JOIN feature win ON win.id = p2.val_feature_id AND win.objectclass_id = 719")
            lines.append("")
            lines.append("**BuildingInstallation own surfaces (2-hop):**")
            lines.append("  BuildingInstallation can have its own WallSurface children via its own `boundary`")
            lines.append("  property (it's also a kind of Space).")
            lines.append("    JOIN property p1 ON p1.feature_id = f.id AND p1.name = 'buildingInstallation'")
            lines.append("    JOIN feature inst ON inst.id = p1.val_feature_id AND inst.objectclass_id = 905")
            lines.append("    JOIN property p2 ON p2.feature_id = inst.id AND p2.name = 'boundary'")
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
            hint = _class_hint(oc.classname)
            lines.append(f"| {oc.id} | {oc.classname} | {identifier} | {oc.namespace_id} | {hint} |")
        lines.append("")

        non_toplevel_with_attrs = [
            (oc, generic_attrs[oc.id]) for oc in non_toplevel
            if generic_attrs and oc.id in generic_attrs
        ]
        if non_toplevel_with_attrs:
            lines.append("#### Generic Attributes by Non-Toplevel Class")
            lines.append("")
            for oc, gen in non_toplevel_with_attrs:
                identifier = oc.identifier if oc.identifier else oc.classname
                lines.append(f"**{identifier}** (ID: {oc.id}, Namespace ID: {oc.namespace_id})")
                lines.extend(_render_class_generic_attrs(gen))

    return "\n".join(lines)


def _render_property(prop: PropertyDefinition) -> str:
    # By construction (see _render_objectclasses()'s Properties/Associations
    # split), every property reaching this function is scalar-valued
    # (join_table is None) — relationship-style properties (FeatureProperty,
    # AddressProperty, GeometryProperty, ImplicitGeometryProperty) are
    # rendered by _render_associations() instead.
    parts = [f"  - **{prop.name}** ({prop.type}) ns:{prop.namespace_id}"]

    if prop.description:
        parts.append(f"    - {prop.description}")

    if prop.value_column:
        parts.append(f"    - col: `{prop.value_column}`")

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
            # Skip codelist section if ALL entries are unresolved and there
            # are very few of them (not a real classification worth showing)
            unresolved = prop.codelist.unresolved_codes
            all_unresolved = len(unresolved) >= len(prop.codelist.entries)
            if all_unresolved and len(prop.codelist.entries) <= 3:
                # Not worth showing — just a few raw values, not a real codelist
                pass
            else:
                header = (f"CodeList ({prop.codelist.codelist_name})" if prop.codelist.codelist_name
                           else "Raw values (no codelist registered for this property)")
                parts.append(f"    - {header}:")
                if unresolved:
                    # Actionable for a read-only query agent: it cannot fix the
                    # data, so tell it what to do at query time instead — show
                    # the raw code and say so, never invent a meaning for it.
                    shown = unresolved[:10]
                    codes_str = ", ".join(f"`{c}`" for c in shown)
                    if len(unresolved) > 10:
                        codes_str += f" (+{len(unresolved) - 10} more)"
                    parts.append(
                        f"      ⚠️ No definition on file for: {codes_str}. "
                        f"Report these as raw codes to the user — do not guess their meaning."
                    )
                for entry in prop.codelist.entries:
                    parts.append(f"      - `{entry.code}` → {entry.value}")

    return "\n".join(parts)


def _truncate_val(value, limit: int = 30) -> str:
    """Cut a displayed value to `limit` characters, marking truncation with {...}."""
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "{...}"


def _generic_attr_rows(attrs: list) -> list:
    """Render the markdown table rows (no header) for a list of GenericAttributes.

    Convention (also explained once in the shared intro — see
    _render_objectclasses()): numeric attributes always show as a min–max
    range (rounded to 2 decimals for val_double, upstream in
    _enrich_numeric_generic; unit of measure in brackets when the attribute
    is a Measure); a complete string value list ends with a period; a
    partial string sample is explicitly marked as an excerpt, not just a
    bare count; long strings are truncated with {...}.
    """
    rows = []
    for attr in attrs:
        if attr.value_column == "various":
            # Grouped metadata prefix — synthetic row; datatype_id=0 is a
            # sentinel for "mixed", not a real type, so show "—" instead.
            prefix_raw = attr.name.split("*")[0].strip()
            sub = ", ".join(attr.sample_values) if attr.sample_values else ""
            detail = f"group — LIKE '{prefix_raw}%'; sub-attrs: {sub}" if sub else f"group — LIKE '{prefix_raw}%'"
            rows.append(f"| {attr.name} | various | — | {detail} |")
            continue

        if attr.code_labels:
            # This generic attribute is a known alias for an existing
            # schema-property codelist (e.g. Bavaria Open Data's
            # 'citygml_function' == bldg:Building.function) — render it like
            # a real CodeList instead of a bare value list, and say where
            # the codes actually come from.
            pairs = ", ".join(f"`{code}` → {label}" for code, label in sorted(attr.code_labels.items()))
            source_note = f" — same codes as `{attr.code_labels_source}`" if attr.code_labels_source else ""
            rows.append(f"| {attr.name} | `{attr.value_column}` | {attr.datatype_id} | {pairs}{source_note} |")
            continue

        is_numeric = attr.value_column in ("val_int", "val_double")
        col = f"`{attr.value_column}`"

        if is_numeric:
            # Numeric attributes always render as a range, even with only
            # two distinct values — never as a raw value list.
            uom_suffix = f"[{attr.uom}]" if attr.uom else ""
            if attr.min_value is not None and attr.max_value is not None:
                detail = f"{attr.min_value} – {attr.max_value}{uom_suffix}"
            else:
                detail = ""
            if attr.uom:
                col = f"`{attr.value_column}[{attr.uom}]`"
        elif attr.is_categorical and attr.distinct_values:
            # Complete list (≤ CATEGORICAL_THRESHOLD distinct values) — the
            # trailing period signals to the LLM that this is the full set.
            detail = ", ".join(f"`{_truncate_val(v)}`" for v in attr.distinct_values) + "."
        elif attr.sample_values:
            # Partial sample — explicitly marked as an excerpt, not a
            # complete list, with the true total distinct count.
            excerpt = ", ".join(f"`{_truncate_val(v)}`" for v in attr.sample_values[:3])
            if attr.distinct_value_count:
                detail = f"{excerpt} (excerpt — 3 of {attr.distinct_value_count} distinct)"
            else:
                detail = excerpt
        elif attr.min_value is not None and attr.max_value is not None:
            detail = f"{attr.min_value} – {attr.max_value}"
        else:
            detail = ""

        rows.append(f"| {attr.name} | {col} | {attr.datatype_id} | {detail} |")
    return rows


def _generic_attr_table(attrs: list, indent: str = "") -> list:
    """Header + rows for a generic-attribute table, optionally indented (for set members)."""
    lines = [
        f"{indent}| attribute | col | datatype_id | values / range |",
        f"{indent}|-----------|-----|-------------|-----------------|",
    ]
    lines += [f"{indent}{row}" for row in _generic_attr_rows(attrs)]
    return lines


def _render_class_generic_attrs(gen: dict) -> list:
    """Inline render of one toplevel class's standalone generic attributes and its
    GenericAttributeSets (IFC PropertySets).

    The explanatory "Generic Attribute Sets (from IFC PropertySets...)" header
    and the "join through the set" SQL example are NOT rendered here — they'd
    otherwise repeat, unchanged in shape, once per class that happens to have
    sets. Both are rendered once, shared, in _render_objectclasses()'s intro,
    right after "Each class below lists its schema **Properties**...".
    """
    lines: list = []
    standalone = gen.get("attrs", [])
    sets = gen.get("sets", {})

    if standalone:
        lines.append("**Generic Attributes** (namespace_id = 3):")
        lines.extend(_generic_attr_table(standalone))
        lines.append("")

    if sets:
        for set_name, members in sorted(sets.items()):
            lines.append(f"- **{set_name}**")
            lines.extend(_generic_attr_table(members, indent="  "))
        lines.append("")

    return lines


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
    lines.append(
        "Substitute <LoD> with the LoD you're targeting — see the Level of Detail section above "
        "for this dataset's available/default LoD."
    )
    lines.append(
        "Patterns 0 and 1 show how to join through `property` to target one specific LoD's "
        "geometry, then filter by type — always use both together to avoid processing the wrong "
        "or duplicate geometry rows."
    )
    lines.append("")

    pattern_labels = {
        "0_volume_query":    "Pattern 0 — Volume query (Solid geometry, type IN (9,10,11))",
        "1_direct_query":    "Pattern 1 — Surface area query (CompositeSurface/MultiSurface, type IN (6,8))",
        "2_boundary_1hop":   "Pattern 2 — 1-hop boundary relationship",
        "3_space_1hop":      "Pattern 3 — 1-hop space relationship",
        "4_chain_2hop":      "Pattern 4 — 2-hop chain (grandparent → intermediate → leaf)",
        "5_exists_filter":   "Pattern 5 — EXISTS filter (parent has at least one child of type X)",
        "6_cte_arithmetic":  "Pattern 6 — CTE arithmetic (subtract / compare two aggregations)",
        "7_intersection_surface": "Pattern 7 — Intersection/Section surface area (confirmed 2-hop)",
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