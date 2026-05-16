"""Shared event type definitions for the agent backends.

Both backends (cloud.py and local.py) are generators that yield (event_type, data) tuples.
The UI's chat_stream switch statement is the contract — backends must not yield other types.

Event types and their data shapes:

    ("status", str)           — short label shown above the chat bubble while processing
    ("ping", "")              — heartbeat during long waits, keeps the Gradio stream alive
    ("thinking", str)         — model reasoning content, shown in the trace panel
    ("tool_call", dict)       — {"tool": str, "args": dict, "iteration": int}
    ("tool_result", dict)     — {"row_count": int, "execution_time_ms": int,
                                  "preview_rows": list, "all_rows": list,
                                  "error": str|None, "iteration": int}
    ("context_update", dict)  — {"used": int, "limit": int}
    ("final", str)            — the assistant's final answer text; may be yielded multiple
                                  times and the UI concatenates them
    ("stopped", "")           — user pressed Stop; backend cleanly exited
"""

from typing import Literal

EventType = Literal[
    "status",
    "ping",
    "thinking",
    "tool_call",
    "tool_result",
    "context_update",
    "final",
    "stopped",
]
