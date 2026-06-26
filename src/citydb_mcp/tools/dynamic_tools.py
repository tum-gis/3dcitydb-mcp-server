"""Dynamic tools - called at session initialization, refreshable."""

import json
import logging
import os
from datetime import datetime
from ..db import DatabaseConnection

logger = logging.getLogger("citydb-mcp")
from ..models import (
    ObjectClassDefinition, ObjectClassCatalog, PropertyDefinition,
    CodeListDefinition, CodeEntry, GenericAttribute,
    DBContextSnapshot, DBStatistics, SpatialContext, LoDConfig,
    ExamplesLibrary
)

CATEGORICAL_THRESHOLD = int(os.getenv("CATEGORICAL_THRESHOLD", 20))
SAMPLE_VALUES_COUNT = int(os.getenv("SAMPLE_VALUES_COUNT", 5))


# ============================================================
# Datatype value column mapping (from 3DCityDB datatype table)
# ============================================================

DATATYPE_VALUE_COLUMNS = {
    1: None,              # Undefined
    2: "val_int",         # Boolean
    3: "val_int",         # Integer
    4: "val_double",      # Double
    5: "val_string",      # String
    6: "val_uri",         # URI
    7: "val_timestamp",   # Timestamp
    14: "val_string",     # Code (with val_codespace)
    17: "val_double",     # Measure (with val_uom)
    18: "val_array",      # MeasureOrNilReasonList
    22: "val_string",     # StringOrRef
    23: "val_timestamp",  # TimePosition
    24: "val_string",     # Duration
}

DATATYPE_JOIN_INFO = {
    8: {"table": "address", "from": "val_address_id", "to": "id"},           # AddressProperty
    9: {"table": "appearance", "from": "val_appearance_id", "to": "id"},      # AppearanceProperty
    10: {"table": "feature", "from": "val_feature_id", "to": "id"},           # FeatureProperty
    11: {"table": "geometry_data", "from": "val_geometry_id", "to": "id"},    # GeometryProperty
    12: None,                                                                  # Reference (just val_uri)
    16: {"table": "implicit_geometry", "from": "val_implicitgeom_id", "to": "id"},  # ImplicitGeometryProperty
}

DATATYPE_NAMES = {
    1: "core:Undefined", 2: "core:Boolean", 3: "core:Integer",
    4: "core:Double", 5: "core:String", 6: "core:URI",
    7: "core:Timestamp", 8: "core:AddressProperty",
    9: "core:AppearanceProperty", 10: "core:FeatureProperty",
    11: "core:GeometryProperty", 12: "core:Reference",
    14: "core:Code", 16: "core:ImplicitGeometryProperty",
    17: "core:Measure", 18: "core:MeasureOrNilReasonList",
    22: "core:StringOrRef", 23: "core:TimePosition", 24: "core:Duration",
    200: "generics:GenericAttributeSet",
}


# ============================================================
# Country-aware static CityGML codelist mappings
# Selected based on database SRS (EPSG code)
# ============================================================

def _get_country_from_epsg(epsg_code: int) -> str:
    """Maps EPSG code to country for codelist selection."""
    # Germany: EPSG 25831-25833, 5650, 4258, 31466-31469
    if epsg_code in range(25831, 25834) or epsg_code in range(31466, 31470) or epsg_code == 5650:
        return "DE"
    # Japan: EPSG 6668-6692, 2443-2461
    if epsg_code in range(6668, 6693) or epsg_code in range(2443, 2462):
        return "JP"
    # Netherlands: EPSG 28992, 7415
    if epsg_code in (28992, 7415):
        return "NL"
    # South Korea: EPSG 5174, 5179, 5186
    if epsg_code in (5174, 5179, 5186):
        return "KR"
    # Singapore: EPSG 3414
    if epsg_code == 3414:
        return "SG"
    # Austria: EPSG 31254-31259
    if epsg_code in range(31254, 31260):
        return "AT"
    # Switzerland: EPSG 2056
    if epsg_code == 2056:
        return "CH"
    return "UNKNOWN"


# Standardized CityGML roofType codes (SIG3D standard, used for non-DE countries)
_ROOFTYPE_STANDARD = {
    "1000": "flat roof",
    "1010": "monopitch roof",
    "1020": "dual pent roof",
    "1030": "gabled roof",
    "1040": "hipped roof",
    "1050": "half-hipped roof",
    "1060": "mansard roof",
    "1070": "pavilion roof",
    "1080": "cone roof",
    "1090": "copula roof",
    "1100": "sawtooth roof",
    "1110": "arch roof",
    "1120": "pyramidal broach roof",
    "1130": "combination of roof forms",
}

# Country-specific codelist extensions
COUNTRY_CODELISTS = {
    "DE": {
        "roofType": {
            "1000": "Flachdach",
            "2100": "Pultdach",
            "2200": "Versetztes Pultdach",
            "3100": "Satteldach",
            "3200": "Walmdach",
            "3300": "Krüppelwalmdach",
            "3400": "Mansardendach",
            "3500": "Zeltdach",
            "3600": "Kegeldach",
            "3700": "Kuppeldach",
            "3800": "Sheddach",
            "3900": "Bogendach",
            "4000": "Turmdach",
            "5000": "Mischform",
            "9999": "Sonstiges",
        },
        "function": {
            # ALKIS building function codes (AdV)
            "1000": "residential",
            "1010": "tenement",
            "1020": "hostel",
            "1100": "residential (with commercial use)",
            "1120": "residential/office",
            "1130": "residential/business",
            "1379": "residential",
            "2000": "commercial/industrial",
            "2100": "industrial",
            "2200": "commercial",
            "2400": "transport",
            "2500": "utility",
            "2700": "agriculture/forestry",
            "3000": "public use",
            "3010": "administration",
            "3020": "education/research",
            "3040": "healthcare",
            "3060": "security/order",
            "3065": "school/daycare",
            "3070": "religious",
            "3074": "garage/infrastructure",
            "3080": "cultural",
            "3087": "residential/industrial",
            "3090": "church",
            "3100": "recreation",
            "3211": "sport club",
        },
        "usage": {
            "1000": "residential",
            "1010": "tenement",
            "1020": "hostel",
            "2000": "commercial/industrial",
            "3000": "public use",
        },
    },
    "DEFAULT": {
        "roofType": _ROOFTYPE_STANDARD,
    },
}


def get_static_codelists(epsg_code: int) -> dict:
    """Returns the appropriate static codelists based on EPSG code."""
    country = _get_country_from_epsg(epsg_code)
    return COUNTRY_CODELISTS.get(country, COUNTRY_CODELISTS["DEFAULT"])


# ============================================================
# scan_objectclasses
# ============================================================

def scan_objectclasses(db: DatabaseConnection) -> ObjectClassCatalog:
    """
    Scans the feature table for existing objectclass_ids, then resolves
    the full class hierarchy from the objectclass table.
    Maps to UML: ObjectClassCatalog + ObjectClassDefinition
    """
    # Step 1: Find all objectclass_ids that have features
    existing_ids = db.execute("""
        SELECT DISTINCT objectclass_id FROM feature
    """)
    objectclass_ids = [row["objectclass_id"] for row in existing_ids]

    if not objectclass_ids:
        return ObjectClassCatalog(
            catalog_version="1.0",
            last_updated=datetime.now(),
            object_classes=[]
        )

    # Step 2: Fetch all objectclass rows (we need the full table for hierarchy walking)
    all_classes = db.execute("""
        SELECT 
            oc.id, oc.classname, oc.is_abstract, oc.is_toplevel,
            oc.superclass_id, oc.namespace_id, oc.schema,
            n.namespace AS namespace_name
        FROM objectclass oc
        LEFT JOIN namespace n ON oc.namespace_id = n.id
    """)

    # Build lookup map
    class_map = {c["id"]: c for c in all_classes}

    # Step 3: For each existing objectclass, build the definition with hierarchy
    object_classes = []
    for oc_id in objectclass_ids:
        if oc_id not in class_map:
            continue
        # Include ALL objectclasses that have features in the DB,
        # regardless of is_toplevel — the DB is the source of truth.

        oc = class_map[oc_id]
        schema_data = {}
        if oc["schema"]:
            try:
                schema_data = json.loads(oc["schema"]) if isinstance(oc["schema"], str) else oc["schema"]
            except (json.JSONDecodeError, TypeError):
                schema_data = {}

        # Calculate hierarchy depth
        depth = 0
        current_id = oc.get("superclass_id")
        while current_id and current_id in class_map:
            depth += 1
            current_id = class_map[current_id].get("superclass_id")

        obj_def = ObjectClassDefinition(
            id=oc_id,
            classname=oc["classname"],
            identifier=schema_data.get("identifier", ""),
            module_name=_extract_module_name(schema_data.get("identifier", "")),
            namespace_id=oc["namespace_id"],
            namespace_name=oc.get("namespace_name", ""),
            super_class_id=oc.get("superclass_id"),
            is_abstract=bool(oc["is_abstract"]),
            is_toplevel=bool(oc["is_toplevel"]),
            schema_raw=json.dumps(schema_data) if schema_data else "",
            hierarchy_depth=depth,
        )

        object_classes.append(obj_def)

    return ObjectClassCatalog(
        catalog_version="1.0",
        last_updated=datetime.now(),
        object_classes=object_classes
    )


def _extract_module_name(identifier: str) -> str:
    """Extracts module name from identifier like 'bldg:Building' -> 'bldg'."""
    if ":" in identifier:
        return identifier.split(":")[0]
    return ""


# ============================================================
# resolve_properties (with codelists)
# ============================================================

def resolve_properties(db: DatabaseConnection, objectclass_id: int, epsg_code: int = 0) -> list[PropertyDefinition]:
    """
    Full property resolution for a given objectclass:
    1. Walk superclass hierarchy, collect all schema properties
    2. Collect namespace_ids from hierarchy
    3. Filter against property table (only surviving properties)
    4. Determine value columns and join info from datatype
    5. For Code-type properties, fetch codelist entries from DB
    Maps to UML: PropertyDefinition + CodeListDefinition + CodeEntry
    """
    # Fetch all objectclass rows for hierarchy walking
    all_classes = db.execute("""
        SELECT 
            oc.id, oc.classname, oc.superclass_id, 
            oc.namespace_id, oc.schema,
            n.namespace AS namespace_name
        FROM objectclass oc
        LEFT JOIN namespace n ON oc.namespace_id = n.id
    """)
    class_map = {c["id"]: c for c in all_classes}

    if objectclass_id not in class_map:
        return []

    # Step 1 & 2: Walk hierarchy, collect properties and namespace_ids
    schema_properties = []  # (name, prop_data, source_class_id, inherited_from, namespace_id)
    namespace_ids = []
    current_id = objectclass_id

    while current_id and current_id in class_map:
        oc = class_map[current_id]
        ns_id = oc["namespace_id"]
        namespace_ids.append(ns_id)

        # Parse schema JSON
        schema_data = {}
        if oc["schema"]:
            try:
                schema_data = json.loads(oc["schema"]) if isinstance(oc["schema"], str) else oc["schema"]
            except (json.JSONDecodeError, TypeError):
                schema_data = {}

        # Collect properties from schema
        for prop in schema_data.get("properties", []):
            schema_properties.append({
                "name": prop.get("name", ""),
                "description": prop.get("description", ""),
                "type": prop.get("type", ""),
                "target": prop.get("target"),
                "namespace": prop.get("namespace", ""),
                "source_objectclass_id": current_id,
                "inherited_from": oc["classname"],
                "namespace_id": ns_id,
                "is_deprecated": "deprecated" in prop.get("namespace", "").lower(),
            })

        current_id = oc.get("superclass_id")

    if not schema_properties or not namespace_ids:
        return []

    # Step 3: Filter against property table — only surviving properties
    # Exclude namespace_id = 3 (generic attributes)
    non_generic_ns = [ns for ns in namespace_ids if ns != 3]

    if not non_generic_ns:
        return []

    placeholders = ",".join(["%s"] * len(non_generic_ns))
    surviving = db.execute(f"""
        SELECT DISTINCT p.name, p.namespace_id, p.datatype_id
        FROM property p
        JOIN feature f ON p.feature_id = f.id
        WHERE f.objectclass_id = %s
        AND p.namespace_id IN ({placeholders})
    """, (objectclass_id, *non_generic_ns))

    # Build lookup: (name, namespace_id) -> (datatype_id, actual_namespace_id)
    surviving_lookup = {}
    for s in surviving:
        key = (s["name"], s["namespace_id"])
        surviving_lookup[key] = (s["datatype_id"], s["namespace_id"])

    # Step 4: Match schema properties to surviving properties
    resolved = []
    for sp in schema_properties:
        key = (sp["name"], sp["namespace_id"])
        exists = key in surviving_lookup

        if not exists:
            continue  # Skip properties not in DB

        datatype_id, actual_namespace_id = surviving_lookup[key]
        value_column = DATATYPE_VALUE_COLUMNS.get(datatype_id)
        join_info = DATATYPE_JOIN_INFO.get(datatype_id)
        type_name = DATATYPE_NAMES.get(datatype_id, sp["type"])

        # Skip internal/metadata properties not useful for user queries
        SKIP_PROPERTIES = {"appearance", "externalReference", "boundary", "relatedTo"}
        if sp["name"] in SKIP_PROPERTIES:
            continue

        prop_def = PropertyDefinition(
            name=sp["name"],
            namespace_id=actual_namespace_id,
            description=sp["description"],
            type=type_name,
            target=sp.get("target"),
            source_objectclass_id=sp["source_objectclass_id"],
            inherited_from=sp["inherited_from"],
            exists_in_db=True,
            value_column=value_column,
            join_table=join_info["table"] if join_info else None,
            join_from_column=join_info["from"] if join_info else None,
            join_to_column=join_info["to"] if join_info else None,
            is_deprecated=sp["is_deprecated"],
            codelist=None,
        )

        # Step 5: For Code-type properties (datatype_id = 14), resolve codelist
        if datatype_id == 14:
            prop_def.codelist = _resolve_codelist_for_property(
                db, objectclass_id, sp["name"], sp["namespace_id"], epsg_code
            )

        resolved.append(prop_def)

    return resolved


def _resolve_codelist_for_property(
    db: DatabaseConnection,
    objectclass_id: int,
    property_name: str,
    namespace_id: int,
    epsg_code: int = 0
) -> CodeListDefinition | None:
    """
    For a Code-type property, resolves code meanings.
    Strategy:
    1. Check country-specific static codelists (selected by EPSG)
    2. Fall back to codelist_entry table in DB
    3. If neither found, return raw codes
    """
    # Step 1: Get distinct code values from DB
    distinct_codes = db.execute("""
        SELECT DISTINCT p.val_string AS code
        FROM property p
        JOIN feature f ON p.feature_id = f.id
        WHERE f.objectclass_id = %s
        AND p.name = %s
        AND p.namespace_id = %s
        AND p.val_string IS NOT NULL
    """, (objectclass_id, property_name, namespace_id))

    if not distinct_codes:
        return None

    code_values = [row["code"] for row in distinct_codes]

    # If too many distinct values, this isn't a true classification codelist
    if len(code_values) > CATEGORICAL_THRESHOLD:
        return CodeListDefinition(
            codelist_id=-1,
            codelist_name="",
            source_url="",
            mime_type="",
            property_name=property_name,
            object_class_name="",
            entries=[CodeEntry(
                code=f"{len(code_values)} distinct values",
                value="Too many values to list — treat as free text"
            )]
        )

    # Step 2: Check country-specific static codelists first
    static_codelists = get_static_codelists(epsg_code)
    if property_name in static_codelists:
        static_map = static_codelists[property_name]
        code_entries = [
            CodeEntry(code=c, value=static_map.get(c, c))
            for c in code_values
        ]
        return CodeListDefinition(
            codelist_id=-1,
            codelist_name=f"static:{property_name}",
            source_url="",
            mime_type="",
            property_name=property_name,
            object_class_name="",
            entries=code_entries
        )

    # Step 3: Try matching in codelist_entry table
    import re
    codelists = db.execute("""
        SELECT id, codelist_type, url, mime_type
        FROM codelist
    """)

    matched_codelist = None
    prop_lower = property_name.lower()
    for cl in codelists:
        cl_name_lower = cl["codelist_type"].lower()
        if prop_lower in cl_name_lower:
            matched_codelist = cl
            break
        parts = re.findall(r'[a-z]+', prop_lower)
        if len(parts) > 1 and all(part in cl_name_lower for part in parts):
            matched_codelist = cl
            break

    if matched_codelist:
        placeholders = ",".join(["%s"] * len(code_values))
        entries = db.execute(f"""
            SELECT code, definition
            FROM codelist_entry
            WHERE codelist_id = %s
            AND code IN ({placeholders})
            ORDER BY code
        """, (matched_codelist["id"], *code_values))

        entry_map = {str(e["code"]): e["definition"] for e in entries}
        unresolved = [c for c in code_values if str(c) not in entry_map]
        code_entries = [
            CodeEntry(code=c, value=entry_map.get(str(c), c))
            for c in code_values
        ]
        if unresolved:
            import logging
            logging.getLogger("citygml-mcp").warning(
                f"Codelist '{matched_codelist['codelist_type']}' missing definitions for codes: {unresolved}. "
                f"Please update the codelist_entry table with the appropriate country-specific code definitions."
            )
        return CodeListDefinition(
            codelist_id=matched_codelist["id"],
            codelist_name=matched_codelist["codelist_type"],
            source_url=matched_codelist.get("url", ""),
            mime_type=matched_codelist.get("mime_type", ""),
            property_name=property_name,
            object_class_name="",
            entries=code_entries
        )

    # Step 4: No codelist found — return raw codes
    return CodeListDefinition(
        codelist_id=-1,
        codelist_name="",
        source_url="",
        mime_type="",
        property_name=property_name,
        object_class_name="",
        entries=[CodeEntry(code=c, value=c) for c in code_values]
    )

# ============================================================
# get_generic_attributes
# ============================================================

def _set_scope(set_name: str | None) -> tuple[str, str, tuple]:
    """Scope a generic-attribute query to standalone attrs or to one named set.

    Returns (join_sql, where_sql, leading_params):
    - set_name is None  -> standalone attrs only (parent_id IS NULL)
    - set_name given    -> members whose parent is the GenericAttributeSet of that name
      (a property row with namespace_id = 3, datatype_id = 200). The set-name parameter is
      consumed by the JOIN, so it must lead the params tuple.
    """
    if set_name is None:
        return "", "AND p.parent_id IS NULL", ()
    join = (
        "JOIN property gset ON p.parent_id = gset.id "
        "AND gset.namespace_id = 3 AND gset.datatype_id = 200 AND gset.name = %s"
    )
    return join, "", (set_name,)


def _build_generic_attr(
    db: DatabaseConnection,
    name: str,
    datatype_id: int,
    objectclass_id: int,
    set_name: str | None = None,
) -> GenericAttribute | None:
    """Build and enrich one generic attribute, scoped either to standalone attrs or to a
    named GenericAttributeSet. Returns None when the attribute has no usable values."""
    value_column = DATATYPE_VALUE_COLUMNS.get(datatype_id, "val_string")
    if value_column is None:
        return None

    attr = GenericAttribute(
        name=name,
        datatype_id=datatype_id,
        value_column=value_column,
        categorical_threshold=CATEGORICAL_THRESHOLD,
    )
    # IDs are identifiers, not categories — disable categorical detection
    if name.lower().endswith("id"):
        attr.categorical_threshold = -1

    enriched = False
    if value_column in ("val_string", "val_uri"):
        attr = _enrich_string_generic(db, attr, objectclass_id, set_name)
        enriched = True
    elif value_column in ("val_int", "val_double"):
        attr = _enrich_numeric_generic(db, attr, objectclass_id, set_name)
        enriched = True
    elif value_column == "val_timestamp":
        attr = _enrich_timestamp_generic(db, attr, objectclass_id, set_name)
        enriched = True

    if enriched and attr.distinct_value_count == 0 and not attr.min_value and not attr.sample_values:
        return None
    return attr


def get_generic_attributes(db: DatabaseConnection, filter_objectclass_ids: set | None = None) -> dict:
    """
    Fetches generic attributes (namespace_id = 3) grouped by objectclass_id.
    Returns: dict[objectclass_id] = {
        "classname": str,
        "attrs": list[GenericAttribute],                  # standalone attrs (parent_id IS NULL)
        "sets":  dict[set_name, list[GenericAttribute]],  # members of each GenericAttributeSet
    }

    Generic attributes derived from IFC PropertySets are nested inside a GenericAttributeSet —
    a property row with datatype_id = 200 whose members link back via parent_id. Those members
    are grouped under their set name and enriched scoped to that set, so identically-named
    members in different sets stay distinct.

    filter_objectclass_ids: if provided, only return attrs for those classes (e.g. toplevel only).
    Maps to UML: GenericAttribute
    """
    if filter_objectclass_ids:
        placeholders = ",".join(["%s"] * len(filter_objectclass_ids))
        oc_filter = f"AND f.objectclass_id IN ({placeholders})"
        params = tuple(filter_objectclass_ids)
    else:
        oc_filter = ""
        params = ()

    # Standalone generic attributes: top-level (parent_id IS NULL), excluding set containers.
    standalone = db.execute(f"""
        SELECT DISTINCT f.objectclass_id, oc.classname, p.name, p.datatype_id
        FROM property p
        JOIN feature f ON p.feature_id = f.id
        JOIN objectclass oc ON f.objectclass_id = oc.id
        WHERE p.namespace_id = 3 AND p.parent_id IS NULL AND p.datatype_id <> 200
          {oc_filter}
        ORDER BY oc.classname, p.name
    """, params)

    # Set members: generic attributes nested inside a GenericAttributeSet (datatype_id = 200).
    members = db.execute(f"""
        SELECT DISTINCT f.objectclass_id, oc.classname, s.name AS set_name,
               p.name AS attr_name, p.datatype_id
        FROM property p
        JOIN property s ON p.parent_id = s.id AND s.namespace_id = 3 AND s.datatype_id = 200
        JOIN feature f ON p.feature_id = f.id
        JOIN objectclass oc ON f.objectclass_id = oc.id
        WHERE p.namespace_id = 3
          {oc_filter}
        ORDER BY oc.classname, s.name, p.name
    """, params)

    result: dict = {}

    def _slot(oc_id: int, classname: str) -> dict:
        if oc_id not in result:
            result[oc_id] = {"classname": classname, "attrs": [], "sets": {}}
        return result[oc_id]

    # --- Standalone attrs, grouped per class ---
    standalone_by_class: dict = {}
    for ga in standalone:
        oc_id = ga["objectclass_id"]
        standalone_by_class.setdefault(oc_id, {"classname": ga["classname"], "raw": []})
        standalone_by_class[oc_id]["raw"].append(
            {"name": ga["name"], "datatype_id": ga["datatype_id"]}
        )

    for oc_id, info in standalone_by_class.items():
        attrs = [
            a for ga in info["raw"]
            if (a := _build_generic_attr(db, ga["name"], ga["datatype_id"], oc_id)) is not None
        ]
        attrs = _filter_generic_attrs(attrs)
        if attrs:
            _slot(oc_id, info["classname"])["attrs"] = attrs

    # --- Set members, grouped per (class, set name) ---
    sets_by_class: dict = {}
    for m in members:
        oc_id = m["objectclass_id"]
        if m["datatype_id"] == 200:
            # A set nested inside a set — one level only; flag, don't recurse.
            logger.info(
                "Skipping nested GenericAttributeSet '%s' inside set '%s' (objectclass %s).",
                m["attr_name"], m["set_name"], oc_id,
            )
            continue
        slot = sets_by_class.setdefault(oc_id, {"classname": m["classname"], "sets": {}})
        slot["sets"].setdefault(m["set_name"], []).append(
            {"name": m["attr_name"], "datatype_id": m["datatype_id"]}
        )

    for oc_id, info in sets_by_class.items():
        rendered_sets: dict = {}
        for set_name, raw_members in info["sets"].items():
            attrs = [
                a for ga in raw_members
                if (a := _build_generic_attr(db, ga["name"], ga["datatype_id"], oc_id, set_name)) is not None
            ]
            # Skip the constant-value filter for set members: in small datasets every
            # attribute may have only one distinct value (e.g. one building), but the set
            # structure itself is still important to expose to the LLM.
            attrs = _filter_generic_attrs(attrs, skip_constant_filter=True)
            if attrs:
                rendered_sets[set_name] = attrs
        if rendered_sets:
            _slot(oc_id, info["classname"])["sets"] = rendered_sets

    return result


def _filter_generic_attrs(attrs: list, skip_constant_filter: bool = False) -> list:
    """
    Removes constant attributes (1 distinct value), deduplicates case-insensitively,
    and collapses large metadata prefix groups into summary entries.

    skip_constant_filter: when True, keep attrs that have exactly 1 distinct value.
    Use this for GenericAttributeSet members where exposing the schema matters even in
    small datasets (e.g. a single-building IFC import).
    """
    from collections import Counter

    # Remove constants (unless caller opts out — e.g. for set members)
    if not skip_constant_filter:
        attrs = [a for a in attrs if a.distinct_value_count != 1]

    # Deduplicate (case-insensitive)
    seen: dict = {}
    deduped = []
    for a in attrs:
        key = a.name.lower()
        if key not in seen:
            seen[key] = True
            deduped.append(a)
    attrs = deduped

    MAX_INDIVIDUAL_ATTRS = 25
    if len(attrs) <= MAX_INDIVIDUAL_ATTRS:
        return attrs

    # Detect and collapse repeated prefixes (prefix_ with ≥5 members)
    prefix_counts: Counter = Counter()
    for a in attrs:
        parts = a.name.split("_", 1)
        if len(parts) > 1:
            prefix_counts[parts[0] + "_"] += 1

    metadata_prefixes = {p for p, count in prefix_counts.items() if count >= 5}

    individual_attrs = []
    grouped_attrs: dict = {}
    for a in attrs:
        matched = next((mp for mp in metadata_prefixes if a.name.startswith(mp)), None)
        if matched:
            grouped_attrs.setdefault(matched, []).append(a)
        else:
            individual_attrs.append(a)

    for prefix, group in grouped_attrs.items():
        notable = sorted(group, key=lambda a: (a.distinct_value_count or 0), reverse=True)[:5]
        individual_attrs.append(GenericAttribute(
            name=f"{prefix}* ({len(group)} attributes)",
            datatype_id=0,
            value_column="various",
            categorical_threshold=0,
            distinct_value_count=len(group),
            sample_values=[a.name.replace(prefix, "") for a in notable],
        ))

    return individual_attrs


def _enrich_string_generic(db: DatabaseConnection, attr: GenericAttribute, objectclass_id: int,
                           set_name: str | None = None) -> GenericAttribute:
    """Enriches a string-type generic attribute with categorical detection, scoped to one
    objectclass and (optionally) to one GenericAttributeSet."""
    join_sql, where_sql, lead = _set_scope(set_name)
    count_result = db.execute_single(f"""
        SELECT COUNT(DISTINCT p.{attr.value_column}) AS cnt
        FROM property p
        JOIN feature f ON p.feature_id = f.id
        {join_sql}
        WHERE p.namespace_id = 3 AND p.name = %s AND p.{attr.value_column} IS NOT NULL
          AND f.objectclass_id = %s {where_sql}
    """, (*lead, attr.name, objectclass_id))

    distinct_count = count_result["cnt"] if count_result else 0
    attr.distinct_value_count = distinct_count

    if distinct_count <= attr.categorical_threshold and distinct_count > 0:
        attr.is_categorical = True
        values = db.execute(f"""
            SELECT DISTINCT p.{attr.value_column} AS val
            FROM property p
            JOIN feature f ON p.feature_id = f.id
            {join_sql}
            WHERE p.namespace_id = 3 AND p.name = %s AND p.{attr.value_column} IS NOT NULL
              AND f.objectclass_id = %s {where_sql}
            ORDER BY val
        """, (*lead, attr.name, objectclass_id))
        attr.distinct_values = [v["val"] for v in values]
    elif distinct_count > 0:
        attr.is_categorical = False
        samples = db.execute(f"""
            SELECT DISTINCT p.{attr.value_column} AS val
            FROM property p
            JOIN feature f ON p.feature_id = f.id
            {join_sql}
            WHERE p.namespace_id = 3 AND p.name = %s AND p.{attr.value_column} IS NOT NULL
              AND f.objectclass_id = %s {where_sql}
            LIMIT %s
        """, (*lead, attr.name, objectclass_id, SAMPLE_VALUES_COUNT))
        attr.sample_values = [s["val"] for s in samples]

    return attr


def _enrich_numeric_generic(db: DatabaseConnection, attr: GenericAttribute, objectclass_id: int,
                            set_name: str | None = None) -> GenericAttribute:
    """Enriches a numeric-type generic attribute with range, scoped to one objectclass and
    (optionally) to one GenericAttributeSet."""
    join_sql, where_sql, lead = _set_scope(set_name)
    stats = db.execute_single(f"""
        SELECT
            COUNT(DISTINCT p.{attr.value_column}) AS cnt,
            MIN(p.{attr.value_column})::text AS min_val,
            MAX(p.{attr.value_column})::text AS max_val
        FROM property p
        JOIN feature f ON p.feature_id = f.id
        {join_sql}
        WHERE p.namespace_id = 3 AND p.name = %s AND p.{attr.value_column} IS NOT NULL
          AND f.objectclass_id = %s {where_sql}
    """, (*lead, attr.name, objectclass_id))

    if not stats:
        return attr

    attr.distinct_value_count = stats["cnt"]
    attr.min_value = stats["min_val"]
    attr.max_value = stats["max_val"]

    if stats["cnt"] <= attr.categorical_threshold and stats["cnt"] > 0:
        attr.is_categorical = True
        values = db.execute(f"""
            SELECT DISTINCT p.{attr.value_column}::text AS val
            FROM property p
            JOIN feature f ON p.feature_id = f.id
            {join_sql}
            WHERE p.namespace_id = 3 AND p.name = %s AND p.{attr.value_column} IS NOT NULL
              AND f.objectclass_id = %s {where_sql}
            ORDER BY val
        """, (*lead, attr.name, objectclass_id))
        attr.distinct_values = [v["val"] for v in values]
    else:
        attr.is_categorical = False

    return attr


def _enrich_timestamp_generic(db: DatabaseConnection, attr: GenericAttribute, objectclass_id: int,
                              set_name: str | None = None) -> GenericAttribute:
    """Enriches a timestamp-type generic attribute with range, scoped to one objectclass and
    (optionally) to one GenericAttributeSet."""
    join_sql, where_sql, lead = _set_scope(set_name)
    stats = db.execute_single(f"""
        SELECT
            MIN(p.val_timestamp)::text AS min_val,
            MAX(p.val_timestamp)::text AS max_val
        FROM property p
        JOIN feature f ON p.feature_id = f.id
        {join_sql}
        WHERE p.namespace_id = 3 AND p.name = %s AND p.val_timestamp IS NOT NULL
          AND f.objectclass_id = %s {where_sql}
    """, (*lead, attr.name, objectclass_id))

    if stats:
        attr.min_value = stats["min_val"]
        attr.max_value = stats["max_val"]

    return attr


# ============================================================
# get_db_context_snapshot
# ============================================================

def get_db_context_snapshot(db: DatabaseConnection) -> DBContextSnapshot:
    """
    Aggregates database-level context: SRS, feature counts, LoDs,
    bounding box, null percentages.
    Maps to UML: DBContextSnapshot + DBStatistics + SpatialContext
    """
    # SRS info
    srs = db.execute_single("""
    SELECT srs_name, srid FROM database_srs LIMIT 1
        """)
    # Feature counts per class (toplevel only)
    feature_counts = db.execute("""
        SELECT f.objectclass_id, oc.classname, COUNT(*) AS cnt
        FROM feature f
        JOIN objectclass oc ON f.objectclass_id = oc.id
        GROUP BY f.objectclass_id, oc.classname
        ORDER BY f.objectclass_id
    """)
    features_per_class = {row["objectclass_id"]: {"count": row["cnt"], "classname": row["classname"]} for row in feature_counts}
    total_features = sum(v["count"] for v in features_per_class.values())

    # Available LoDs
    lods = db.execute("""
        SELECT DISTINCT val_lod FROM property
        WHERE val_lod IS NOT NULL
        ORDER BY val_lod
    """)
    lod_available = [row["val_lod"] for row in lods]

    # Bounding box
    bbox = db.execute_single("""
    SELECT ST_AsText(ST_Extent(envelope)) AS bbox
    FROM feature
    WHERE envelope IS NOT NULL
    """)

    # Null value percentages for properties
    null_pct = db.execute("""
        SELECT 
            name,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE 
                val_int IS NULL AND val_double IS NULL AND 
                val_string IS NULL AND val_timestamp IS NULL AND
                val_uri IS NULL AND val_array IS NULL
            ) AS null_count
        FROM property
        WHERE namespace_id != 3
        GROUP BY name
        HAVING COUNT(*) > 10
    """)
    null_percentages = {}
    for row in null_pct:
        if row["total"] > 0:
            null_percentages[row["name"]] = round(
                row["null_count"] / row["total"] * 100, 2
            )

    # Available objectclass IDs
    available_ids = list(features_per_class.keys())

    # Coordinate system
    epsg_code = srs["srid"] if srs else 0
    coord_system = f"EPSG:{epsg_code}" if epsg_code else ""

    # Detect whether the SRID is a 2D CRS by querying spatial_ref_sys.
    # A compound or geographic 3D CRS has COMPD_CS or GEOGCS[... with AXIS containing UP.
    # For simplicity: absence of VERT_CS / COMPD_CS in srtext → 2D.
    srid_is_2d = True
    if epsg_code:
        try:
            srs_row = db.execute_single(
                "SELECT srtext FROM spatial_ref_sys WHERE srid = %s", (epsg_code,)
            )
            if srs_row and srs_row["srtext"]:
                srtext = srs_row["srtext"].upper()
                srid_is_2d = "COMPD_CS" not in srtext and "VERT_CS" not in srtext
        except Exception:
            srid_is_2d = True  # assume 2D on error

    # Actual geometry coordinate dimension from the data
    coord_dim = 0
    try:
        dim_row = db.execute_single(
            "SELECT ST_CoordDim(geometry) AS dim FROM geometry_data "
            "WHERE geometry IS NOT NULL LIMIT 1"
        )
        if dim_row:
            coord_dim = dim_row["dim"] or 0
    except Exception:
        coord_dim = 0

    z_reference = os.getenv("CITYDB_Z_REFERENCE", "meters above sea level")

    return DBContextSnapshot(
        srs_name=srs["srs_name"] if srs else "",
        epsg_code=epsg_code,
        timestamp=datetime.now(),
        lod_available=lod_available,
        available_objectclass_ids=available_ids,
        statistics=DBStatistics(
            total_features=total_features,
            features_per_class=features_per_class,
            null_value_percentage=null_percentages,
        ),
        spatial_context=SpatialContext(
            bounding_box=bbox["bbox"] if bbox and bbox["bbox"] else "",
            coverage_area_km2=0.0,
            spatial_index_type="GiST",
            coordinate_system=coord_system,
            supported_spatial_ops=[
                "ST_Intersects", "ST_Contains", "ST_Within",
                "ST_DWithin", "ST_Distance", "ST_Area",
                "ST_Buffer", "ST_Centroid"
            ],
            coord_dim=coord_dim,
            srid_is_2d=srid_is_2d,
            z_reference=z_reference,
        ),
    )


# ============================================================
# get_lod_config
# ============================================================

def get_lod_config(db: DatabaseConnection) -> LoDConfig:
    """
    Queries available LoDs and determines default.
    Maps to UML: LoDConfig
    """
    lods = db.execute("""
        SELECT val_lod, COUNT(*) AS cnt
        FROM property
        WHERE val_lod IS NOT NULL
        GROUP BY val_lod
        ORDER BY val_lod
    """)

    if not lods:
        return LoDConfig()

    supported = [row["val_lod"] for row in lods]
    # Default = most common LoD
    default = max(lods, key=lambda x: x["cnt"])["val_lod"]

    lod_descriptions = {}
    lod_counts = {}
    for row in lods:
        lod = row["val_lod"]
        if isinstance(lod, str):
            lod_descriptions[lod] = f"Level of Detail {lod}"
        lod_counts[str(lod)] = row["cnt"]

    return LoDConfig(
        supported_lods=supported,
        default_lod=default,
        immutable_base=True,
        lod_descriptions=lod_descriptions,
        lod_counts=lod_counts,
    )


# ============================================================
# get_examples
# ============================================================
_TRANSPORT_IDS = {604, 610, 613}  # Intersection, TrafficSpace, TrafficArea
_INTERIOR_CLASSNAMES = {           # Classes that imply LoD4/interior structure
    "BuildingRoom", "Storey", "BuildingUnit", "IntBuildingInstallation",
}


def get_examples(available_objectclass_ids: list[int], classnames: set[str] | None = None) -> ExamplesLibrary:
    """
    Returns canonical query pattern templates filtered to this dataset.
    Patterns 0–6 use generic <PLACEHOLDER> IDs and are always included.
    Pattern 7 (transportation) is only included when transportation classes exist.
    Pattern 8 (containment) is only included when interior/room classes exist.
    Maps to UML: ExamplesLibrary
    """
    patterns = {

        "0_volume_query":
"""-- Volume of a feature (use geometry_properties type filter to target Solid rows only)
-- Filter (geometry_properties->>'type')::int IN (9,10,11) ensures only Solid/CompositeSolid/MultiSolid rows
-- are joined — avoids processing MultiSurface rows that also exist for the same feature.
SELECT f.objectid, CG_Volume(CG_MakeSolid(g.geometry)) AS volume_m3
FROM feature f
JOIN geometry_data g ON g.feature_id = f.id
WHERE f.objectclass_id = <ID>
  AND g.geometry IS NOT NULL
  AND ST_IsClosed(g.geometry) = true
  AND (g.geometry_properties->>'type')::int IN (9, 10, 11)
ORDER BY volume_m3 DESC LIMIT 10;""",

        "1_direct_query":
"""-- Surface area of a feature (target CompositeSurface/MultiSurface rows only)
-- Filter (geometry_properties->>'type')::int IN (6,8) avoids Solid rows that may also exist.
SELECT COUNT(*), SUM(CG_3DArea(g.geometry)) AS total_area_m2
FROM feature f
JOIN geometry_data g ON g.feature_id = f.id
WHERE f.objectclass_id = <ID>
  AND g.geometry IS NOT NULL
  AND (g.geometry_properties->>'type')::int IN (6, 8);""",

        "2_boundary_1hop":
"""-- val_relation_type=1: parent→boundary children (e.g. TrafficSpace→TrafficArea, Building→WallSurface)
SELECT parent.objectid, COUNT(child.id), SUM(CG_3DArea(g.geometry)) AS total_area_m2
FROM feature       parent
JOIN property      p     ON p.feature_id = parent.id AND p.val_relation_type = 1
JOIN feature       child ON child.id = p.val_feature_id AND child.objectclass_id = <CHILD_ID>
JOIN geometry_data g     ON g.feature_id = child.id
WHERE parent.objectclass_id = <PARENT_ID> AND g.geometry IS NOT NULL
GROUP BY parent.objectid ORDER BY total_area_m2 DESC;""",

        "3_space_1hop":
"""-- val_relation_type=0: parent space→child spaces (e.g. TrafficSpace→AuxiliaryTrafficSpace)
SELECT parent.objectid, COUNT(child.id) AS child_count
FROM feature  parent
JOIN property p     ON p.feature_id = parent.id AND p.val_relation_type = 0
JOIN feature  child ON child.id = p.val_feature_id AND child.objectclass_id = <CHILD_ID>
WHERE parent.objectclass_id = <PARENT_ID>
GROUP BY parent.objectid ORDER BY child_count DESC;""",

        "4_chain_2hop":
"""-- 2-hop: grandparent→mid[rel=<REL1>]→leaf[rel=<REL2>] (e.g. Building→WallSurface[1]→WindowSurface[1])
SELECT gp.objectid, COUNT(leaf.id), SUM(CG_3DArea(g.geometry)) AS total_area_m2
FROM feature       gp
JOIN property      p1   ON p1.feature_id = gp.id  AND p1.val_relation_type = <REL1>
JOIN feature       mid  ON mid.id  = p1.val_feature_id AND mid.objectclass_id  = <MID_ID>
JOIN property      p2   ON p2.feature_id = mid.id AND p2.val_relation_type = <REL2>
JOIN feature       leaf ON leaf.id = p2.val_feature_id AND leaf.objectclass_id = <LEAF_ID>
JOIN geometry_data g    ON g.feature_id = leaf.id
WHERE gp.objectclass_id = <GP_ID> AND g.geometry IS NOT NULL
GROUP BY gp.objectid ORDER BY total_area_m2 DESC;""",

        "5_exists_filter":
"""-- EXISTS: parents that have ≥1 child of type X (avoids row multiplication from JOIN)
SELECT parent.objectid
FROM feature parent
WHERE parent.objectclass_id = <PARENT_ID>
  AND EXISTS (
      SELECT 1 FROM property p
      JOIN feature child ON child.id = p.val_feature_id AND child.objectclass_id = <CHILD_ID>
      WHERE p.feature_id = parent.id AND p.val_relation_type = <REL_TYPE>
  );""",

        "7_intersection_surface":
"""-- Intersection/Section surface area (confirmed chain, all hops use val_relation_type=1)
-- Intersection(604)→TrafficSpace(610)[rel=1]→TrafficArea(613)[rel=1]→geometry_data
-- Same pattern applies to Section(602). Replace 604 with 602 for sections.
SELECT
    i.objectid,
    SUM(CG_3DArea(g.geometry)) AS total_surface_m2
FROM feature       i
JOIN property      p1  ON p1.feature_id = i.id  AND p1.val_relation_type = 1
JOIN feature       ts  ON ts.id  = p1.val_feature_id AND ts.objectclass_id  = 610
JOIN property      p2  ON p2.feature_id = ts.id AND p2.val_relation_type = 1
JOIN feature       ta  ON ta.id  = p2.val_feature_id AND ta.objectclass_id  = 613
JOIN geometry_data g   ON g.feature_id = ta.id
WHERE i.objectclass_id = 604 AND g.geometry IS NOT NULL
GROUP BY i.objectid ORDER BY total_surface_m2 DESC;""",

        "8_containment":
"""-- Spatial containment: which objects of type X lie inside objects of type Y?
-- Use this ONLY as a fallback when no explicit val_relation_type relationship exists
-- between the two classes (e.g. BuildingInstallation inside a Storey/BuildingRoom).
-- Especially relevant for LoD4 / CityGML 3.0 datasets with interiors (IFC conversions).
--
-- Step 1: always check for explicit relationships first:
--   SELECT 1 FROM property p
--   WHERE p.feature_id = <inner_feature_id>
--     AND p.val_relation_type IN (0, 1)
--     AND p.val_feature_id = <container_feature_id>
-- If rows exist, use the property-join approach (Pattern 2/3) — it is faster and correct.
--
-- Step 2 (spatial fallback): geom1 = inner object, geom2 = container (must be Solid/CompositeSolid).
SELECT
    inner_f.objectid  AS inner_object,
    outer_f.objectid  AS container,
    CG_3DIntersects(inner_g.geometry, outer_g.geometry)
        AND ST_IsEmpty(CG_3DDifference(inner_g.geometry, outer_g.geometry)) AS is_within
FROM feature       inner_f
JOIN geometry_data inner_g ON inner_g.feature_id = inner_f.id
JOIN feature       outer_f ON outer_f.objectclass_id = <CONTAINER_CLASS_ID>
JOIN geometry_data outer_g ON outer_g.feature_id = outer_f.id
WHERE inner_f.objectclass_id = <INNER_CLASS_ID>
  AND inner_g.geometry IS NOT NULL
  AND outer_g.geometry IS NOT NULL
  AND (outer_g.geometry_properties->>'type')::int IN (9, 10, 11)  -- container must be Solid
ORDER BY outer_f.objectid, inner_f.objectid;"""

    }

    available = set(available_objectclass_ids)

    # Pattern 7: transportation-specific (hardcoded IDs) — drop for non-transport datasets.
    if not _TRANSPORT_IDS.issubset(available):
        patterns.pop("7_intersection_surface", None)

    # Pattern 8: spatial containment — only relevant when interior/room classes exist.
    if classnames is not None and not classnames.intersection(_INTERIOR_CLASSNAMES):
        patterns.pop("8_containment", None)

    all_queries = list(patterns.values())

    return ExamplesLibrary(
        example_queries=all_queries,
        allow_extension_by_llm=True,
        version="2.0",
        examples_by_objectclass={},
        examples_by_pattern=patterns,
    )


def _get_examples_legacy(available_objectclass_ids: list[int]) -> ExamplesLibrary:
    """Legacy class-specific examples — kept for reference, not used in production."""
    _EXAMPLES = [

        # ----------------------------------------------------------------
        # Building — no non-toplevel dependencies
        # ----------------------------------------------------------------
        ([901], 901,
         """-- Tallest buildings by height (nested property via parent_id)
SELECT f.objectid, child.val_double AS height
FROM property child
JOIN property parent ON child.parent_id = parent.id
JOIN feature f ON child.feature_id = f.id
WHERE f.objectclass_id = 901
  AND parent.name = 'height' AND child.name = 'value'
ORDER BY child.val_double DESC
LIMIT 10;"""),

        ([901], 901,
         """-- Largest building by volume (3D solid geometry)
SELECT f.objectid, CG_Volume(CG_MakeSolid(g.geometry)) AS volume_m3
FROM feature f
JOIN geometry_data g ON g.feature_id = f.id
WHERE f.objectclass_id = 901
  AND g.geometry IS NOT NULL
  AND ST_IsClosed(g.geometry) = true
ORDER BY volume_m3 DESC
LIMIT 1;"""),

        # Requires boundary surfaces to exist
        ([901, 709, 710, 712], 901,
         """-- Buildings with roof, wall, and ground surface areas
SELECT
    b.objectid AS building_id,
    CG_Volume(CG_MakeSolid(bg.geometry))                                        AS volume_m3,
    SUM(CASE WHEN s.objectclass_id = 712 THEN CG_3DArea(sg.geometry) ELSE 0 END) AS roof_area_m2,
    SUM(CASE WHEN s.objectclass_id = 709 THEN CG_3DArea(sg.geometry) ELSE 0 END) AS wall_area_m2,
    SUM(CASE WHEN s.objectclass_id = 710 THEN CG_3DArea(sg.geometry) ELSE 0 END) AS ground_area_m2
FROM feature b
JOIN geometry_data bg ON bg.feature_id = b.id
JOIN property p ON p.feature_id = b.id AND p.val_relation_type = 1
JOIN feature s ON s.id = p.val_feature_id
JOIN geometry_data sg ON sg.feature_id = s.id
WHERE b.objectclass_id = 901
  AND s.objectclass_id IN (709, 710, 712)
  AND bg.geometry IS NOT NULL
  AND ST_IsClosed(bg.geometry) = true
GROUP BY b.id, b.objectid, bg.geometry
LIMIT 10;"""),

        # ----------------------------------------------------------------
        # Window (719) — only included when WindowSurface exists in the DB
        # ----------------------------------------------------------------
        ([901, 709, 719], 719,
         """-- Largest building (by volume) that has at least one window
-- Windows: 2-hop relation Building → WallSurface (709) → WindowSurface (719)
-- EXISTS avoids row multiplication across multiple walls/windows
SELECT
    b.objectid,
    CG_Volume(CG_MakeSolid(bg.geometry)) AS volume_m3
FROM feature b
JOIN geometry_data bg ON bg.feature_id = b.id
WHERE b.objectclass_id = 901
  AND bg.geometry IS NOT NULL
  AND ST_IsClosed(bg.geometry) = true
  AND EXISTS (
      SELECT 1
      FROM property  p1
      JOIN feature   wall ON wall.id = p1.val_feature_id
                          AND wall.objectclass_id = 709
      JOIN property  p2   ON p2.feature_id = wall.id
                          AND p2.val_relation_type = 1
      JOIN feature   win  ON win.id = p2.val_feature_id
                          AND win.objectclass_id = 719
      WHERE p1.feature_id = b.id
        AND p1.val_relation_type = 1
  )
ORDER BY volume_m3 DESC
LIMIT 1;"""),

        ([901, 709, 719], 719,
         """-- Total window surface area for a specific building
-- CG_3DArea used instead of ST_Area: windows are vertical/tilted so XY projection is wrong
SELECT
    b.objectid                  AS building_id,
    COUNT(win.id)               AS window_count,
    SUM(CG_3DArea(wg.geometry)) AS total_window_area_m2
FROM feature b
JOIN property      p1   ON p1.feature_id = b.id AND p1.val_relation_type = 1
JOIN feature       wall ON wall.id = p1.val_feature_id AND wall.objectclass_id = 709
JOIN property      p2   ON p2.feature_id = wall.id AND p2.val_relation_type = 1
JOIN feature       win  ON win.id = p2.val_feature_id AND win.objectclass_id = 719
JOIN geometry_data wg   ON wg.feature_id = win.id
WHERE b.objectclass_id = 901
  AND b.objectid = '<building_objectid>'
  AND wg.geometry IS NOT NULL
GROUP BY b.objectid;"""),

        # ----------------------------------------------------------------
        # Window + Door (719 + 718) — only when both exist in the DB
        # ----------------------------------------------------------------
        ([901, 709, 718, 719], 719,
         """-- Net wall area = gross wall area minus openings (windows + doors)
-- CTEs avoid double-counting wall areas when a wall has multiple openings.
-- COALESCE handles buildings that have walls but no openings.
WITH wall_area AS (
    SELECT SUM(CG_3DArea(wg.geometry)) AS total
    FROM feature b
    JOIN property      p1   ON p1.feature_id = b.id AND p1.val_relation_type = 1
    JOIN feature       wall ON wall.id = p1.val_feature_id AND wall.objectclass_id = 709
    JOIN geometry_data wg   ON wg.feature_id = wall.id
    WHERE b.objectclass_id = 901
      AND b.objectid = '<building_objectid>'
      AND wg.geometry IS NOT NULL
),
opening_area AS (
    SELECT SUM(CG_3DArea(og.geometry)) AS total
    FROM feature b
    JOIN property      p1      ON p1.feature_id = b.id AND p1.val_relation_type = 1
    JOIN feature       wall    ON wall.id = p1.val_feature_id AND wall.objectclass_id = 709
    JOIN property      p2      ON p2.feature_id = wall.id AND p2.val_relation_type = 1
    JOIN feature       opening ON opening.id = p2.val_feature_id
                               AND opening.objectclass_id IN (718, 719)
    JOIN geometry_data og      ON og.feature_id = opening.id
    WHERE b.objectclass_id = 901
      AND b.objectid = '<building_objectid>'
      AND og.geometry IS NOT NULL
)
SELECT
    ROUND(wall_area.total::numeric, 2)                                     AS gross_wall_area_m2,
    ROUND(COALESCE(opening_area.total, 0)::numeric, 2)                     AS openings_area_m2,
    ROUND((wall_area.total - COALESCE(opening_area.total, 0))::numeric, 2) AS net_wall_area_m2
FROM wall_area, opening_area;"""),

        # ----------------------------------------------------------------
        # Transportation — objectclass IDs confirmed from this DB:
        #   Road=607, Section=602, Intersection=604
        #   TrafficSpace=610, AuxiliaryTrafficSpace=608
        #   TrafficArea=613, AuxiliaryTrafficArea=612, Marking=614
        #
        # IMPORTANT: Road (607) has NO geometry of its own.
        # Geometry lives in TrafficArea (613) and AuxiliaryTrafficArea (612).
        # Relationship chain:
        #   Road(607) --[rel=1]--> Section(602)/Intersection(604)
        #   TrafficSpace(610) --[rel=1]--> TrafficArea(613)       <- geometry
        #   TrafficSpace(610) --[rel=0]--> AuxiliaryTrafficSpace(608)
        #   AuxiliaryTrafficSpace(608) --[rel=1]--> AuxiliaryTrafficArea(612) <- geometry
        #
        # Always use CG_3DArea — ST_Area returns 0 on PolyhedralSurface Z.
        # ----------------------------------------------------------------

        # TrafficArea (613) — lane surfaces, directly queryable
        ([613], 613,
         """-- Total traffic lane surface area (all TrafficAreas in dataset)
-- TrafficArea (613) carries the actual lane geometry — Road itself has none.
-- CG_3DArea required: geometries are PolyhedralSurface Z, ST_Area returns 0.
SELECT
    COUNT(*)                   AS traffic_area_count,
    SUM(CG_3DArea(g.geometry)) AS total_lane_area_m2
FROM feature f
JOIN geometry_data g ON g.feature_id = f.id
WHERE f.objectclass_id = 613
  AND g.geometry IS NOT NULL;"""),

        ([610, 613], 613,
         """-- Lane area per TrafficSpace (610 → 613 via val_relation_type = 1)
SELECT
    ts.objectid                  AS traffic_space_id,
    COUNT(ta.id)                 AS area_count,
    SUM(CG_3DArea(g.geometry))   AS lane_area_m2
FROM feature       ts
JOIN property      p  ON p.feature_id = ts.id AND p.val_relation_type = 1
JOIN feature       ta ON ta.id = p.val_feature_id AND ta.objectclass_id = 613
JOIN geometry_data g  ON g.feature_id = ta.id
WHERE ts.objectclass_id = 610
  AND g.geometry IS NOT NULL
GROUP BY ts.objectid
ORDER BY lane_area_m2 DESC;"""),

        # AuxiliaryTrafficArea (612) — sidewalks, cycle lanes, shoulders
        ([612], 612,
         """-- Total auxiliary surface area (sidewalks, cycle lanes, shoulders)
SELECT
    COUNT(*)                   AS aux_area_count,
    SUM(CG_3DArea(g.geometry)) AS total_aux_area_m2
FROM feature f
JOIN geometry_data g ON g.feature_id = f.id
WHERE f.objectclass_id = 612
  AND g.geometry IS NOT NULL;"""),

        ([608, 612], 612,
         """-- Auxiliary surface area per AuxiliaryTrafficSpace (608 → 612 via val_relation_type = 1)
SELECT
    ats.objectid                 AS aux_traffic_space_id,
    SUM(CG_3DArea(g.geometry))   AS aux_area_m2
FROM feature       ats
JOIN property      p   ON p.feature_id = ats.id AND p.val_relation_type = 1
JOIN feature       ata ON ata.id = p.val_feature_id AND ata.objectclass_id = 612
JOIN geometry_data g   ON g.feature_id = ata.id
WHERE ats.objectclass_id = 608
  AND g.geometry IS NOT NULL
GROUP BY ats.objectid
ORDER BY aux_area_m2 DESC;"""),

        # Road (607) — container only, geometry via Section/Intersection children
        ([607], 607,
         """-- Road sections and intersections (Road 607 is a container with no geometry)
-- Children linked via val_relation_type = 1: Section (602), Intersection (604)
SELECT
    r.objectid       AS road_id,
    child_oc.classname AS child_type,
    COUNT(child.id)  AS child_count
FROM feature r
JOIN property      p        ON p.feature_id = r.id AND p.val_relation_type = 1
JOIN feature       child    ON child.id = p.val_feature_id
JOIN objectclass   child_oc ON child_oc.id = child.objectclass_id
WHERE r.objectclass_id = 607
GROUP BY r.objectid, child_oc.classname
ORDER BY r.objectid;"""),

        # Marking (614)
        ([614], 614,
         """-- Road markings: count and total surface area
SELECT
    COUNT(*)                   AS marking_count,
    SUM(CG_3DArea(g.geometry)) AS total_marking_area_m2
FROM feature f
JOIN geometry_data g ON g.feature_id = f.id
WHERE f.objectclass_id = 614
  AND g.geometry IS NOT NULL;"""),

        # TrafficSpace within bounding box
        ([610], 610,
         """-- TrafficSpaces within a bounding box (replace coordinates and srid)
SELECT f.objectid
FROM feature f
WHERE f.objectclass_id = 610
  AND ST_Intersects(
      f.envelope,
      ST_MakeEnvelope(<xmin>, <ymin>, <xmax>, <ymax>, <srid>)
  );"""),

    ]

    available = set(available_objectclass_ids)
    filtered = {}
    for required_ids, primary_oc_id, query in _EXAMPLES:
        if all(oc_id in available for oc_id in required_ids):
            filtered.setdefault(primary_oc_id, []).append(query)

    all_filtered = [q for queries in filtered.values() for q in queries]

    return ExamplesLibrary(
        example_queries=all_filtered,
        allow_extension_by_llm=True,
        version="1.0",
        examples_by_objectclass=filtered,
    )


def get_geometry_types_per_class(db: DatabaseConnection) -> dict:
    """
    Queries which geometry type codes exist in geometry_data per objectclass.
    Returns a dict: objectclass_id → {classname, types: [{code, label, count}]}

    geometry_properties is a JSON column encoding the geometry hierarchy.
    The 'type' field at the top level tells you the outermost geometry kind:
      5 = Polygon           (single face — leaf geometry)
      6 = CompositeSurface  (connected surfaces — use CG_3DArea)
      7 = TriangulatedSurface (use CG_3DArea)
      8 = MultiSurface      (surface collection — use CG_3DArea)
      9 = Solid             (enclosed volume — use CG_Volume)
      10 = CompositeSolid   (multiple solids — use CG_Volume)
      11 = MultiSolid       (multiple solids — use CG_Volume)
    """
    TYPE_LABELS = {
        1: "Point",
        2: "MultiPoint",
        3: "LineString",
        4: "MultiLineString",
        5: "Polygon (leaf)",
        6: "CompositeSurface (area)",
        7: "TriangulatedSurface (area)",
        8: "MultiSurface (area)",
        9: "Solid (volume)",
        10: "CompositeSolid (volume)",
        11: "MultiSolid (volume)",
    }

    # The ->>'type' cast to int will fail if any row has a non-integer "type"
    # value in geometry_properties. Filter out malformed JSON via a regex on
    # the text representation before casting, so one bad import does not
    # crash prompt assembly.
    rows = db.execute("""
        SELECT
            f.objectclass_id,
            oc.classname,
            (g.geometry_properties->>'type')::int AS geom_type,
            COUNT(*) AS cnt
        FROM feature f
        JOIN geometry_data g ON g.feature_id = f.id
        JOIN objectclass oc ON f.objectclass_id = oc.id
        WHERE g.geometry IS NOT NULL
          AND g.geometry_properties IS NOT NULL
          AND (g.geometry_properties->>'type') ~ '^[0-9]+$'
        GROUP BY f.objectclass_id, oc.classname, geom_type
        ORDER BY f.objectclass_id, geom_type
    """)

    result = {}
    for row in rows:
        oc_id = row["objectclass_id"]
        if oc_id not in result:
            result[oc_id] = {"classname": row["classname"], "types": []}
        geom_type = row["geom_type"]
        result[oc_id]["types"].append({
            "code": geom_type,
            "label": TYPE_LABELS.get(geom_type, f"type {geom_type}"),
            "count": row["cnt"],
        })

    return result


# ============================================================
# get_vocabulary: street names + generic attribute value vocabulary
# ============================================================

import time as _time

_vocab_cache: dict = {}
_vocab_cache_ts: float = 0.0
_VOCAB_TTL = float(os.getenv("VOCAB_TTL_SECONDS", "3600"))


def get_vocabulary(db: DatabaseConnection):
    """Fetch street names and generic attribute distinct values, frequency-ordered.

    Results are TTL-cached (default 1 hour; override via VOCAB_TTL_SECONDS env var).
    """
    from ..models import VocabularyData

    global _vocab_cache, _vocab_cache_ts
    now = _time.time()
    if _vocab_cache and (now - _vocab_cache_ts) < _VOCAB_TTL:
        return _vocab_cache["data"]

    # Street names: only include when there are fewer than 20 distinct values.
    # Large datasets with thousands of streets add noise without helping the LLM.
    try:
        count_row = db.execute_single(
            "SELECT COUNT(DISTINCT street) AS n FROM address "
            "WHERE street IS NOT NULL AND street != ''"
        )
        _distinct_streets = count_row["n"] if count_row else 0
    except Exception:
        _distinct_streets = 999

    if _distinct_streets < 20:
        try:
            street_rows = db.execute("""
                SELECT street, COUNT(*) AS n
                FROM address
                WHERE street IS NOT NULL AND street != ''
                GROUP BY street
                ORDER BY n DESC
                LIMIT 30
            """)
            street_names = [(r["street"], r["n"]) for r in street_rows]
        except Exception:
            street_names = []
    else:
        street_names = []

    # Generic attribute distinct values per attribute (frequency-ordered)
    # Only include attributes with ≤ 30 distinct values (categorical).
    # Hard-cap the result set at 10 000 (name, val_string) groups so the scan
    # cannot stall prompt assembly on a multi-million-row property table.
    try:
        attr_rows = db.execute("""
            SELECT p.name, p.val_string, COUNT(*) AS n
            FROM property p
            WHERE p.namespace_id = 3
              AND p.val_string IS NOT NULL
              AND p.val_string != ''
            GROUP BY p.name, p.val_string
            ORDER BY p.name, n DESC
            LIMIT 10000
        """)
        raw: dict = {}
        for r in attr_rows:
            raw.setdefault(r["name"], []).append((r["val_string"], r["n"]))
        generic_attr_values = {name: vals[:30] for name, vals in raw.items() if len(vals) <= 30}
    except Exception:
        generic_attr_values = {}

    result = VocabularyData(
        street_names=street_names,
        generic_attr_values=generic_attr_values,
    )
    _vocab_cache = {"data": result}
    _vocab_cache_ts = now
    return result


# ============================================================
# synthesize_examples: 5 concrete substituted SQL examples
# ============================================================

def synthesize_examples(db: DatabaseConnection, catalog, codelists: dict, vocab) -> list:
    """Generate 5 fully-substituted SQL examples using real values from this database.

    Uses: dominant objectclass, most common function code, most common street name.
    Returns list of SQL strings ready to include in the assembled prompt.
    """
    toplevel = [oc for oc in catalog.object_classes if oc.is_toplevel]
    if not toplevel:
        return []

    # Dominant objectclass by feature count
    try:
        count_rows = db.execute("""
            SELECT objectclass_id, COUNT(*) AS n
            FROM feature
            GROUP BY objectclass_id
            ORDER BY n DESC
        """)
        count_map = {r["objectclass_id"]: r["n"] for r in count_rows}
    except Exception:
        count_map = {}

    toplevel.sort(key=lambda oc: count_map.get(oc.id, 0), reverse=True)
    dominant = toplevel[0]
    oc_id = dominant.id
    classname = dominant.classname
    classname_lower = classname.lower()

    # Most common function code for dominant class
    try:
        func_rows = db.execute("""
            SELECT p.val_string AS code, COUNT(*) AS n
            FROM property p
            JOIN feature f ON f.id = p.feature_id
            WHERE f.objectclass_id = %s
              AND p.name = 'function'
              AND p.val_string IS NOT NULL
            GROUP BY p.val_string
            ORDER BY n DESC
            LIMIT 1
        """, (oc_id,))
        func_code = func_rows[0]["code"] if func_rows else "1379"
    except Exception:
        func_code = "1379"

    func_label = codelists.get("function", {}).get(func_code, func_code)

    # Most common street name
    top_street = vocab.street_names[0][0] if vocab.street_names else "Röblingweg"

    # Roof surface objectclass_id
    roof_ids = [oc.id for oc in catalog.object_classes if oc.classname == "RoofSurface"]
    roof_id = roof_ids[0] if roof_ids else 712

    examples = []

    examples.append(f"""\
-- Count all {classname}s in the database
SELECT COUNT(*) AS {classname_lower}_count
FROM feature f
WHERE f.objectclass_id = {oc_id};""")

    examples.append(f"""\
-- {func_label.capitalize()} {classname}s in {top_street} (objectid for map highlighting)
SELECT f.objectid, a.street, a.house_number
FROM feature f
JOIN property p_func ON p_func.feature_id = f.id AND p_func.name = 'function'
JOIN property p_addr ON p_addr.feature_id = f.id AND p_addr.name = 'address'
JOIN address a ON a.id = p_addr.val_address_id
WHERE f.objectclass_id = {oc_id}
  AND p_func.val_string = '{func_code}'
  AND a.street ILIKE '%{top_street}%'
ORDER BY a.house_number;""")

    examples.append(f"""\
-- 5 tallest {classname}s by height
SELECT f.objectid, child.val_double AS height_m
FROM feature f
JOIN property parent ON parent.feature_id = f.id AND parent.name = 'height'
JOIN property child ON child.parent_id = parent.id AND child.name = 'value'
WHERE f.objectclass_id = {oc_id}
  AND child.val_double IS NOT NULL
ORDER BY child.val_double DESC
LIMIT 5;""")

    examples.append(f"""\
-- Total roof surface area of {classname}s in {top_street}
SELECT a.street, SUM(CG_3DArea(g.geometry)) AS total_roof_m2
FROM feature b
JOIN property p_addr ON p_addr.feature_id = b.id AND p_addr.name = 'address'
JOIN address a ON a.id = p_addr.val_address_id
JOIN property rel ON rel.feature_id = b.id AND rel.val_relation_type = 1
JOIN feature s ON s.id = rel.val_feature_id AND s.objectclass_id = {roof_id}
JOIN geometry_data g ON g.feature_id = s.id
WHERE b.objectclass_id = {oc_id}
  AND a.street ILIKE '%{top_street}%'
GROUP BY a.street;""")

    examples.append(f"""\
-- {classname} count by function type
SELECT p.val_string AS function_code, COUNT(*) AS count
FROM feature f
JOIN property p ON p.feature_id = f.id AND p.name = 'function'
WHERE f.objectclass_id = {oc_id}
  AND p.val_string IS NOT NULL
GROUP BY p.val_string
ORDER BY count DESC;""")

    return examples


def get_spatial_capabilities(db: DatabaseConnection) -> dict:
    """Checks which spatial extensions are available."""
    capabilities = {
        "postgis": True,
        "sfcgal": False,
        "postgis_functions": [
            "ST_Intersects", "ST_Contains", "ST_Within",
            "ST_DWithin", "ST_Distance", "ST_Area",
            "ST_Buffer", "ST_Centroid", "ST_Transform",
            "ST_Envelope", "ST_AsText", "ST_MakeEnvelope",
            "ST_SetSRID", "ST_MakePoint", "ST_IsClosed",
            "ST_GeometryType",
        ],
        "sfcgal_functions": [],
    }
    
    # Check if SFCGAL is available
    try:
        result = db.execute_single("SELECT postgis_sfcgal_version();")
        if result:
            capabilities["sfcgal"] = True
            capabilities["sfcgal_functions"] = [
                "CG_Volume(geometry) — volume of a solid in cubic meters",
                "CG_MakeSolid(geometry) — converts PolyhedralSurface to Solid (required before CG_Volume)",
                "CG_3DArea(geometry) — true 3D surface area (accounts for tilted surfaces)",
                "CG_3DDistance(geomA, geomB) — 3D distance between geometries",
                "CG_IsSolid(geometry) — check if geometry is a valid solid",
                "CG_Tesselate(geometry) — triangulate surfaces",
                "CG_3DIntersects(geomA, geomB) — tests if two 3D geometries intersect",
                "CG_3DIntersection(geomA, geomB) — computes the 3D intersection of two geometries",
                "CG_3DUnion(geomA, geomB) — computes the 3D union of two geometries",
                "CG_3DDifference(geomA, geomB) — computes the 3D difference of two geometries",
                "CG_Extrude(geom, x float, y float, z float) — extrudes a line to a surface or a surface to a volume",
                "CG_3DBuffer(geom, radius float8, segments integer, buffer_type integer) — generates a 3D buffer around the input geometry; buffer_type: 0=rounded (default), 1=flat, 2=square; minimum 4 segments",
                "CG_3DTranslate(geom, deltaX, deltaY, deltaZ) — translates (moves) a geometry by given offsets in 3D space",
            ]
    except Exception:
        pass
    
    return capabilities