"""Interactive terminal chat using the same runtime and MCP tools as the WebGUI.

Run with ``python -m webui.cli chat`` in the chatbot environment.
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import TextIO

from dotenv import load_dotenv

load_dotenv(override=False)

from webui.chat_runtime import (  # noqa: E402
    ChatTurnSettings,
    build_reasoning_transcript,
    build_tool_cache,
    cache_is_fresh,
    format_tool_cache_note,
    prepare_chat_turn,
    resolve_compact,
    stream_chat_turn,
    summarize_story_ollama,
    summarize_story_openai,
)
from webui.llm_utils import (  # noqa: E402
    default_model,
    detect_default_provider,
    full_result_csv,
    scratchpad_enabled,
    scratchpad_tool_specs,
)
from webui.mcp_client import (  # noqa: E402
    assemble_system_prompt_sync,
    run_tool_sync,
)

_CTX_CHOICES = (8192, 32768, 65536, 131072, 262144)
_SELF_STORY = re.compile(
    r"\[?(?:how i solved it|verified queries|auto-generated summary|so habe ich es gelöst)\b.*",
    re.IGNORECASE | re.DOTALL,
)


def _assemble_prompt_with_progress(
    stdout: TextIO,
    *,
    stderr: TextIO,
    compact: bool = False,
) -> str:
    """Assemble the MCP prompt while printing its live JSONL progress records."""
    print("Assembling system prompt from the database context...", file=stdout, flush=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="citydb-chat-prompt-",
        suffix=".jsonl",
        delete=False,
    ) as progress_file:
        progress_path = progress_file.name

    result: dict[str, str | Exception] = {}
    finished = threading.Event()

    def assemble() -> None:
        try:
            result["prompt"] = assemble_system_prompt_sync(
                compact=compact,
                progress_path=progress_path,
                stderr=stderr,
            )
        except Exception as exc:
            result["error"] = exc
        finally:
            finished.set()

    worker = threading.Thread(target=assemble, name="prompt-assembly", daemon=True)
    worker.start()
    offset = 0
    remainder = ""
    started: list[tuple[int, str, float]] = []
    step_count = 0

    def drain_progress() -> None:
        nonlocal offset, remainder, step_count
        with open(progress_path, encoding="utf-8") as stream:
            stream.seek(offset)
            chunk = stream.read()
            offset = stream.tell()
        records = (remainder + chunk).split("\n")
        remainder = records.pop()
        for raw in records:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            step = record.get("step")
            total = record.get("total")
            label = str(record.get("label", "")).strip()
            if not isinstance(step, int) or not isinstance(total, int):
                continue
            if record.get("state") == "start":
                step_count = max(step_count, step)
                started.append((step, label, time.monotonic()))
                print(
                    f"  Step {step}/{total}: {label} ",
                    end="",
                    file=stdout,
                    flush=True,
                )
            elif record.get("state") == "done":
                match_index = next(
                    (
                        index
                        for index in range(len(started) - 1, -1, -1)
                        if started[index][1] == label
                    ),
                    None,
                )
                if match_index is None:
                    completed_step, elapsed = max(1, step - 1), None
                else:
                    completed_step, _, began = started.pop(match_index)
                    elapsed = f"{time.monotonic() - began:.1f} s"
                if elapsed is None:
                    print(
                        f"  Step {completed_step}/{total}: {label} - finished",
                        file=stdout,
                        flush=True,
                    )
                else:
                    print(f"- finished in {elapsed}", file=stdout, flush=True)

    try:
        while not finished.wait(0.2):
            drain_progress()
        drain_progress()
        if remainder.strip():
            remainder += "\n"
            drain_progress()
    finally:
        worker.join()
        try:
            os.unlink(progress_path)
        except OSError as exc:
            print(
                f"Warning: could not remove prompt progress file {progress_path}: {exc}",
                file=stderr,
                flush=True,
            )

    if "error" in result:
        error = result["error"]
        print("System prompt assembly failed.", file=stderr, flush=True)
        if isinstance(error, Exception):
            raise error
        raise RuntimeError(f"Prompt assembly failed: {error}")
    prompt = result.get("prompt")
    if not isinstance(prompt, str):
        raise RuntimeError("Prompt assembly completed without returning a prompt.")
    elapsed = "with no reported steps" if not step_count else f"after {step_count} steps"
    print(f"System prompt assembled {elapsed}.", file=stdout, flush=True)
    return prompt


def _format_activity(events: list[tuple[str, object]]) -> str:
    """Format backend events with persistent labels separate from the answer."""
    lines: list[str] = []
    reasoning: list[str] = []

    def flush_reasoning() -> None:
        if reasoning:
            lines.append("Provider-emitted reasoning:\n" + "".join(reasoning).strip())
            reasoning.clear()

    for event, data in events:
        if event in ("thinking", "thinking_token"):
            reasoning.append(str(data))
            continue
        flush_reasoning()
        if event == "status":
            lines.append(f"Status: {data}")
        elif event == "tool_call":
            sql = data.get("args", {}).get("sql", "")
            lines.append(f"Tool call (run_query):\n```sql\n{sql}\n```")
        elif event == "tool_result":
            if data.get("error"):
                lines.append(f"Tool result: ERROR — {data['error']}")
            else:
                lines.append(
                    f"Tool result: {data.get('row_count', 0)} row(s), "
                    f"{data.get('execution_time_ms', 0)} ms"
                )
        elif event == "context_update":
            lines.append(
                f"Backend context estimate: {data.get('used', 0):,} / "
                f"{data.get('limit', 0):,} tokens"
            )
        elif event == "stopped":
            lines.append("Generation stopped.")
    flush_reasoning()
    return "\n\n".join(lines)


@dataclass
class ChatConsole:
    """Maintain one CLI conversation while sharing the production chat runtime."""

    settings: ChatTurnSettings
    system_prompt: str
    keep_story: bool = True
    reasoning_replay: bool = False
    show_reasoning: bool = False
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    history: list[list[str | None]] = field(default_factory=list)
    reasoning_history: list[str | None] = field(default_factory=list)
    story_history: list[str | None] = field(default_factory=list)
    tool_cache: dict | None = None
    last_context_tokens: int | None = None
    last_context_limit: int | None = None
    last_trimmed_turns: int | None = None
    last_elapsed_seconds: float = 0.0
    last_story: str = ""
    # Scratchpad (CITYDB_SCRATCHPAD=true): every console has its own user, so
    # benchmark conversations do not see each other's items. A given id (e.g.
    # the web UI's, from the browser's localStorage) shares that scratchpad;
    # a generated one is removed again by close().
    scratchpad_user: str | None = None
    keep_scratchpad: bool = False
    turn_count: int = 0

    def __post_init__(self) -> None:
        if self.scratchpad_user:
            self.keep_scratchpad = True
        else:
            self.scratchpad_user = str(uuid.uuid4())
        self.session_id = f"cli-{uuid.uuid4().hex[:12]}"
        self._last_query_raw = ""

    def tool_ctx(self) -> dict:
        return {"user_id": self.scratchpad_user, "session_id": self.session_id,
                "turn_id": str(self.turn_count)}

    def _run_query(self, sql: str) -> str:
        raw = run_tool_sync("run_query", {"sql": sql}, stderr=self.stderr, ctx=self.tool_ctx())
        self._last_query_raw = raw
        return raw

    def _extra_tools(self) -> dict:
        """The scratchpad tools for the backends ({} unless the scratchpad is on)."""
        def tool(name: str):
            def run(args: dict) -> str:
                self._last_query_raw = ""  # the answer is about the tool call, not an earlier list
                return run_tool_sync(name, args, stderr=self.stderr, ctx=self.tool_ctx())
            return run

        return {spec["function"]["name"]: {"spec": spec, "run": tool(spec["function"]["name"])}
                for spec in scratchpad_tool_specs()}

    def close(self) -> None:
        """Remove the items of a generated scratchpad user (not of a given one)."""
        if self.keep_scratchpad or not scratchpad_enabled():
            return
        try:
            from citydb_mcp.tools import scratchpad

            removed = scratchpad.delete_user(self.scratchpad_user)
            if removed:
                print(f"Scratchpad: removed {removed} item(s) of this run.", file=self.stderr)
        except Exception as exc:
            print(f"Scratchpad cleanup failed: {exc}", file=self.stderr)

    def reset(self) -> None:
        self.history.clear()
        self.reasoning_history.clear()
        self.story_history.clear()
        self.tool_cache = None

    def answer(self, question: str) -> str:
        """Run one turn and return only the cleaned final answer."""
        self.last_context_tokens = None
        self.last_context_limit = None
        self.last_trimmed_turns = None
        self.last_elapsed_seconds = 0.0
        if self.keep_story and self.settings.provider not in ("openai", "ollama"):
            self.last_story = (
                f"Story distillation is not supported for provider "
                f"{self.settings.provider!r}."
            )
        else:
            self.last_story = ""
        notes = (
            [format_tool_cache_note(self.tool_cache)]
            if cache_is_fresh(self.tool_cache)
            else []
        )
        with redirect_stdout(self.stderr):
            prepared = prepare_chat_turn(
                question,
                self.history,
                self.system_prompt,
                self.settings,
                reasoning_history=self.reasoning_history,
                story_history=self.story_history,
                reasoning_replay=self.reasoning_replay,
                add_story=self.keep_story,
                leading_system_notes=notes,
            )
        limit = prepared.context_limit
        self.last_context_tokens = prepared.estimated_tokens
        self.last_context_limit = limit
        self.last_trimmed_turns = prepared.dropped_messages
        percentage = prepared.estimated_tokens / limit * 100 if limit else 0
        print(
            f"Context estimate: {prepared.estimated_tokens:,} / {limit:,} tokens "
            f"({percentage:.0f}%).",
            file=self.stdout,
        )
        if prepared.dropped_messages:
            print(
                f"Context cap removed {prepared.dropped_messages} oldest "
                "message(s).",
                file=self.stderr,
            )

        events: list[tuple[str, object]] = []
        answer_parts: list[str] = []
        pending_sql = ""
        self._last_query_raw = ""
        self.turn_count += 1
        started = time.monotonic()
        with redirect_stdout(self.stderr):
            try:
                for event, data in stream_chat_turn(
                    self.settings,
                    prepared.messages,
                    tool_executor=self._run_query,
                    extra_tools=self._extra_tools(),
                ):
                    events.append((event, data))
                    if event == "tool_call":
                        # Only run_query results are reusable as a tool cache.
                        is_query = data.get("tool", "run_query") == "run_query"
                        pending_sql = data.get("args", {}).get("sql", "") if is_query else ""
                    elif event == "tool_result" and not data.get("error") and pending_sql:
                        cached = build_tool_cache(
                            pending_sql,
                            data.get("all_rows") or data.get("preview_rows") or [],
                            int(data.get("row_count") or 0),
                        )
                        if cached:
                            self.tool_cache = cached
                    elif event == "final":
                        answer_parts.append(str(data))
                    elif event == "stopped":
                        raise RuntimeError("The backend stopped generation before answering.")
            finally:
                self.last_elapsed_seconds = time.monotonic() - started

        if not answer_parts:
            raise RuntimeError("The backend finished without producing a final answer.")

        transcript = build_reasoning_transcript(events)
        self.reasoning_history.append(transcript or None)

        if self.keep_story and self.settings.provider in ("openai", "ollama"):
            if any(event == "tool_call" for event, _ in events) and transcript:
                if self.settings.provider == "openai":
                    with redirect_stdout(self.stderr):
                        story, error = summarize_story_openai(
                            transcript, question, self.settings.model
                        )
                else:
                    with redirect_stdout(self.stderr):
                        story, error = summarize_story_ollama(
                            transcript,
                            question,
                            self.settings.model,
                            self.settings.num_ctx,
                        )
                if story:
                    self.story_history.append(story)
                    self.last_story = story
                else:
                    self.story_history.append(None)
                    self.last_story = f"Story distillation skipped: {error}"
                    print(f"Story distillation skipped: {error}", file=self.stderr)
            else:
                self.story_history.append(None)
                self.last_story = (
                    "No SQL query was executed for this answer; it was "
                    "generated from the current conversation context and "
                    "built-in knowledge."
                )
        else:
            self.story_history.append(None)

        answer = _SELF_STORY.split("".join(answer_parts), maxsplit=1)[0].rstrip()
        self.history.append([question, answer])
        if not self.settings.full_lists:
            # short lists: the model shows 5 rows and points to the full list,
            # which is printed here (kept out of the history, like the web UI's)
            answer += full_result_csv(self._last_query_raw)
        if self.show_reasoning:
            activity = _format_activity(events)
            if activity:
                print(
                    "\nReasoning / Agent activity — not the final answer\n"
                    "---------------------------------------------------\n"
                    f"{activity}\n"
                    "------------------------------ end of activity ------------------------------",
                    file=self.stderr,
                )
        return answer


def _scratchpad_user(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a uuid: {value!r}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="3dcitydb-chat",
        description="Chat with or benchmark the 3DCityDB assistant using the WebGUI's chat runtime.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    chat = subparsers.add_parser("chat", help="start an interactive chat session")
    chat.add_argument("--provider", choices=("openai", "anthropic", "ollama"))
    chat.add_argument("--model", help="model name; defaults to the provider's first configured model")
    chat.add_argument(
        "--base-url",
        help="provider endpoint override (OPENAI_BASE_URL for openai, OLLAMA_BASE_URL for ollama)",
    )
    chat.add_argument("--temperature", type=float, help="sampling temperature (unset uses provider default)")
    chat.add_argument(
        "--thinking",
        choices=("off", "low", "medium", "high", "max"),
        default="off",
        help="provider-supported reasoning effort (default: off)",
    )
    chat.add_argument(
        "--prompt-mode",
        choices=("auto", "compact", "full"),
        default="auto",
        help="system prompt size policy (default: auto)",
    )
    chat.add_argument(
        "--num-ctx",
        type=int,
        choices=_CTX_CHOICES,
        help="Ollama context limit; defaults to the WebGUI's 131072",
    )
    chat.add_argument(
        "--full-lists",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="list every row in result tables; --no-full-lists shows 5 example rows above "
             "10 rows (default: enabled)",
    )
    chat.add_argument(
        "--keep-story",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="retain distilled summaries between turns when supported (default: enabled)",
    )
    chat.add_argument(
        "--include-reasoning",
        action="store_true",
        help="replay prior turns' provider reasoning in later prompts",
    )
    chat.add_argument(
        "--show-reasoning",
        action="store_true",
        help="show provider-emitted reasoning and tool activity in a labeled stderr section",
    )
    chat.add_argument(
        "--quiet-stderr",
        action="store_true",
        help="suppress stderr output, including INFO logs and backend diagnostics",
    )
    chat.add_argument(
        "--question",
        help="answer one question and exit; otherwise read stdin or run interactively",
    )
    chat.add_argument(
        "--scratchpad-user",
        type=_scratchpad_user,
        help=(
            "with CITYDB_SCRATCHPAD=true: use this user's scratchpad (a uuid, e.g. the web UI's "
            "citydb_user_id from the browser) and keep its items; by default each run gets its "
            "own scratchpad, removed at exit"
        ),
    )

    benchmark = subparsers.add_parser(
        "benchmark",
        help="run CSV-defined questions against one or more model configurations",
    )
    benchmark.add_argument("--models-csv", required=True, help="model matrix CSV path")
    benchmark.add_argument("--queries-csv", required=True, help="question and ground-truth CSV path")
    benchmark.add_argument("--dataset", required=True, help="exact citygml_dataset label to select")
    benchmark.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="number of independent repetitions (default: 1)",
    )
    benchmark.add_argument(
        "--output",
        required=True,
        help="results CSV path; results append and flush after each query turn",
    )
    benchmark.add_argument(
        "--base-url",
        help="run-wide endpoint override for OpenAI-compatible or Ollama model rows",
    )
    benchmark.add_argument(
        "--temperature",
        type=float,
        help="run-wide sampling temperature; overrides models.csv when provided",
    )
    benchmark.add_argument(
        "--thinking",
        choices=("off", "low", "medium", "high", "max"),
        default=None,
        help="run-wide thinking level; overrides models.csv when provided (default: off)",
    )
    benchmark.add_argument(
        "--prompt-mode",
        choices=("auto", "compact", "full"),
        default=None,
        help="run-wide prompt size policy; overrides models.csv when provided (default: auto)",
    )
    benchmark.add_argument(
        "--num-ctx",
        type=int,
        choices=_CTX_CHOICES,
        help="Ollama context limit; defaults to the WebGUI's 131072",
    )
    benchmark.add_argument(
        "--full-lists",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="list every row in result tables; --no-full-lists shows 5 example rows above "
             "10 rows (default: enabled)",
    )
    benchmark.add_argument(
        "--keep-story",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="retain distilled summaries between grouped turns (default: enabled)",
    )
    benchmark.add_argument(
        "--include-reasoning",
        action="store_true",
        help="replay prior turns' provider reasoning within grouped conversations",
    )
    benchmark.add_argument(
        "--escape-answer-newlines",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "replace answer line breaks with literal \\n in the results CSV "
            "(default: enabled; use --no-escape-answer-newlines to disable)"
        ),
    )
    benchmark.add_argument(
        "--env-file",
        help="optional dotenv file overriding CITYDB_* settings for this benchmark",
    )
    benchmark.add_argument(
        "--quiet-stderr",
        action="store_true",
        help="suppress stderr output, including INFO logs and backend diagnostics",
    )
    return parser


def _resolve_settings(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ChatTurnSettings:
    provider = args.provider or (
        "openai" if args.base_url else detect_default_provider()
    )
    if provider is None:
        parser.error(
            "No provider detected. Configure OPENAI_API_KEY, OPENAI_BASE_URL, "
            "ANTHROPIC_API_KEY, or a reachable OLLAMA_BASE_URL, or pass --provider."
        )

    if args.base_url:
        if provider == "openai":
            os.environ["OPENAI_BASE_URL"] = args.base_url
        elif provider == "ollama":
            os.environ["OLLAMA_BASE_URL"] = args.base_url
        else:
            parser.error("--base-url is supported for OpenAI-compatible and Ollama endpoints.")

    model = args.model or default_model(provider)
    if not model:
        parser.error(f"No default model found for {provider}; specify --model.")
    if args.temperature is not None and not 0 <= args.temperature <= 1:
        parser.error("--temperature must be between 0.0 and 1.0.")
    if args.thinking == "max" and provider != "openai":
        parser.error("--thinking max is supported only by the OpenAI provider.")

    return ChatTurnSettings(
        provider=provider,
        model=model,
        temperature=args.temperature,
        enable_thinking=args.thinking,
        prompt_mode=args.prompt_mode,
        num_ctx=(
            (args.num_ctx or 131072)
            if provider == "ollama"
            else None
        ),
        full_lists=getattr(args, "full_lists", True),
    )


def run_chat(
    args: argparse.Namespace,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = _build_parser()
    settings = _resolve_settings(args, parser)
    interactive = stdin.isatty() and args.question is None
    piped_lines: list[str] | None = None
    if args.question is None and not interactive:
        piped_lines = [line.strip() for line in stdin if line.strip()]
        questions = [
            line for line in piped_lines
            if line.lower() not in ("/new", "/bye", "/exit", "/quit")
        ]
        if not questions:
            print(
                "No question provided. For interactive chat, allocate a terminal "
                "and keep stdin open (for Docker: use `docker exec -it`). "
                "Otherwise pass --question or pipe questions to stdin.",
                file=stderr,
            )
            return 2

    effective_compact, mode_label = resolve_compact(
        settings.prompt_mode, settings.provider, settings.model
    )
    print(
        f"Provider: {settings.provider} | Model: {settings.model} | "
        f"Prompt: {mode_label}",
        file=stdout,
    )
    with redirect_stdout(stderr):
        system_prompt = _assemble_prompt_with_progress(
            stdout,
            stderr=stderr,
            compact=effective_compact,
        )
    console = ChatConsole(
        settings=settings,
        system_prompt=system_prompt,
        keep_story=args.keep_story,
        reasoning_replay=args.include_reasoning,
        show_reasoning=args.show_reasoning,
        stderr=stderr,
        stdout=stdout,
        scratchpad_user=getattr(args, "scratchpad_user", None),
    )
    scratchpad_user = getattr(console, "scratchpad_user", None)
    if scratchpad_user and scratchpad_enabled():
        mode = "kept" if getattr(console, "keep_scratchpad", False) else "removed at exit"
        print(f"Scratchpad user: {scratchpad_user} (items {mode})", file=stderr)
    try:
        return _converse(console, args, interactive, piped_lines, stdin=stdin, stdout=stdout,
                         stderr=stderr)
    finally:
        close = getattr(console, "close", None)
        if close:
            close()


def _converse(
    console: ChatConsole,
    args: argparse.Namespace,
    interactive: bool,
    piped_lines: list[str] | None,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    def process(question: str) -> None:
        answer = console.answer(question)
        if interactive:
            print(f"\nAssistant answer:\n{answer}\n", file=stdout, flush=True)
        else:
            print(answer, file=stdout, flush=True)

    if args.question is not None:
        try:
            process(args.question)
        except Exception as exc:
            print(f"Chat failed ({type(exc).__name__}): {exc}", file=stderr)
            return 1
        return 0

    if interactive:
        print(
            "Enter /new to clear conversation history or /bye, /exit to quit.",
            file=stdout,
        )
        while True:
            print("\nYou: ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if not line:
                break
            question = line.strip()
            if question.lower() in ("/bye", "/exit", "/quit"):
                break
            if question.lower() == "/new":
                console.reset()
                print("Conversation cleared.", file=stdout)
                continue
            if not question:
                continue
            try:
                process(question)
            except Exception as exc:
                print(f"Chat turn failed ({type(exc).__name__}): {exc}", file=stderr)
        return 0

    for line in piped_lines or []:
        question = line.strip()
        if question.lower() in ("/bye", "/exit", "/quit"):
            break
        if question.lower() == "/new":
            console.reset()
            continue
        try:
            process(question)
        except Exception as exc:
            print(f"Chat failed ({type(exc).__name__}): {exc}", file=stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    stderr = sys.stderr
    if args.quiet_stderr:
        stderr = open(os.devnull, "w", encoding="utf-8")

    previous_logging_disable = logging.root.manager.disable
    if args.quiet_stderr:
        logging.disable(logging.CRITICAL)
    try:
        with redirect_stderr(stderr):
            if args.command == "benchmark":
                from webui.benchmark import run_benchmark

                return run_benchmark(
                    args,
                    stdout=sys.stdout,
                    stderr=stderr,
                )
            return run_chat(
                args,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=stderr,
            )
    except KeyboardInterrupt:
        print("\nChat interrupted.", file=stderr)
        return 130
    except Exception as exc:
        print(f"Chat failed ({type(exc).__name__}): {exc}", file=stderr)
        return 1
    finally:
        if args.quiet_stderr:
            logging.disable(previous_logging_disable)
            stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
