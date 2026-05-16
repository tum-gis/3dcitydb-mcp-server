"""
3DCityDB MCP Server

A Model Context Protocol server that provides intelligent access to
3DCityDB v5 semantic city models. It dynamically resolves object classes,
properties, codelists, and generic attributes from the database and
assembles optimized prompts for LLM-based agents.

Usage:
    python -m citydb_mcp.server
"""

import json
import logging
from dataclasses import asdict
from mcp.server import Server
from mcp.types import Tool, TextContent

from .db import DatabaseConnection
from .tools.static_tools import get_database_schema, get_query_guidelines
from .tools.dynamic_tools import (
    scan_objectclasses, resolve_properties, get_generic_attributes,
    get_db_context_snapshot, get_lod_config, get_examples,
)
from .tools.runtime_tools import (
    run_query, get_session_context, update_module_selection,
    get_history, submit_feedback, add_to_history,
)
from .tools.assembly import assemble_prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("citydb-mcp")

# Initialize server and database
server = Server("citydb-context-server")
db = DatabaseConnection()

# Cache for static components
_cache = {}


def _to_json(obj) -> str:
    """Serialize dataclass or dict to JSON string."""
    if hasattr(obj, "__dataclass_fields__"):
        return json.dumps(asdict(obj), indent=2, default=str)
    return json.dumps(obj, indent=2, default=str)


# ============================================================
# Tool Registration
# ============================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # Static tools
        Tool(
            name="get_database_schema",
            description=(
                "Returns the 3DCityDB v5 table structures, column details, "
                "and foreign key relationships. Called once and cached."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_query_guidelines",
            description=(
                "Returns SQL best practices, indexed columns, optimization tips, "
                "and expensive operations to avoid for 3DCityDB queries."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # Dynamic tools
        Tool(
            name="scan_objectclasses",
            description=(
                "Scans the database for existing object classes (e.g. Building, "
                "Vegetation, LandUse). Returns the full class hierarchy with "
                "superclass chain, namespace IDs, and schema definitions."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="resolve_properties",
            description=(
                "For a given objectclass_id, walks the superclass hierarchy, "
                "collects all schema-defined properties, filters against the "
                "property table to keep only existing properties, determines "
                "value columns and join info, and resolves codelists for "
                "Code-type properties. Returns fully enriched PropertyDefinitions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "objectclass_id": {
                        "type": "integer",
                        "description": "The objectclass ID to resolve properties for (e.g. 901 for Building)"
                    }
                },
                "required": ["objectclass_id"],
            },
        ),
        Tool(
            name="get_generic_attributes",
            description=(
                "Fetches generic attributes (namespace_id=3) with categorical "
                "detection. String attributes with few distinct values include "
                "all possible values. Numeric attributes include min/max range."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_db_context_snapshot",
            description=(
                "Returns database-level context: coordinate system, EPSG code, "
                "bounding box, feature counts per class, available LoDs, "
                "null value percentages, and supported spatial operations."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_lod_config",
            description=(
                "Returns available Levels of Detail in the database, "
                "with the most common LoD set as default."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_examples",
            description=(
                "Returns SQL query examples filtered to only include examples "
                "for object classes that exist in the database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "objectclass_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of available objectclass IDs to filter examples"
                    }
                },
                "required": ["objectclass_ids"],
            },
        ),

        # Query execution
        Tool(
            name="run_query",
            description=(
                "Executes a read-only SQL query against 3DCityDB. "
                "Only SELECT and WITH (CTE) statements are allowed. "
                "Results are automatically limited to 500 rows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQL SELECT query to execute"
                    }
                },
                "required": ["sql"],
            },
        ),

        # User context
        Tool(
            name="get_session_context",
            description="Returns or creates the current user session context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Optional session ID. Creates new session if not provided."
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="update_module_selection",
            description=(
                "Narrows the user's scope to specific object classes or modules. "
                "Affects which properties, examples, and codelists are relevant."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "objectclass_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Object class IDs to focus on"
                    },
                    "modules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Module names to focus on (e.g. ['building', 'vegetation'])"
                    },
                },
                "required": ["session_id", "objectclass_ids"],
            },
        ),
        Tool(
            name="get_history",
            description="Returns the conversation history for a session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"}
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="submit_feedback",
            description="Logs feedback for a query execution (rating, errors).",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "query": {"type": "string"},
                    "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                    "execution_time_ms": {"type": "integer"},
                    "result_count": {"type": "integer"},
                    "error": {"type": "string"},
                },
                "required": ["session_id", "query", "rating"],
            },
        ),

        # Assembly
        Tool(
            name="assemble_prompt",
            description=(
                "Assembles the complete system prompt by orchestrating all "
                "static and dynamic tools. Returns a structured prompt string "
                "containing database schema, object classes with resolved "
                "properties and codelists, generic attributes, spatial context, "
                "and optionally SQL examples and query guidelines. "
                "Set include_query_agent_extras=false for non-query agents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_query_agent_extras": {
                        "type": "boolean",
                        "description": "Include SQL examples and query guidelines (default: true)",
                        "default": True,
                    },
                    "compact": {
                        "type": "boolean",
                        "description": "Compact rendering for local models with small context windows (default: false)",
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
    ]


# ============================================================
# Tool Execution
# ============================================================

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = _execute_tool(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(e)})
        )]


def _execute_tool(name: str, arguments: dict) -> str:
    """Routes tool calls to implementations."""

    # --- Static tools (with caching) ---
    if name == "get_database_schema":
        if "schema" not in _cache:
            _cache["schema"] = get_database_schema(db)
        return _to_json(_cache["schema"])

    if name == "get_query_guidelines":
        if "guidelines" not in _cache:
            _cache["guidelines"] = get_query_guidelines(db)
        return _to_json(_cache["guidelines"])

    # --- Dynamic tools ---
    if name == "scan_objectclasses":
        result = scan_objectclasses(db)
        return _to_json(result)

    if name == "resolve_properties":
        oc_id = arguments["objectclass_id"]
        result = resolve_properties(db, oc_id)
        return _to_json(result)

    if name == "get_generic_attributes":
        result = get_generic_attributes(db)
        return _to_json(result)

    if name == "get_db_context_snapshot":
        result = get_db_context_snapshot(db)
        return _to_json(result)

    if name == "get_lod_config":
        result = get_lod_config(db)
        return _to_json(result)

    if name == "get_examples":
        oc_ids = arguments["objectclass_ids"]
        result = get_examples(oc_ids)
        return _to_json(result)

    # --- Query execution ---
    if name == "run_query":
        result = run_query(db, arguments["sql"])
        return json.dumps(result, indent=2, default=str)

    # --- User context ---
    if name == "get_session_context":
        result = get_session_context(arguments.get("session_id"))
        return _to_json(result)

    if name == "update_module_selection":
        result = update_module_selection(
            session_id=arguments["session_id"],
            objectclass_ids=arguments["objectclass_ids"],
            modules=arguments.get("modules", []),
            reason=arguments.get("reason", ""),
        )
        return _to_json(result)

    if name == "get_history":
        result = get_history(arguments["session_id"])
        return _to_json(result)

    if name == "submit_feedback":
        result = submit_feedback(
            session_id=arguments["session_id"],
            query=arguments["query"],
            rating=arguments["rating"],
            execution_time_ms=arguments.get("execution_time_ms", 0),
            result_count=arguments.get("result_count", 0),
            error=arguments.get("error", ""),
        )
        return _to_json(result)

    # --- Assembly ---
    if name == "assemble_prompt":
        include_extras = arguments.get("include_query_agent_extras", True)
        compact = arguments.get("compact", False)
        result = assemble_prompt(db, include_query_agent_extras=include_extras, compact=compact)
        return result

    raise ValueError(f"Unknown tool: {name}")


# ============================================================
# Server Entry Point
# ============================================================

async def _run():
    from mcp.server.stdio import stdio_server

    logger.info("Starting CityGML Context MCP Server...")
    logger.info(f"Connecting to database: {db.conn_params['host']}:{db.conn_params['port']}/{db.conn_params['dbname']}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
