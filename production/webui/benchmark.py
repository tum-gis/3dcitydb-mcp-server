"""CSV-driven sequential benchmark runner for the shared chatbot runtime."""

import csv
import hashlib
import os
import re
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values

from webui.benchmark_results import (
    RESULT_COLUMNS,
    ResultsCsvError,
    ensure_result_schema,
)
from webui.chat_runtime import ChatTurnSettings, resolve_compact
from webui.mcp_client import assemble_system_prompt_sync

_PROVIDERS = {"openai", "anthropic", "ollama"}
_THINKING_LEVELS = {"off", "low", "medium", "high", "max"}
_PROMPT_MODES = {"auto", "compact", "full"}
_MODEL_COLUMNS = {
    "provider",
    "model",
    "base_url",
    "temperature",
    "thinking_level",
    "prompt_mode",
}
_QUERY_COLUMNS = {
    "id",
    "question",
    "ground_truth",
    "citygml_dataset",
    "category",
    "group_number",
    "sequence_number",
}
_RESULT_COLUMNS = RESULT_COLUMNS
_SECRET_ENV_NAMES = re.compile(r"(?:API_KEY|TOKEN|PASSWORD|SECRET)", re.IGNORECASE)
_URL_AUTH = re.compile(r"(?i)(https?://)[^/@\s]+@")
_URL_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|token|password|secret|authorization)=)[^&\s]+"
)


class BenchmarkInputError(ValueError):
    """An invalid CSV file or benchmark option that must fail before model calls."""


@dataclass(frozen=True)
class ModelConfig:
    row_number: int
    provider: str
    model: str
    base_url: str | None
    temperature: float | None
    thinking_level: str | None
    prompt_mode: str | None


@dataclass(frozen=True)
class QueryCase:
    row_number: int
    query_id: str
    question: str
    ground_truth: str
    citygml_dataset: str
    category: str
    group_number: str
    sequence_number: int | None


@dataclass(frozen=True)
class _WorkUnit:
    order: int
    conversation_key: str
    queries: tuple[QueryCase, ...]


def _read_csv(path: str | Path, required: set[str], optional: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            headers = reader.fieldnames
            if not headers:
                raise BenchmarkInputError(f"{source}: CSV is empty or has no header row.")
            if len(headers) != len(set(headers)):
                raise BenchmarkInputError(f"{source}: CSV header contains duplicate column names.")
            missing = required - set(headers)
            if missing:
                raise BenchmarkInputError(
                    f"{source}: missing required column(s): {', '.join(sorted(missing))}."
                )
            unknown = set(headers) - required - optional
            if unknown:
                raise BenchmarkInputError(
                    f"{source}: unsupported column(s): {', '.join(sorted(unknown))}."
                )
            rows: list[dict[str, str]] = []
            for row in reader:
                if None in row:
                    raise BenchmarkInputError(
                        f"{source}: row {reader.line_num} has more values than the header."
                    )
                rows.append({
                    key: (value or "").strip()
                    for key, value in row.items()
                    if key is not None
                })
            return headers, rows
    except BenchmarkInputError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BenchmarkInputError(f"Could not read CSV {source}: {exc}") from exc


def _validate_url(value: str, *, path: Path, row_number: int, column: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise BenchmarkInputError(
            f"{path}: row {row_number}, column {column}: malformed URL."
        ) from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise BenchmarkInputError(
            f"{path}: row {row_number}, column {column}: expected an absolute HTTP(S) URL."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise BenchmarkInputError(
            f"{path}: row {row_number}, column {column}: invalid URL port."
        ) from exc
    if port == 0:
        raise BenchmarkInputError(
            f"{path}: row {row_number}, column {column}: URL port must be between 1 and 65535."
        )


def load_models_csv(path: str | Path) -> list[ModelConfig]:
    """Load and validate the answering-model matrix."""
    source = Path(path)
    _, rows = _read_csv(source, {"provider", "model"}, _MODEL_COLUMNS - {"provider", "model"})
    if not rows:
        raise BenchmarkInputError(f"{source}: no model rows found.")

    models: list[ModelConfig] = []
    for row_number, row in enumerate(rows, start=2):
        provider = row.get("provider", "").lower()
        model = row.get("model", "")
        if provider not in _PROVIDERS:
            raise BenchmarkInputError(
                f"{source}: row {row_number}: provider must be one of "
                f"{', '.join(sorted(_PROVIDERS))}; got {provider or '<blank>'!r}."
            )
        if not model:
            raise BenchmarkInputError(f"{source}: row {row_number}: model is required.")
        base_url = row.get("base_url") or None
        if base_url:
            if provider == "anthropic":
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: base_url is not supported for Anthropic."
                )
            _validate_url(base_url, path=source, row_number=row_number, column="base_url")
        raw_temperature = row.get("temperature", "")
        try:
            temperature = float(raw_temperature) if raw_temperature else None
        except ValueError as exc:
            raise BenchmarkInputError(
                f"{source}: row {row_number}: temperature must be numeric."
            ) from exc
        if temperature is not None and not 0 <= temperature <= 1:
            raise BenchmarkInputError(
                f"{source}: row {row_number}: temperature must be between 0.0 and 1.0."
            )
        thinking_level = row.get("thinking_level") or None
        if thinking_level and thinking_level not in _THINKING_LEVELS:
            raise BenchmarkInputError(
                f"{source}: row {row_number}: thinking_level must be one of "
                f"{', '.join(sorted(_THINKING_LEVELS))}."
            )
        if thinking_level == "max" and provider != "openai":
            raise BenchmarkInputError(
                f"{source}: row {row_number}: thinking_level=max is supported only by OpenAI."
            )
        prompt_mode = row.get("prompt_mode") or None
        if prompt_mode and prompt_mode not in _PROMPT_MODES:
            raise BenchmarkInputError(
                f"{source}: row {row_number}: prompt_mode must be one of "
                f"{', '.join(sorted(_PROMPT_MODES))}."
            )
        models.append(ModelConfig(
            row_number=row_number,
            provider=provider,
            model=model,
            base_url=base_url,
            temperature=temperature,
            thinking_level=thinking_level,
            prompt_mode=prompt_mode,
        ))
    return models


def load_queries_csv(path: str | Path) -> list[QueryCase]:
    """Load query rows and reject invalid conversation grouping before execution."""
    source = Path(path)
    _, rows = _read_csv(source, {"id", "question", "ground_truth", "citygml_dataset"}, _QUERY_COLUMNS - {
        "id", "question", "ground_truth", "citygml_dataset",
    })
    if not rows:
        raise BenchmarkInputError(f"{source}: no query rows found.")

    queries: list[QueryCase] = []
    seen_ids: set[str] = set()
    group_sequences: dict[str, set[int]] = {}
    group_datasets: dict[str, str] = {}
    for row_number, row in enumerate(rows, start=2):
        query_id = row.get("id", "")
        question = row.get("question", "")
        ground_truth = row.get("ground_truth", "")
        dataset = row.get("citygml_dataset", "")
        missing = [
            name for name, value in (
                ("id", query_id),
                ("question", question),
                ("ground_truth", ground_truth),
                ("citygml_dataset", dataset),
            )
            if not value
        ]
        if missing:
            raise BenchmarkInputError(
                f"{source}: row {row_number}: required value(s) are blank: {', '.join(missing)}."
            )
        if query_id in seen_ids:
            raise BenchmarkInputError(f"{source}: row {row_number}: duplicate query id {query_id!r}.")
        seen_ids.add(query_id)

        raw_group = row.get("group_number", "")
        raw_sequence = row.get("sequence_number", "")
        if raw_group:
            try:
                group_value = int(raw_group)
            except ValueError as exc:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: group_number must be a positive integer."
                ) from exc
            if group_value < 1:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: group_number must be a positive integer."
                )
            group_number = str(group_value)
            group_dataset = group_datasets.setdefault(group_number, dataset)
            if group_dataset != dataset:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: group {group_number} contains "
                    "queries assigned to different citygml_dataset values."
                )
            if not raw_sequence:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: sequence_number is required for grouped queries."
                )
            try:
                sequence_number = int(raw_sequence)
            except ValueError as exc:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: sequence_number must be a positive integer."
                ) from exc
            if sequence_number < 1:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: sequence_number must be a positive integer."
                )
            used_sequences = group_sequences.setdefault(group_number, set())
            if sequence_number in used_sequences:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: duplicate sequence_number "
                    f"{sequence_number} in group {group_number}."
                )
            used_sequences.add(sequence_number)
        else:
            if raw_sequence:
                raise BenchmarkInputError(
                    f"{source}: row {row_number}: sequence_number requires a group_number."
                )
            group_number = ""
            sequence_number = None
        queries.append(QueryCase(
            row_number=row_number,
            query_id=query_id,
            question=question,
            ground_truth=ground_truth,
            citygml_dataset=dataset,
            category=row.get("category", ""),
            group_number=group_number,
            sequence_number=sequence_number,
        ))
    return queries


def _work_units(queries: list[QueryCase]) -> list[_WorkUnit]:
    grouped: dict[str, list[QueryCase]] = {}
    units: list[_WorkUnit] = []
    for query in queries:
        if query.group_number:
            grouped.setdefault(query.group_number, []).append(query)
        else:
            units.append(_WorkUnit(query.row_number, f"query-{query.query_id}", (query,)))
    for group_number, members in grouped.items():
        ordered = tuple(sorted(members, key=lambda item: item.sequence_number or 0))
        units.append(_WorkUnit(
            min(member.row_number for member in members),
            f"group-{group_number}",
            ordered,
        ))
    return sorted(units, key=lambda unit: unit.order)


def _effective_settings(model: ModelConfig, args) -> ChatTurnSettings:
    temperature = args.temperature if args.temperature is not None else model.temperature
    thinking_level = args.thinking or model.thinking_level or "off"
    prompt_mode = args.prompt_mode or model.prompt_mode or "auto"
    if thinking_level == "max" and model.provider != "openai":
        raise BenchmarkInputError(
            f"Model CSV row {model.row_number}: effective thinking_level=max "
            f"is not supported by {model.provider}."
        )
    return ChatTurnSettings(
        provider=model.provider,
        model=model.model,
        temperature=temperature,
        enable_thinking=thinking_level,
        prompt_mode=prompt_mode,
        num_ctx=(args.num_ctx or 131072) if model.provider == "ollama" else None,
    )


def _safe_base_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
    except ValueError:
        return "invalid URL"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    secret_names = re.compile(r"(?:key|token|password|secret|auth)", re.IGNORECASE)
    query = [
        (key, "[REDACTED]" if secret_names.search(key) else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path,
        urlencode(query, quote_via=quote),
        "",
    ))


def _safe_error(error: Exception) -> str:
    message = f"{type(error).__name__}: {error}"
    for name, value in os.environ.items():
        if value and _SECRET_ENV_NAMES.search(name):
            message = message.replace(value, "[REDACTED]")
    message = _URL_AUTH.sub(r"\1[REDACTED]@", message)
    return _URL_SECRET_QUERY.sub(r"\1[REDACTED]", message)


def _escape_answer_newlines(answer: str) -> str:
    return answer.replace("\r\n", r"\n").replace("\r", r"\n").replace("\n", r"\n")


@contextmanager
def _model_endpoint(
    model: ModelConfig,
    default_base_url: str | None,
) -> Generator[str | None, None, None]:
    provider = model.provider
    if provider == "anthropic":
        yield None
        return
    env_name = "OLLAMA_BASE_URL" if provider == "ollama" else "OPENAI_BASE_URL"
    explicit_url = model.base_url or default_base_url
    original = os.environ.get(env_name)
    inherited_url = original
    if provider == "openai" and inherited_url is None:
        inherited_url = os.environ.get("OPENAI_API_BASE")
    if explicit_url:
        os.environ[env_name] = explicit_url
    elif original is not None:
        os.environ[env_name] = original
    else:
        os.environ.pop(env_name, None)
    try:
        yield explicit_url or inherited_url
    finally:
        if original is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original


def _database_env_values(path: str | Path) -> dict[str, str]:
    source = Path(path)
    if not source.is_file():
        raise BenchmarkInputError(f"DB environment file not found: {source}")
    values = dotenv_values(source)
    aliases = {
        "POSTGRES_HOST": "CITYDB_HOST",
        "POSTGRES_PORT": "CITYDB_PORT",
        "POSTGRES_DB": "CITYDB_NAME",
        "POSTGRES_USER": "CITYDB_USER",
        "POSTGRES_PASSWORD": "CITYDB_PASSWORD",
    }
    result = {
        key: value
        for key, value in values.items()
        if key in {
            "CITYDB_HOST",
            "CITYDB_PORT",
            "CITYDB_NAME",
            "CITYDB_USER",
            "CITYDB_PASSWORD",
            "CITYDB_SCHEMA",
            "CITYDB_POOL_MAX",
        } and value is not None
    }
    for alias, target in aliases.items():
        if target not in result and values.get(alias):
            result[target] = str(values[alias])
    database_url = values.get("DATABASE_URL")
    if database_url:
        try:
            parsed = urlsplit(str(database_url))
            hostname = parsed.hostname
        except ValueError as exc:
            raise BenchmarkInputError(
                f"{source}: DATABASE_URL is malformed."
            ) from exc
        if parsed.scheme not in {"postgres", "postgresql"} or not hostname:
            raise BenchmarkInputError(
                f"{source}: DATABASE_URL must be a PostgreSQL connection URL."
            )
        try:
            port = parsed.port or 5432
        except ValueError as exc:
            raise BenchmarkInputError(f"{source}: DATABASE_URL has an invalid port.") from exc
        url_values = {
            "CITYDB_HOST": parsed.hostname,
            "CITYDB_PORT": str(port),
            "CITYDB_NAME": unquote(parsed.path.lstrip("/")),
            "CITYDB_USER": unquote(parsed.username or ""),
            "CITYDB_PASSWORD": unquote(parsed.password or ""),
        }
        for key, value in url_values.items():
            result.setdefault(key, value)
    if not result:
        raise BenchmarkInputError(
            f"{source}: no CITYDB_*/POSTGRES_* settings or DATABASE_URL found."
        )
    for key, maximum in (("CITYDB_PORT", 65535), ("CITYDB_POOL_MAX", None)):
        if key in result:
            try:
                numeric_value = int(result[key])
            except ValueError as exc:
                raise BenchmarkInputError(f"{source}: {key} must be an integer.") from exc
            if numeric_value < 1 or (maximum is not None and numeric_value > maximum):
                bound = f"between 1 and {maximum}" if maximum else "at least 1"
                raise BenchmarkInputError(f"{source}: {key} must be {bound}.")
    return result


@contextmanager
def _database_override(path: str | Path | None) -> Generator[None, None, None]:
    if not path:
        yield
        return
    values = _database_env_values(path)
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _append_results(path: str | Path):
    target = Path(path)
    if target.exists() and target.is_dir():
        raise BenchmarkInputError(f"Results path is a directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not target.exists() or target.stat().st_size == 0
    if not needs_header:
        try:
            ensure_result_schema(target)
        except ResultsCsvError as exc:
            raise BenchmarkInputError(str(exc)) from exc
    try:
        stream = target.open("a", encoding="utf-8", newline="")
    except OSError as exc:
        raise BenchmarkInputError(f"Could not open results CSV {target}: {exc}") from exc
    writer = csv.DictWriter(stream, fieldnames=_RESULT_COLUMNS, extrasaction="raise")
    if not needs_header:
        try:
            with target.open("r", encoding="utf-8", newline="") as existing:
                actual_header = next(csv.reader(existing), [])
        except (OSError, csv.Error) as exc:
            stream.close()
            raise BenchmarkInputError(f"Could not validate existing results CSV {target}: {exc}") from exc
        if actual_header != list(_RESULT_COLUMNS):
            stream.close()
            raise BenchmarkInputError(
                f"Existing results CSV {target} has an incompatible header; "
                "choose another output path or move the old file."
            )
    else:
        writer.writeheader()
        stream.flush()
    return stream, writer


def run_benchmark(
    args,
    *,
    stdout: TextIO,
    stderr: TextIO,
    console_factory=None,
    assembler=None,
) -> int:
    """Run all selected model/query/repetition cases sequentially."""
    models = load_models_csv(args.models_csv)
    all_queries = load_queries_csv(args.queries_csv)
    if not args.dataset.strip():
        raise BenchmarkInputError("--dataset must not be blank.")
    queries = [
        query for query in all_queries
        if query.citygml_dataset == args.dataset
    ]
    if not queries:
        datasets = sorted({query.citygml_dataset for query in all_queries})
        raise BenchmarkInputError(
            f"No queries found for dataset {args.dataset!r}. "
            f"Available datasets: {', '.join(datasets)}."
        )
    query_path = Path(args.queries_csv).resolve()
    output_path = Path(args.output).resolve()
    if query_path == output_path or Path(args.models_csv).resolve() == output_path:
        raise BenchmarkInputError("The results output path must differ from both input CSV files.")
    if args.repetitions < 1:
        raise BenchmarkInputError("--repetitions must be at least 1.")
    if args.temperature is not None and not 0 <= args.temperature <= 1:
        raise BenchmarkInputError("--temperature must be between 0.0 and 1.0.")
    if args.thinking is not None and args.thinking not in _THINKING_LEVELS:
        raise BenchmarkInputError(f"Unsupported --thinking value: {args.thinking!r}.")
    if args.prompt_mode is not None and args.prompt_mode not in _PROMPT_MODES:
        raise BenchmarkInputError(f"Unsupported --prompt-mode value: {args.prompt_mode!r}.")
    if args.base_url:
        _validate_url(args.base_url, path=Path("--base-url"), row_number=1, column="--base-url")
    for model in models:
        _effective_settings(model, args)
    if args.num_ctx and not any(model.provider == "ollama" for model in models):
        raise BenchmarkInputError("--num-ctx can only be used when models.csv contains an Ollama row.")
    if args.base_url and not any(
        model.provider in {"openai", "ollama"} for model in models
    ):
        raise BenchmarkInputError(
            "--base-url requires at least one OpenAI or Ollama model row."
        )
    try:
        query_hash = hashlib.sha256(query_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BenchmarkInputError(f"Could not hash query CSV {query_path}: {exc}") from exc

    run_id = str(uuid.uuid4())
    units = _work_units(queries)
    total_turns = sum(len(unit.queries) for unit in units) * len(models) * args.repetitions
    model_csv_line_count = len(models) + 1
    console_type = console_factory
    if console_type is None:
        from webui.cli import ChatConsole, _assemble_prompt_with_progress

        console_type = ChatConsole
        assemble_with_progress = _assemble_prompt_with_progress
    else:
        assemble_with_progress = None
    prompt_assembler = assembler or assemble_system_prompt_sync

    with _database_override(args.env_file):
        stream, writer = _append_results(args.output)
        completed = 0
        failures = 0
        try:
            for model in models:
                settings = _effective_settings(model, args)
                with _model_endpoint(model, args.base_url) as endpoint:
                    compact, _ = resolve_compact(
                        settings.prompt_mode, settings.provider, settings.model
                    )
                    print(
                        f"Preparing {settings.provider}/{settings.model} "
                        f"(models.csv row {model.row_number})...",
                        file=stderr,
                        flush=True,
                    )
                    if assemble_with_progress:
                        system_prompt = assemble_with_progress(
                            stdout,
                            stderr=stderr,
                            compact=compact,
                        )
                    else:
                        system_prompt = prompt_assembler(
                            compact=compact,
                            stderr=stderr,
                        )

                    for repetition in range(1, args.repetitions + 1):
                        for unit in units:
                            console = console_type(
                                settings=settings,
                                system_prompt=system_prompt,
                                keep_story=args.keep_story,
                                reasoning_replay=args.include_reasoning,
                                stderr=stderr,
                                stdout=stdout,
                            )
                            conversation_id = (
                                f"{run_id}:model-{model.row_number}:rep-{repetition}:"
                                f"{unit.conversation_key}"
                            )
                            for query_index, query in enumerate(unit.queries, start=1):
                                group_label = (
                                    query.group_number
                                    if query.group_number
                                    else f"standalone ({query.query_id})"
                                )
                                print(
                                    f"Provider: {settings.provider} | "
                                    f"Model: {settings.model} "
                                    f"(models.csv line {model.row_number} of "
                                    f"{model_csv_line_count}) | "
                                    f"Prompt: {settings.prompt_mode}",
                                    file=stdout,
                                    flush=True,
                                )
                                print(
                                    f"Group: {group_label} | "
                                    f"Query: {query_index} of {len(unit.queries)} | "
                                    f"Repetition: {repetition} of {args.repetitions}",
                                    file=stdout,
                                    flush=True,
                                )
                                print(f"User query: {query.question}", file=stdout, flush=True)
                                started = time.monotonic()
                                status = "completed"
                                answer = ""
                                error_text = ""
                                try:
                                    answer = console.answer(query.question)
                                except KeyboardInterrupt:
                                    status = "interrupted"
                                    error_text = "KeyboardInterrupt"
                                except Exception as exc:
                                    status = "failed"
                                    error_text = _safe_error(exc)
                                elapsed = getattr(
                                    console,
                                    "last_elapsed_seconds",
                                    time.monotonic() - started,
                                )
                                result = {
                                    "run_id": run_id,
                                    "query_file": str(query_path),
                                    "query_file_sha256": query_hash,
                                    "query_id": query.query_id,
                                    "citygml_dataset": query.citygml_dataset,
                                    "question": query.question,
                                    "ground_truth": query.ground_truth,
                                    "category": query.category,
                                    "group_number": query.group_number,
                                    "sequence_number": query.sequence_number or "",
                                    "conversation_id": conversation_id,
                                    "model_row": model.row_number,
                                    "provider": settings.provider,
                                    "model": settings.model,
                                    "base_url": _safe_base_url(endpoint),
                                    "temperature": (
                                        "" if settings.temperature is None else settings.temperature
                                    ),
                                    "thinking_level": settings.enable_thinking,
                                    "prompt_mode": settings.prompt_mode,
                                    "num_ctx": settings.num_ctx or "",
                                    "repetition": repetition,
                                    "generated_answer": (
                                        _escape_answer_newlines(answer)
                                        if args.escape_answer_newlines
                                        else answer
                                    ),
                                    "status": status,
                                    "error": error_text,
                                    "elapsed_seconds": f"{elapsed:.3f}",
                                    "context_tokens": getattr(console, "last_context_tokens", ""),
                                    "context_limit": getattr(console, "last_context_limit", ""),
                                    "context_trimmed_turns": getattr(
                                        console, "last_trimmed_turns", ""
                                    ),
                                }
                                writer.writerow(result)
                                stream.flush()
                                completed += 1
                                failures += status != "completed"
                                if answer:
                                    print(f"Answer:\n{answer}", file=stdout, flush=True)
                                else:
                                    print(
                                        f"No answer generated (status: {status}).",
                                        file=stdout,
                                        flush=True,
                                    )
                                print(file=stdout, flush=True)
                                print(
                                    f"[{status}] {settings.provider}/{settings.model} "
                                    f"query={query.query_id} repetition={repetition}",
                                    file=stderr,
                                    flush=True,
                                )
                                print(file=stderr, flush=True)
                                if status == "interrupted":
                                    raise KeyboardInterrupt
                print(file=stdout, flush=True)
                print(file=stdout, flush=True)
                print(file=stderr, flush=True)
                print(file=stderr, flush=True)
        finally:
            stream.close()

    print(
        f"Benchmark complete: {completed}/{total_turns} turn(s) recorded, "
        f"{failures} failed or interrupted. Results: {Path(args.output)}",
        file=stdout,
        flush=True,
    )
    return 0
