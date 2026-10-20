"""Standalone local web application for human-grading benchmark results."""

import argparse
import sys
import warnings
from pathlib import Path
from typing import Any

import gradio as gr

from webui.benchmark_results import (
    HUMAN_GRADING_COLUMNS,
    ResultsCsvError,
    is_human_graded,
    load_result_rows,
    result_identity,
    save_ground_truth,
    save_human_grade,
)

_RATING_CHOICES = [
    ("Not rated", ""),
]
_RATING_CHOICES_BY_FIELD = {
    "human_correctness": _RATING_CHOICES + [
        ("0 — incorrect", "0"),
        ("1 — mostly incorrect", "1"),
        ("2 — mostly correct, with minor errors", "2"),
        ("3 — fully correct", "3"),
    ],
    "human_completeness": _RATING_CHOICES + [
        ("0 — omits most requested information", "0"),
        ("1 — includes only a few key points", "1"),
        ("2 — includes most key points", "2"),
        ("3 — includes all important points", "3"),
    ],
    "human_relevance": _RATING_CHOICES + [
        ("0 — unrelated", "0"),
        ("1 — mostly off-topic", "1"),
        ("2 — mostly relevant", "2"),
        ("3 — directly addresses the question", "3"),
    ],
    "human_clarity": _RATING_CHOICES + [
        ("0 — unintelligible", "0"),
        ("1 — difficult to follow", "1"),
        ("2 — understandable", "2"),
        ("3 — clear and well organized", "3"),
    ],
}
_RESIZE_JS = """
async (_win, _event_data) => {
  if (window.__benchmarkGradeSplitsInit || window.__benchmarkGradeSplitsPending) return;
  window.__benchmarkGradeSplitsPending = true;

  function isColumn(element) {
    return element.classList.contains("column") ||
      element.classList.contains("col") ||
      element.classList.contains("gr-column");
  }

  function setup() {
    var row = document.getElementById("grading-columns");
    if (!row || row.__gradeSplittersReady) return false;
    function getColumns() {
      return Array.from(row.children).filter(isColumn);
    }
    var columns = getColumns();
    if (columns.length !== 3) return false;
    row.__gradeSplittersReady = true;
    window.__benchmarkGradeSplitsInit = true;
    window.__benchmarkGradeSplitsPending = false;

    var handles = [];
    for (var boundary = 0; boundary < 2; boundary++) {
      var handle = document.createElement("div");
      handle.className = "grading-splitter";
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", "Resize adjacent answer panes");
      handle.setAttribute("tabindex", "0");
      handle.setAttribute("data-boundary", String(boundary));
      handle.style.position = "absolute";
      handle.style.top = "0";
      handle.style.bottom = "0";
      handle.style.zIndex = "10";
      handle.style.flex = "0 0 12px";
      handle.style.width = "12px";
      handle.style.minWidth = "12px";
      handle.style.maxWidth = "12px";
      handle.style.marginLeft = "-6px";
      handle.style.boxSizing = "border-box";
      handle.style.cursor = "col-resize";
      handle.style.touchAction = "none";
      handle.style.display = "flex";
      handle.style.justifyContent = "center";
      row.appendChild(handle);
      handles.push(handle);
    }

    function labelEditGroundTruthButton() {
      var editGroundTruthButton = document.getElementById("edit-ground-truth-button");
      if (editGroundTruthButton) {
        editGroundTruthButton.setAttribute("aria-label", "Edit Ground Truth");
        editGroundTruthButton.setAttribute("title", "Edit Ground Truth");
      }
      var copyGeneratedAnswerButton = document.getElementById(
        "copy-generated-answer-button"
      );
      if (copyGeneratedAnswerButton) {
        var copyDescription =
          "Copies the original Markdown text displayed below to the clipboard. " +
          "You can paste it into the Ground Truth Answer editing window.";
        copyGeneratedAnswerButton.setAttribute("aria-label", copyDescription);
        copyGeneratedAnswerButton.setAttribute("title", copyDescription);
      }
    }
    labelEditGroundTruthButton();
    new MutationObserver(labelEditGroundTruthButton).observe(document.body, {
      childList: true,
      subtree: true
    });

    var widths = columns.map(function (column) {
      return column.getBoundingClientRect().width;
    });
    var storageKey = "benchmark-grading-column-widths-v2";
    try {
      var saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (Array.isArray(saved) && saved.length === 3 &&
          saved.every(function (value) { return typeof value === "number" && value > 0; })) {
        var widthTotal = widths.reduce(function (sum, value) { return sum + value; }, 0);
        var savedTotal = saved.reduce(function (sum, value) { return sum + value; }, 0);
        widths = saved.map(function (value) { return value * widthTotal / savedTotal; });
      }
    } catch (error) {}

    function availableWidth() {
      var style = getComputedStyle(row);
      var horizontalInsets =
        (parseFloat(style.paddingLeft) || 0) +
        (parseFloat(style.paddingRight) || 0) +
        (parseFloat(style.borderLeftWidth) || 0) +
        (parseFloat(style.borderRightWidth) || 0);
      var gap = parseFloat(style.columnGap) || 0;
      return Math.max(3 * 150, row.clientWidth - horizontalInsets - 2 * gap);
    }

    function updateHandles() {
      var currentColumns = getColumns();
      var rowRect = row.getBoundingClientRect();
      handles.forEach(function (handle, index) {
        var leftRect = currentColumns[index].getBoundingClientRect();
        var rightRect = currentColumns[index + 1].getBoundingClientRect();
        var boundaryX = (leftRect.right + rightRect.left) / 2;
        handle.style.left = (boundaryX - rowRect.left) + "px";
      });
    }

    function applyWidths() {
      getColumns().forEach(function (column, index) {
        column.style.flex = "0 0 " + widths[index] + "px";
        column.style.width = widths[index] + "px";
        column.style.maxWidth = widths[index] + "px";
      });
      updateHandles();
    }

    handles.forEach(function (handle, boundary) {
      handle.addEventListener("pointerdown", function (event) {
        event.preventDefault();
        event.stopPropagation();
        var startX = event.clientX;
        var startLeftWidth = widths[boundary];
        var startRightWidth = widths[boundary + 1];
        var minWidth = 150;
        handle.classList.add("active");
        document.body.classList.add("grading-resizing");
        handle.setPointerCapture(event.pointerId);

        function move(moveEvent) {
          var delta = moveEvent.clientX - startX;
          var leftWidth = Math.max(
            minWidth,
            Math.min(startLeftWidth + startRightWidth - minWidth, startLeftWidth + delta)
          );
          widths[boundary] = leftWidth;
          widths[boundary + 1] = startLeftWidth + startRightWidth - leftWidth;
          applyWidths();
        }

        function finish() {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", finish);
          if (handle.hasPointerCapture(event.pointerId)) {
            handle.releasePointerCapture(event.pointerId);
          }
          handle.classList.remove("active");
          document.body.classList.remove("grading-resizing");
          try { localStorage.setItem(storageKey, JSON.stringify(widths)); } catch (error) {}
        }

        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", finish);
        handle.addEventListener("pointercancel", finish, { once: true });
      });
      handle.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        event.stopPropagation();
        var direction = event.key === "ArrowLeft" ? -1 : 1;
        var pairTotal = widths[boundary] + widths[boundary + 1];
        widths[boundary] = Math.max(
          150,
          Math.min(pairTotal - 150, widths[boundary] + direction * 24)
        );
        widths[boundary + 1] = pairTotal - widths[boundary];
        applyWidths();
        try { localStorage.setItem(storageKey, JSON.stringify(widths)); } catch (error) {}
      });
    });

    applyWidths();
    window.addEventListener("resize", function () {
      var totalWidth = widths.reduce(function (sum, value) { return sum + value; }, 0);
      var targetWidth = availableWidth();
      widths = widths.map(function (value) { return value * targetWidth / totalWidth; });
      applyWidths();
    });
    return true;
  }

  var attempts = 0;
  (function poll() {
    if (setup()) return;
    if (++attempts >= 100) {
      window.__benchmarkGradeSplitsPending = false;
      return;
    }
    setTimeout(poll, 200);
  })();
}
"""

_BLOCK_CSS = """
        .gradio-container, .contain { max-width: 100% !important; }
        #grading-columns { position: relative !important; display: flex !important;
          flex-wrap: nowrap !important; gap: 12px !important; align-items: stretch !important; }
        #grading-columns > .column { min-width: 150px !important; overflow-wrap: anywhere; }
        .ground-truth-heading-row {
          align-items: center !important; flex-wrap: nowrap !important;
          justify-content: space-between !important; gap: 4px !important;
        }
        .generated-answer-heading-row {
          align-items: center !important; flex-wrap: nowrap !important;
          justify-content: space-between !important; gap: 4px !important;
        }
        .ground-truth-heading-row > div:first-child {
          flex: 1 1 0% !important; min-width: 0 !important; width: 0 !important;
        }
        .generated-answer-heading-row > div:first-child {
          flex: 1 1 0% !important; min-width: 0 !important; width: 0 !important;
        }
        #edit-ground-truth-button, #copy-generated-answer-button {
          flex: 0 0 32px !important; margin-left: auto !important;
          min-width: 32px !important; width: 32px !important; height: 32px !important;
          padding: 0 !important; border: 0 !important; background: transparent !important;
          color: #64748b !important; font-size: 20px !important;
        }
        #edit-ground-truth-button:hover, #edit-ground-truth-button:focus-visible,
        #copy-generated-answer-button:hover, #copy-generated-answer-button:focus-visible {
          background: #f1f5f9 !important; color: #2563eb !important;
        }
        .grading-splitter { position: absolute; top: 0; bottom: 0; z-index: 10;
          width: 12px; margin-left: -6px; cursor: col-resize; touch-action: none;
          display: flex; justify-content: center; }
        .grading-splitter::before { content: ""; width: 2px; height: 100%;
          background: #94a3b8; border-radius: 2px; }
        .grading-splitter:hover::before, .grading-splitter.active::before,
        .grading-splitter:focus-visible::before { width: 4px; background: #2563eb; }
        body.grading-resizing { cursor: col-resize !important; user-select: none !important; }
        @media (max-width: 760px) {
          #grading-columns { overflow-x: auto !important; }
        }
        """


def _display_answer(text: str) -> str:
    """Render the benchmark's default escaped answer newlines as Markdown lines."""
    return text.replace(r"\r\n", "\n").replace(r"\n", "\n").replace(r"\r", "\n")


def _grade_values(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(column, "") for column in HUMAN_GRADING_COLUMNS)


def _row_markdown(row: dict[str, str], index: int, total: int, graded_count: int) -> str:
    metadata = (
        f"**Result {index + 1} of {total}** · {graded_count} fully graded\n\n"
        f"**Run:** `{row['run_id']}` · **Query:** `{row['query_id']}` · "
        f"**Model:** {row['provider']}/{row['model']} (row {row['model_row']}) · "
        f"**Repetition:** {row['repetition']} · **Status:** `{row['status']}`"
    )
    if row.get("error"):
        metadata += f"\n\n**Benchmark error:** {row['error']}"
    return metadata


def _next_ungraded_index(rows: list[dict[str, str]], index: int) -> int | None:
    candidates = list(range(index + 1, len(rows))) + list(range(0, index))
    return next(
        (candidate for candidate in candidates if not is_human_graded(rows[candidate])),
        None,
    )


def _load_result_by_identity(
    path: Path,
    identity: tuple[str, ...],
) -> tuple[list[dict[str, str]], int]:
    rows = load_result_rows(path)
    matching_indices = [
        index for index, row in enumerate(rows) if result_identity(row) == identity
    ]
    if len(matching_indices) != 1:
        raise ResultsCsvError(
            "The selected result row is no longer present exactly once in the CSV."
        )
    return rows, matching_indices[0]


def build_ui(results_csv: str | Path) -> gr.Blocks:
    """Build the independent human-grading application."""
    path = Path(results_csv).expanduser().resolve()
    rows = load_result_rows(path)
    if not rows:
        raise ResultsCsvError(f"Results CSV {path} contains no result rows.")

    def render(index: int) -> tuple[Any, ...]:
        current_rows = load_result_rows(path)
        if not current_rows:
            raise ResultsCsvError(f"Results CSV {path} contains no result rows.")
        index = max(0, min(index, len(current_rows) - 1))
        row = current_rows[index]
        graded_count = sum(is_human_graded(item) for item in current_rows)
        return (
            row.get("question", "") or "_(empty)_",
            row.get("ground_truth", "") or "_(empty)_",
            row.get("ground_truth", ""),
            row.get("ground_truth", "") or "_(empty)_",
            _display_answer(row.get("generated_answer", "")) or "_(empty)_",
            _display_answer(row.get("generated_answer", "")),
            _row_markdown(row, index, len(current_rows), graded_count),
            *_grade_values(row),
            index,
            result_identity(row),
            f"Loaded result {index + 1} of {len(current_rows)}.",
        )

    initial = render(0)
    (
        initial_question,
        initial_truth,
        initial_ground_truth_source,
        initial_ground_truth_preview,
        initial_answer,
        initial_answer_source,
        initial_metadata,
        initial_correctness,
        initial_completeness,
        initial_relevance,
        initial_clarity,
        initial_comments,
        initial_index,
        initial_identity,
        initial_status,
    ) = initial

    def save_current(
        correctness: str,
        completeness: str,
        relevance: str,
        clarity: str,
        comments: str,
        index: int,
        identity: tuple[str, ...],
    ) -> tuple[str, str]:
        grade = {
            "human_correctness": correctness or "",
            "human_completeness": completeness or "",
            "human_relevance": relevance or "",
            "human_clarity": clarity or "",
            "human_comments": comments or "",
        }
        save_human_grade(path, identity, grade)
        current_rows, current_index = _load_result_by_identity(path, identity)
        graded_count = sum(is_human_graded(row) for row in current_rows)
        return (
            _row_markdown(current_rows[current_index], current_index, len(current_rows), graded_count),
            f"Saved grading for result {index + 1}.",
        )

    def begin_ground_truth_edit(
        identity: tuple[str, ...],
    ) -> tuple[Any, ...]:
        current_rows, current_index = _load_result_by_identity(path, identity)
        source = current_rows[current_index].get("ground_truth", "")
        return (
            gr.update(visible=False),
            source,
            source or "_(empty)_",
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            "Edit the raw Markdown source, then save or cancel before navigating.",
        )

    def save_ground_truth_edit(
        source: str,
        identity: tuple[str, ...],
    ) -> tuple[Any, ...]:
        updated_count = save_ground_truth(path, identity, source)
        current_rows, current_index = _load_result_by_identity(path, identity)
        return (
            gr.update(
                value=current_rows[current_index].get("ground_truth", "") or "_(empty)_",
                visible=True,
            ),
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True),
            f"Saved Ground Truth for this query in {updated_count} result row(s) "
            "in the current run.",
        )

    def cancel_ground_truth_edit(
        identity: tuple[str, ...],
    ) -> tuple[Any, ...]:
        current_rows, current_index = _load_result_by_identity(path, identity)
        source = current_rows[current_index].get("ground_truth", "")
        return (
            gr.update(value=source or "_(empty)_", visible=True),
            source,
            source or "_(empty)_",
            gr.update(visible=False),
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True),
            "Discarded unsaved Ground Truth edits.",
        )

    def preview_ground_truth(source: str) -> str:
        return source or "_(empty)_"

    def move_to(
        action: str,
        index: int,
        identity: tuple[str, ...],
        correctness: str,
        completeness: str,
        relevance: str,
        clarity: str,
        comments: str,
    ) -> tuple[Any, ...]:
        saved_message = save_current(
            correctness,
            completeness,
            relevance,
            clarity,
            comments,
            index,
            identity,
        )
        current_rows = load_result_rows(path)
        if not current_rows:
            raise ResultsCsvError(f"Results CSV {path} contains no result rows.")
        target = max(0, min(index, len(current_rows) - 1))
        if action == "previous":
            target = max(0, index - 1)
        elif action == "next":
            target = min(len(current_rows) - 1, index + 1)
        elif action == "next_ungraded":
            ungraded_index = _next_ungraded_index(current_rows, index)
            if ungraded_index is None:
                message = (
                    "All results are fully graded."
                    if is_human_graded(current_rows[index])
                    else "There are no other ungraded results."
                )
                return (*render(index)[:-1], message)
            target = ungraded_index
        rendered = render(target)
        return (*rendered[:-1], f"{saved_message} {rendered[-1]}")

    # Gradio 5.x accepts theme/css/js only in the Blocks constructor, but emits a
    # DeprecationWarning for each, pointing at Blocks.launch() — which only accepts
    # them from Gradio 6.0 onward (outside the pinned >=5,<6 range). Suppress the
    # resulting warnings locally; the parameters are fully functional on 5.x.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        demo = gr.Blocks(
            title="Benchmark Answer Grader",
            theme=gr.themes.Soft(
                primary_hue="slate",
                font=(
                    "Fira Code",
                    "ui-monospace",
                    "SFMono-Regular",
                    "Menlo",
                    "Consolas",
                    "monospace",
                ),
            ),
            css=_BLOCK_CSS,
            js=_RESIZE_JS,
            fill_width=True,
        )
    with demo:
        gr.Markdown("# Human grading for benchmark answers")
        gr.Textbox(
            label="Benchmark results CSV",
            value=str(path),
            interactive=False,
        )
        metadata = gr.Markdown(value=initial_metadata)
        index_state = gr.State(initial_index)
        identity_state = gr.State(initial_identity)
        generated_answer_source = gr.Textbox(
            value=initial_answer_source,
            visible=False,
            show_label=False,
        )

        with gr.Row(elem_id="grading-columns"):
            with gr.Column(scale=1, min_width=150):
                gr.Markdown("### Question")
                question_view = gr.Markdown(value=initial_question)
            with gr.Column(scale=1, min_width=150):
                with gr.Row(elem_classes="ground-truth-heading-row"):
                    gr.Markdown("### Ground Truth Answer")
                    edit_ground_truth_button = gr.Button(
                        "✎",
                        size="sm",
                        min_width=32,
                        elem_id="edit-ground-truth-button",
                    )
                truth_view = gr.Markdown(value=initial_truth)
                with gr.Column(visible=False) as ground_truth_editor:
                    gr.Markdown("Edit the raw Markdown source. The preview updates as you type.")
                    ground_truth_source = gr.Textbox(
                        label="Ground Truth Markdown source",
                        lines=10,
                        max_lines=24,
                        value=initial_ground_truth_source,
                    )
                    ground_truth_preview = gr.Markdown(
                        value=initial_ground_truth_preview,
                        label="Markdown preview",
                    )
                    with gr.Row():
                        save_ground_truth_button = gr.Button(
                            "Save Ground Truth",
                            variant="primary",
                            visible=False,
                        )
                        cancel_ground_truth_button = gr.Button(
                            "Cancel",
                            visible=False,
                        )
            with gr.Column(scale=1, min_width=150):
                with gr.Row(elem_classes="generated-answer-heading-row"):
                    gr.Markdown("### Generated Answer")
                    copy_generated_answer_button = gr.Button(
                        "📋",
                        size="sm",
                        min_width=32,
                        elem_id="copy-generated-answer-button",
                    )
                answer_view = gr.Markdown(value=initial_answer)
        with gr.Row():
            correctness = gr.Radio(
                _RATING_CHOICES_BY_FIELD["human_correctness"],
                label="Correctness",
                value=initial_correctness,
            )
            completeness = gr.Radio(
                _RATING_CHOICES_BY_FIELD["human_completeness"],
                label="Completeness",
                value=initial_completeness,
            )
            relevance = gr.Radio(
                _RATING_CHOICES_BY_FIELD["human_relevance"],
                label="Relevance",
                value=initial_relevance,
            )
            clarity = gr.Radio(
                _RATING_CHOICES_BY_FIELD["human_clarity"],
                label="Clarity",
                value=initial_clarity,
            )
        comments = gr.Textbox(
            label="Comments",
            lines=4,
            max_lines=12,
            value=initial_comments,
        )

        with gr.Row():
            previous = gr.Button("Previous")
            save_button = gr.Button("Save grading", variant="primary")
            next_button = gr.Button("Next")
            next_ungraded = gr.Button("Next ungraded")
        save_status = gr.Markdown(value=initial_status)

        edit_ground_truth_button.click(
            fn=begin_ground_truth_edit,
            inputs=[identity_state],
            outputs=[
                truth_view,
                ground_truth_source,
                ground_truth_preview,
                ground_truth_editor,
                edit_ground_truth_button,
                save_ground_truth_button,
                cancel_ground_truth_button,
                previous,
                next_button,
                next_ungraded,
                save_status,
            ],
        )
        ground_truth_source.input(
            fn=preview_ground_truth,
            inputs=[ground_truth_source],
            outputs=[ground_truth_preview],
        )
        copy_generated_answer_button.click(
            fn=lambda answer: (
                f"Copied {len(answer)} characters of original Markdown to the clipboard."
            ),
            inputs=[generated_answer_source],
            outputs=[save_status],
            js="""async (answer) => {
              await navigator.clipboard.writeText(answer);
              return answer;
            }""",
        )
        save_ground_truth_button.click(
            fn=save_ground_truth_edit,
            inputs=[ground_truth_source, identity_state],
            outputs=[
                truth_view,
                ground_truth_editor,
                edit_ground_truth_button,
                save_ground_truth_button,
                cancel_ground_truth_button,
                previous,
                next_button,
                next_ungraded,
                save_status,
            ],
        )
        cancel_ground_truth_button.click(
            fn=cancel_ground_truth_edit,
            inputs=[identity_state],
            outputs=[
                truth_view,
                ground_truth_source,
                ground_truth_preview,
                ground_truth_editor,
                edit_ground_truth_button,
                save_ground_truth_button,
                cancel_ground_truth_button,
                previous,
                next_button,
                next_ungraded,
                save_status,
            ],
        )

        controls = [correctness, completeness, relevance, clarity, comments]
        save_inputs = [*controls, index_state, identity_state]
        save_button.click(
            fn=save_current,
            inputs=save_inputs,
            outputs=[metadata, save_status],
        )
        for control in controls[:4]:
            control.input(fn=save_current, inputs=save_inputs, outputs=[metadata, save_status])
        comments.change(fn=save_current, inputs=save_inputs, outputs=[metadata, save_status])

        navigation_inputs = [index_state, identity_state, *controls]
        display_outputs = [
            question_view,
            truth_view,
            ground_truth_source,
            ground_truth_preview,
            answer_view,
            generated_answer_source,
            metadata,
            correctness,
            completeness,
            relevance,
            clarity,
            comments,
            index_state,
            identity_state,
            save_status,
        ]
        for button, action in (
            (previous, "previous"),
            (next_button, "next"),
            (next_ungraded, "next_ungraded"),
        ):
            button.click(
                fn=lambda index, identity, corr, comp, rel, clr, note, action=action: move_to(
                    action, index, identity, corr, comp, rel, clr, note
                ),
                inputs=navigation_inputs,
                outputs=display_outputs,
            )
        demo.load(fn=render, inputs=index_state, outputs=display_outputs)

    return demo


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review benchmark answers and save human grades to the results CSV."
    )
    parser.add_argument(
        "--results-csv",
        required=True,
        help="Benchmark results CSV to review and update.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7861,
        help="Local web UI port (default: 7861).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind to; use 0.0.0.0 only inside a container with a loopback-published host port.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = Path(args.results_csv).expanduser()
    if not path.is_file():
        print(f"Results CSV does not exist or is not a file: {path}", file=sys.stderr)
        return 2
    try:
        app = build_ui(path)
    except ResultsCsvError as exc:
        print(f"Could not load results CSV: {exc}", file=sys.stderr)
        return 2
    app.launch(
        server_name=args.host,
        server_port=args.port,
        share=False,
        inbrowser=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
