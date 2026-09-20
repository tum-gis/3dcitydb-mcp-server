"""Generate the agent system prompt from the database configured in an .env file.

Runs the same ``assemble_prompt`` the chatbot uses (read-only), and by default
prefixes the chat instructions exactly like the UI's "System Prompt" tab does.
The result is written to ``tests/prompt_output/<dbname>_prompt[_compact].md``.

Usage (from the repo root, inside an environment that has the project deps):
    python tests/generate_prompt.py [--env production/.env] [--compact] [--assembled-only]

Only host, port and database name are printed — never the credentials.
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _cand in (ROOT / "src", Path("/app/src")):
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
for _cand in (ROOT / "production", Path("/app")):
    if (_cand / "webui").is_dir():
        sys.path.insert(0, str(_cand))
        break


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default=str(ROOT / "production" / ".env"),
                    help="env file with CITYDB_HOST/PORT/NAME/USER/PASSWORD/SCHEMA")
    ap.add_argument("--compact", action="store_true", help="compact prompt (small-context models)")
    ap.add_argument("--assembled-only", action="store_true",
                    help="skip the chat-instructions prefix (what assemble_prompt returns)")
    ap.add_argument("--out-dir", default=str(ROOT / "tests" / "prompt_output"))
    args = ap.parse_args()

    from dotenv import load_dotenv
    env_path = Path(args.env)
    if env_path.is_file():
        # override=True: the .env is the source of truth for this run, even if the
        # surrounding environment (e.g. a compose container) already sets CITYDB_*.
        load_dotenv(env_path, override=True)
    else:
        print(f"env file not found: {env_path} — using the current environment")

    from citydb_mcp.db import DatabaseConnection
    from citydb_mcp.tools.assembly import assemble_prompt

    db = DatabaseConnection()
    p = db.conn_params
    print(f"Database: {p['host']}:{p['port']}/{p['dbname']} (schema {db.schema}, read-only)")

    started = time.time()
    prompt = assemble_prompt(
        db, include_query_agent_extras=True, compact=args.compact, force_refresh=True
    )
    if not args.assembled_only:
        from webui.llm_utils import CHAT_INSTRUCTIONS
        prompt = CHAT_INSTRUCTIONS + "\n\n" + prompt

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_compact" if args.compact else ""
    out = out_dir / f"{p['dbname']}_prompt{suffix}.md"
    out.write_text(prompt, encoding="utf-8")
    print(f"Wrote {out} — {len(prompt):,} chars in {time.time() - started:.1f}s")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
