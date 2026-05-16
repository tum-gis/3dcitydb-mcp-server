"""Smoke test for assemble_prompt -- the main orchestration tool.

Usage (from mcp-server/ directory):
    python tests/test_assembly.py

Requires a populated .env file with CITYDB_* variables.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

from citydb_mcp.db import DatabaseConnection
from citydb_mcp.tools.assembly import assemble_prompt

db = DatabaseConnection()
try:
    prompt = assemble_prompt(db)
    print(f"Prompt length: {len(prompt)} characters")
    print("=" * 60)
    print(prompt[:2000], "..." if len(prompt) > 2000 else "")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
finally:
    db.close()