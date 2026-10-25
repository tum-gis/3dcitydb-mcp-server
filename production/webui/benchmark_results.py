"""Shared benchmark-results CSV schema and safe grading persistence."""

import csv
import os
import tempfile
from pathlib import Path
from typing import TextIO

BENCHMARK_RESULT_COLUMNS = (
    "run_id",
    "query_file",
    "query_file_sha256",
    "query_id",
    "citygml_dataset",
    "question",
    "ground_truth",
    "category",
    "group_number",
    "sequence_number",
    "conversation_id",
    "model_row",
    "provider",
    "model",
    "base_url",
    "temperature",
    "thinking_level",
    "prompt_mode",
    "num_ctx",
    "repetition",
    "generated_answer",
    "answer_story",
    "status",
    "error",
    "elapsed_seconds",
    "context_tokens",
    "context_limit",
    "context_trimmed_turns",
)
HUMAN_GRADING_COLUMNS = (
    "human_correctness",
    "human_completeness",
    "human_relevance",
    "human_clarity",
    "human_language_conformity",
    "human_comments",
)
RESULT_COLUMNS = BENCHMARK_RESULT_COLUMNS + HUMAN_GRADING_COLUMNS
# Files written before the "Language conformity" dimension existed carry only
# the first four human rating columns; they are accepted for reading and
# migrated on save.
LEGACY_RESULT_COLUMNS = (
    "human_correctness",
    "human_completeness",
    "human_relevance",
    "human_clarity",
    "human_comments",
)
# Files written before the "answer_story" column existed lack it; they are
# accepted for reading and migrated by inserting an empty column.
_PRE_STORY_RESULT_COLUMNS = BENCHMARK_RESULT_COLUMNS[:21] + BENCHMARK_RESULT_COLUMNS[22:]
_VALID_HEADERS = (
    BENCHMARK_RESULT_COLUMNS,
    RESULT_COLUMNS,
    BENCHMARK_RESULT_COLUMNS + LEGACY_RESULT_COLUMNS,
    _PRE_STORY_RESULT_COLUMNS,
    _PRE_STORY_RESULT_COLUMNS + HUMAN_GRADING_COLUMNS,
)
_RATING_COLUMNS = HUMAN_GRADING_COLUMNS[:-1]

# Free-text columns whose line breaks are converted to real newlines on load.
# The legacy CLI default stored answer line breaks as literal two-character
# "\\n" sequences; the grader/ground-truth editor stored real newlines. So a
# single file mixes both. We normalize only these text columns (never e.g.
# ``query_file``/``base_url`` — a Windows path such as ``C:\new…`` legitimately
# contains the "\\n" substring and must not be touched).
_NEWLINE_TEXT_COLUMNS = (
    "question",
    "ground_truth",
    "generated_answer",
    "answer_story",
    "human_comments",
)


def _normalize_newlines(text: str) -> str:
    """Convert legacy escaped line breaks into real newlines.

    Turns the literal two-character sequences ``\\r\\n`` / ``\\n`` / ``\\r``
    (as written by the legacy benchmark runner) and stray CR/LF bytes into
    genuine ``\n`` line breaks. Text that already uses real newlines passes
    through unchanged, so the conversion is safe to apply repeatedly.
    """
    if not text:
        return text
    return (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )


class ResultsCsvError(ValueError):
    """An invalid or inaccessible benchmark-results CSV."""


def _read_header(stream: TextIO, path: Path) -> tuple[str, ...]:
    try:
        header = next(csv.reader(stream, strict=True), None)
    except csv.Error as exc:
        raise ResultsCsvError(f"Could not parse results CSV {path}: {exc}") from exc
    if header is None:
        raise ResultsCsvError(f"Results CSV {path} is empty.")
    if len(header) != len(set(header)) or tuple(header) not in _VALID_HEADERS:
        raise ResultsCsvError(
            f"Results CSV {path} has an unsupported header. Expected the benchmark "
            "results columns, optionally followed by the human grading columns."
        )
    return tuple(header)


def _validated_rows(stream: TextIO, path: Path, header: tuple[str, ...]):
    reader = csv.DictReader(stream, fieldnames=header, strict=True)
    for row_number, row in enumerate(reader, start=2):
        if None in row or any(row.get(column) is None for column in header):
            raise ResultsCsvError(
                f"Results CSV {path} has a malformed row near record {row_number}."
            )
        yield row_number, row


def result_identity(row: dict[str, str]) -> tuple[str, ...]:
    """Return the stable key for one result across saves and CSV rewrites."""
    identity_columns = (
        "run_id",
        "query_file_sha256",
        "query_id",
        "model_row",
        "repetition",
    )
    identity = tuple(row.get(column, "") for column in identity_columns)
    if any(not value for value in identity):
        raise ResultsCsvError(
            "A result row is missing run, query, model-row, or repetition identity."
        )
    return identity


def load_result_rows(path: str | Path) -> list[dict[str, str]]:
    """Load benchmark rows and fill blank grading fields for legacy files."""
    target = Path(path)
    try:
        # utf-8-sig transparently strips a leading BOM (the files we write carry
        # one for MS Excel), so plain-UTF-8 and BOM-prefixed files both parse.
        with target.open("r", encoding="utf-8-sig", newline="") as stream:
            header = _read_header(stream, target)
            rows = [
                row
                for _, row in _validated_rows(stream, target, header)
            ]
    except ResultsCsvError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ResultsCsvError(f"Could not read results CSV {target}: {exc}") from exc

    # Convert any legacy escaped line breaks to real newlines across the whole
    # loaded file so the grader/preview and every subsequent save see one
    # consistent format. Idempotent: already-real newlines are untouched, and
    # only the free-text columns are converted (paths are left alone).
    for row in rows:
        for column in _NEWLINE_TEXT_COLUMNS:
            value = row.get(column)
            if value:
                row[column] = _normalize_newlines(value)

    seen: set[tuple[str, ...]] = set()
    for row in rows:
        for column in (*BENCHMARK_RESULT_COLUMNS, *HUMAN_GRADING_COLUMNS):
            row.setdefault(column, "")
        identity = result_identity(row)
        if identity in seen:
            raise ResultsCsvError(
                f"Results CSV {target} contains duplicate result identities."
            )
        seen.add(identity)
    return rows


def _open_temporary_csv(target: Path):
    try:
        return tempfile.NamedTemporaryFile(
            mode="w",
            # utf-8-sig writes a leading BOM so MS Excel detects the encoding and
            # keeps quoted multi-line cells intact on import; reading strips it.
            encoding="utf-8-sig",
            newline="",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        )
    except OSError as exc:
        raise ResultsCsvError(f"Could not create a temporary results file: {exc}") from exc


def _remove_temporary_csv(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ResultsCsvError(f"Could not remove temporary results file {path}: {exc}") from exc


def _atomic_write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    temporary_path: Path | None = None
    try:
        with _open_temporary_csv(path) as stream:
            temporary_path = Path(stream.name)
            writer = csv.DictWriter(
                stream,
                fieldnames=RESULT_COLUMNS,
                extrasaction="raise",
                # CRLF record terminators are the CSV default Excel expects;
                # in-cell newlines stay real (quoted), so the web UI renders them.
                lineterminator="\r\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in RESULT_COLUMNS})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except ResultsCsvError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ResultsCsvError(f"Could not save grading to results CSV {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            _remove_temporary_csv(temporary_path)


def ensure_result_schema(path: str | Path) -> None:
    """Add empty human-grading columns to an existing legacy results CSV."""
    target = Path(path)
    if not target.exists():
        return
    temporary_path: Path | None = None
    try:
        if target.stat().st_size == 0:
            return
        with target.open("r", encoding="utf-8-sig", newline="") as source:
            header = _read_header(source, target)
            if header in (
                RESULT_COLUMNS,
                BENCHMARK_RESULT_COLUMNS + LEGACY_RESULT_COLUMNS,
            ):
                for _ in _validated_rows(source, target, header):
                    pass
                return

            with _open_temporary_csv(target) as output:
                temporary_path = Path(output.name)
                writer = csv.DictWriter(
                    output,
                    fieldnames=RESULT_COLUMNS,
                    extrasaction="raise",
                    lineterminator="\r\n",
                )
                writer.writeheader()
                for _, row in _validated_rows(source, target, header):
                    writer.writerow({
                        column: row.get(column, "")
                        for column in RESULT_COLUMNS
                    })
                output.flush()
                os.fsync(output.fileno())
        if temporary_path is not None:
            os.replace(temporary_path, target)
    except ResultsCsvError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ResultsCsvError(f"Could not update results CSV schema for {target}: {exc}") from exc
    finally:
        if temporary_path is not None:
            _remove_temporary_csv(temporary_path)


def save_human_grade(
    path: str | Path,
    identity: tuple[str, ...],
    grade: dict[str, str],
) -> None:
    """Update one row's human grade while preserving the latest CSV contents."""
    target = Path(path)
    if set(grade) != set(HUMAN_GRADING_COLUMNS):
        raise ResultsCsvError("A human grade must include all five ratings and comments.")
    normalized: dict[str, str] = {}
    for column, value in grade.items():
        text = str(value)
        if column in _RATING_COLUMNS and text not in {"", "0", "1", "2", "3"}:
            raise ResultsCsvError(f"{column} must be blank or one of 0, 1, 2, or 3.")
        normalized[column] = text

    rows = load_result_rows(target)
    matching_rows = [row for row in rows if result_identity(row) == identity]
    if len(matching_rows) != 1:
        raise ResultsCsvError(
            "The selected result row is no longer present exactly once in the CSV."
        )
    matching_rows[0].update(normalized)
    _atomic_write_rows(target, rows)


def save_ground_truth(
    path: str | Path,
    identity: tuple[str, ...],
    ground_truth: str,
) -> int:
    """Update a query's reference answer across its model and repetition rows."""
    target = Path(path)
    rows = load_result_rows(target)
    selected_rows = [row for row in rows if result_identity(row) == identity]
    if len(selected_rows) != 1:
        raise ResultsCsvError(
            "The selected result row is no longer present exactly once in the CSV."
        )

    query_identity = identity[:3]
    matching_rows = [
        row for row in rows if result_identity(row)[:3] == query_identity
    ]
    for row in matching_rows:
        row["ground_truth"] = ground_truth
    _atomic_write_rows(target, rows)
    return len(matching_rows)


def is_human_graded(row: dict[str, str]) -> bool:
    """Whether all five rating dimensions have been scored."""
    return all(row.get(column, "") in {"0", "1", "2", "3"} for column in _RATING_COLUMNS)
