"""Entry point for running the MCP server as a module."""
import asyncio
from .server import main

asyncio.run(main())
