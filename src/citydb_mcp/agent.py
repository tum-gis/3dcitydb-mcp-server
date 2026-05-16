"""
CityGML Query Agent - LangChain wrapper for the CityGML MCP Server.

Provides a conversational agent that can query 3DCityDB v5 using natural language.
Supports both commercial LLMs (OpenAI, Anthropic) and local models (Ollama).

Usage:
    # With OpenAI
    agent = CityGMLQueryAgent(provider="openai", model="gpt-4o")
    
    # With Anthropic
    agent = CityGMLQueryAgent(provider="anthropic", model="claude-sonnet-4-20250514")
    
    # With Ollama (local)
    agent = CityGMLQueryAgent(provider="ollama", model="llama3.1")
    
    # Start interactive session
    agent.start()
    
    # Or single query
    response = agent.query("How many buildings are in the dataset?")
"""

import os
import json
import asyncio
import subprocess
import sys
from typing import Optional

from dotenv import load_dotenv
from langchain_core.tools import Tool as LangChainTool
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.agents import AgentExecutor, create_tool_calling_agent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()


# ============================================================
# LLM Provider Factory
# ============================================================

def get_llm(provider: str, model: str, temperature: float = 0.0):
    """Creates an LLM instance based on the provider."""

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=os.getenv("OPENAI_API_KEY"),
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            temperature=temperature,
            api_key=os.getenv("ANTHROPIC_API_KEY"),
        )

    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model,
            temperature=temperature,
            base_url=os.getenv("OLLAMA_BASE_URL",
                               "http://10.162.246.130:11434"),
        )

    else:
        raise ValueError(
            f"Unsupported provider: {provider}. Use 'openai', 'anthropic', or 'ollama'.")


# ============================================================
# MCP Client Wrapper
# ============================================================

class MCPClientWrapper:
    """Wraps the MCP client to call tools on the CityGML MCP server."""

    def __init__(self, server_script_path: str, env: dict = None):
        self.server_script_path = server_script_path
        self.env = env or {}
        self.session: Optional[ClientSession] = None
        self._client_context = None
        self._session_context = None

    async def connect(self):
        """Establishes connection to the MCP server via stdio."""
        server_env = {**os.environ, **self.env}

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "citygml_mcp.server"],
            cwd=os.path.abspath(self.server_script_path),
            env=server_env,
        )

        self._client_context = stdio_client(server_params)
        read, write = await self._client_context.__aenter__()

        self._session_context = ClientSession(read, write)
        self.session = await self._session_context.__aenter__()
        await self.session.initialize()

    async def disconnect(self):
        """Closes the MCP connection."""
        if self._session_context:
            await self._session_context.__aexit__(None, None, None)
        if self._client_context:
            await self._client_context.__aexit__(None, None, None)
        self.session = None

    async def call_tool(self, tool_name: str, arguments: dict = None) -> str:
        """Calls a tool on the MCP server and returns the result as string."""
        if not self.session:
            raise RuntimeError(
                "Not connected to MCP server. Call connect() first.")

        result = await self.session.call_tool(tool_name, arguments or {})

        # Extract text content from result
        if result.content:
            return result.content[0].text
        return ""

    async def list_tools(self) -> list:
        """Lists available tools from the MCP server."""
        if not self.session:
            raise RuntimeError("Not connected to MCP server.")
        result = await self.session.list_tools()
        return result.tools


# ============================================================
# CityGML Query Agent
# ============================================================

class CityGMLQueryAgent:
    """
    LangChain-based agent for querying 3DCityDB via natural language.

    Uses the CityGML MCP server for:
    - System prompt assembly (assemble_prompt)
    - SQL query execution (run_query)

    The assembled prompt becomes the system prompt. During conversation,
    the LLM only calls run_query() — keeping history lean.
    """

    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4o",
        temperature: float = 0.0,
        mcp_server_path: str = None,
        mcp_env: dict = None,
    ):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.mcp_server_path = mcp_server_path or os.getenv(
            "MCP_SERVER_PATH",
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.mcp_env = mcp_env or {}

        self.llm = None
        self.mcp_client = None
        self.agent_executor = None
        self.system_prompt = None

    async def initialize(self):
        """
        Initializes the agent:
        1. Connect to MCP server
        2. Assemble system prompt (no LLM needed)
        3. Create LangChain agent with run_query tool
        """
        print(f"Initializing CityGML Query Agent...")
        print(f"  Provider: {self.provider}")
        print(f"  Model: {self.model}")

        # Step 1: Connect to MCP server
        print("  Connecting to MCP server...")
        self.mcp_client = MCPClientWrapper(self.mcp_server_path, self.mcp_env)
        await self.mcp_client.connect()
        print("  Connected.")

        # Step 2: Assemble system prompt (pure logic, no LLM)
        print("  Assembling system prompt from database...")
        self.system_prompt = await self.mcp_client.call_tool(
            "assemble_prompt",
            {"include_query_agent_extras": True}
        )
        print(
            f"  System prompt assembled ({len(self.system_prompt)} characters).")

        # Step 3: Create LLM and agent
        print("  Creating LLM and agent...")
        self.llm = get_llm(self.provider, self.model, self.temperature)
        self._create_agent()
        print("  Agent ready.\n")

    def _create_agent(self):
        """Creates the LangChain agent with run_query as the only tool."""

        # The only tool the LLM needs during conversation
        run_query_tool = LangChainTool(
            name="run_query",
            description=(
                "Execute a read-only SQL query against the 3DCityDB database. "
                "Input must be a valid SQL SELECT statement. "
                "Returns results as JSON with columns and rows. "
                "Always use the schema information from the system prompt to "
                "construct correct queries with proper table joins, "
                "namespace_id filters, and value columns."
            ),
            func=lambda sql: asyncio.get_event_loop().run_until_complete(
                self.mcp_client.call_tool("run_query", {"sql": sql})
            ),
            coroutine=lambda sql: self.mcp_client.call_tool(
                "run_query", {"sql": sql}
            ),
        )

        # Prompt template with system prompt + conversation history
        prompt = ChatPromptTemplate.from_messages([
            ("system", self._build_system_message()),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        # Create the agent
        agent = create_tool_calling_agent(self.llm, [run_query_tool], prompt)

        self.agent_executor = AgentExecutor(
            agent=agent,
            tools=[run_query_tool],
            verbose=True,
            max_iterations=5,
            handle_parsing_errors=True,
            return_intermediate_steps=True,
        )

    def _build_system_message(self) -> str:
        """Builds the full system message combining role instructions + assembled prompt."""
        role_instructions = """You are a CityGML Query Agent — an expert at querying 3D semantic city models stored in 3DCityDB v5.

        Your task is to help users explore and analyze urban data by translating their natural language questions into SQL queries.

        IMPORTANT RULES:
        1. Always use the database schema and property information provided below to construct correct SQL queries.
        2. Use the correct value columns (val_string, val_int, val_double, etc.) based on the property's datatype.
        3. Always filter by objectclass_id when querying features.
        4. Always use namespace_id to disambiguate properties (namespace_id != 3 for schema properties, namespace_id = 3 for generic attributes).
        5. For Code-type properties, use the codelist mappings provided to translate between codes and human-readable values.
        6. For properties that require JOINs (addresses, geometry, related features), use the join information provided.
        7. Explain your query results in a clear, non-technical way.
        8. If a query fails, analyze the error and try a corrected version.
        9. If you're unsure about the exact query, start with an exploratory query and refine based on results.

        Below is the complete database context assembled from the 3DCityDB instance:

        """
        full_prompt = role_instructions + self.system_prompt
        # Escape ALL curly braces so LangChain doesn't treat them as template variables
        return full_prompt.replace("{", "{{").replace("}", "}}")

    async def query(self, user_input: str, chat_history: list = None) -> dict:
        """
        Process a single user query.

        Args:
            user_input: Natural language question
            chat_history: Optional conversation history

        Returns:
            dict with 'output' (answer) and 'intermediate_steps' (SQL queries executed)
        """
        if not self.agent_executor:
            raise RuntimeError(
                "Agent not initialized. Call initialize() first.")

        result = await self.agent_executor.ainvoke({
            "input": user_input,
            "chat_history": chat_history or [],
        })

        return result

    async def start(self):
        """Starts an interactive chat session."""
        if not self.agent_executor:
            await self.initialize()

        print("=" * 60)
        print("CityGML Query Agent")
        print(f"Model: {self.provider}/{self.model}")
        print("Type 'quit' or 'exit' to end the session.")
        print("Type 'prompt' to see the assembled system prompt.")
        print("=" * 60)
        print()

        chat_history = []

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            if user_input.lower() == "prompt":
                print("\n" + "=" * 60)
                print("ASSEMBLED SYSTEM PROMPT:")
                print("=" * 60)
                print(self.system_prompt)
                print("=" * 60 + "\n")
                continue

            if user_input.lower() == "tokens":
                # Rough token estimate (1 token ≈ 4 chars)
                est_tokens = len(self.system_prompt) // 4
                print(f"\nEstimated system prompt tokens: ~{est_tokens}\n")
                continue

            try:
                result = await self.query(user_input, chat_history)
                answer = result["output"]
                print(f"\nAgent: {answer}\n")

                # Update chat history (lean — only question + answer, not tool calls)
                chat_history.append(HumanMessage(content=user_input))
                chat_history.append(SystemMessage(content=answer))

            except Exception as e:
                print(f"\nError: {e}\n")

    async def shutdown(self):
        """Cleans up resources."""
        if self.mcp_client:
            await self.mcp_client.disconnect()
            print("Disconnected from MCP server.")


# ============================================================
# CLI Entry Point
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="CityGML Query Agent")
    parser.add_argument(
        "--provider", default="openai",
        choices=["openai", "anthropic", "ollama"],
        help="LLM provider"
    )
    parser.add_argument(
        "--model", default=None,
        help="Model name (default depends on provider)"
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="LLM temperature"
    )
    parser.add_argument(
        "--mcp-server-path", default=None,
        help="Path to the MCP server src directory"
    )

    args = parser.parse_args()

    # Default models per provider
    default_models = {
        "openai": "gpt-4o",
        "anthropic": "claude-sonnet-4-20250514",
        "ollama": "llama3.1",
    }
    model = args.model or default_models.get(args.provider, "gpt-4o")

    agent = CityGMLQueryAgent(
        provider=args.provider,
        model=model,
        temperature=args.temperature,
        mcp_server_path=args.mcp_server_path,
    )

    async def run():
        try:
            await agent.start()
        finally:
            await agent.shutdown()

    asyncio.run(run())


if __name__ == "__main__":
    main()
