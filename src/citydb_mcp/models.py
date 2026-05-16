from dataclasses import dataclass, field
from datetime import datetime


# ============================================================
# Dynamic Package: ObjectClass & Property Models
# ============================================================

@dataclass
class CodeEntry:
    code: str
    value: str


@dataclass
class CodeListDefinition:
    codelist_id: int
    codelist_name: str
    source_url: str
    mime_type: str
    property_name: str
    object_class_name: str
    entries: list[CodeEntry] = field(default_factory=list)


@dataclass
class PropertyDefinition:
    name: str
    namespace_id: int
    description: str
    type: str  # e.g. "core:Code", "core:String", "core:FeatureProperty"
    target: str | None  # for join-type properties, e.g. "bldg:BuildingInstallation"
    source_objectclass_id: int
    inherited_from: str  # classname where this property is defined
    exists_in_db: bool
    value_column: str | None  # e.g. "val_string", "val_int"
    join_table: str | None
    join_from_column: str | None
    join_to_column: str | None
    is_deprecated: bool
    codelist: CodeListDefinition | None = None  # attached for core:Code properties


@dataclass
class ObjectClassDefinition:
    id: int
    classname: str
    identifier: str
    module_name: str
    namespace_id: int
    namespace_name: str
    allowed_lods: list[int] = field(default_factory=list)
    super_class_id: int | None = None
    is_abstract: bool = False
    is_toplevel: bool = False
    geometry_types: list[str] = field(default_factory=list)
    schema_raw: str = ""
    hierarchy_depth: int = 0
    resolved_properties: list[PropertyDefinition] = field(default_factory=list)


@dataclass
class ObjectClassCatalog:
    catalog_version: str
    last_updated: datetime
    object_classes: list[ObjectClassDefinition] = field(default_factory=list)


# ============================================================
# Dynamic Package: Generic Attributes
# ============================================================

@dataclass
class GenericAttribute:
    name: str
    datatype_id: int
    value_column: str
    description: str = ""
    is_categorical: bool = False
    distinct_values: list[str] = field(default_factory=list)
    distinct_value_count: int = 0
    sample_values: list[str] = field(default_factory=list)
    min_value: str | None = None
    max_value: str | None = None
    categorical_threshold: int = 20


# ============================================================
# Dynamic Package: DB Context
# ============================================================

@dataclass
class DBStatistics:
    total_features: int
    features_per_class: dict[int, int] = field(default_factory=dict)
    null_value_percentage: dict[str, float] = field(default_factory=dict)


@dataclass
class SpatialContext:
    bounding_box: str
    coverage_area_km2: float
    spatial_index_type: str
    coordinate_system: str
    supported_spatial_ops: list[str] = field(default_factory=list)
    typical_query_extent: str = ""


@dataclass
class DBContextSnapshot:
    srs_name: str
    epsg_code: int
    timestamp: datetime = field(default_factory=datetime.now)
    max_age_minutes: int = 60
    feature_and_property_count: list[int] = field(default_factory=list)
    lod_available: list[int] = field(default_factory=list)
    available_objectclass_ids: list[int] = field(default_factory=list)
    statistics: DBStatistics = field(default_factory=lambda: DBStatistics(total_features=0))
    spatial_context: SpatialContext = field(
        default_factory=lambda: SpatialContext(
            bounding_box="", coverage_area_km2=0.0,
            spatial_index_type="", coordinate_system=""
        )
    )


# ============================================================
# Dynamic Package: LoD Config
# ============================================================

@dataclass
class LoDConfig:
    supported_lods: list[int] = field(default_factory=list)
    default_lod: int = 0
    immutable_base: bool = True
    lod_descriptions: dict[int, str] = field(default_factory=dict)


# ============================================================
# Dynamic Package: Examples
# ============================================================

@dataclass
class QueryPattern:
    pattern_type: str
    keywords: list[str] = field(default_factory=list)
    relevant_objectclasses: list[int] = field(default_factory=list)
    example_queries: list[str] = field(default_factory=list)
    complexity: str = "simple"
    estimated_tokens: int = 0


@dataclass
class ExamplesLibrary:
    example_queries: list[str] = field(default_factory=list)
    allow_extension_by_llm: bool = True
    version: str = "1.0"
    examples_by_objectclass: dict[int, list[str]] = field(default_factory=dict)
    examples_by_pattern: dict[str, list[str]] = field(default_factory=dict)


# ============================================================
# User Context
# ============================================================

@dataclass
class Message:
    timestamp: datetime
    number_of_tokens: int
    message_type: str
    was_successful: bool


@dataclass
class QueryFeedback:
    query_text: str
    execution_time_ms: int
    result_count: int
    error_message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    user_rating: int = 0


@dataclass
class UserSessionContext:
    session_id: str
    preferences: list[str] = field(default_factory=list)
    permissions: str = "readonly"
    started_at: datetime = field(default_factory=datetime.now)
    max_session_minutes: int = 120
    user_role: str = "viewer"
    language: str = "en"


@dataclass
class UserModuleSelection:
    selected_modules: list[str] = field(default_factory=list)
    selected_objectclass_ids: list[int] = field(default_factory=list)
    selected_feature_ids: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    selection_reason: str = ""


@dataclass
class HistoryContext:
    last_n_messages: list[Message] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    max_window_size: int = 10
    successful_queries: list[str] = field(default_factory=list)
    failed_queries: list[str] = field(default_factory=list)
    feedbacks: list[QueryFeedback] = field(default_factory=list)


# ============================================================
# Static Package
# ============================================================

@dataclass
class DatabaseSchema:
    tables: list[str] = field(default_factory=list)
    relationships: str = ""
    version: str = "5.0"
    schema_hash: str = ""


@dataclass
class QueryGuidelines:
    rules: list[str] = field(default_factory=list)
    category: str = ""
    severity: str = ""
    indexed_columns: list[str] = field(default_factory=list)
    materialized_views: list[str] = field(default_factory=list)
    query_optimization_tips: list[str] = field(default_factory=list)
    expensive_operations: list[str] = field(default_factory=list)
    recommended_batch_size: int = 1000


# ============================================================
# Vocabulary: street names + generic attribute values
# ============================================================

@dataclass
class VocabularyData:
    street_names: list = field(default_factory=list)          # list of (name, count) tuples
    generic_attr_values: dict = field(default_factory=dict)   # attr_name -> list of (value, count)
