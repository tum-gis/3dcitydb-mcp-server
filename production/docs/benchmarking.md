# LLM Benchmarking and Validation

> [!NOTE]
> **Work in progress.** The benchmarking and result-validation tools
> described in this document are **currently under development**. The
> commands, file formats, and workflows documented here are expected to keep
> changing: **this document and the tools it describe will likely be modified
> in the future.**

## Overview

LLM benchmarking and validation is about systematically measuring how well the
ChatBot answers questions about the 3DCityDB contents. The general idea is to
drive the very same ChatBot that end users use with a known set of questions and
compare what it generates against reference answers that have been created and
verified by a human. The tooling supports two complementary kinds of input:

- **Whole chat sessions.** By using the optional `group_number` /
  `sequence_number` columns, a series of related questions can be grouped into a
  single multi-turn conversation that is sent to the model in order. This lets
  you simulate an entire chat session rather than isolated one-shot questions.
  This is useful, because the chatbot has a function that summarizes how it has
  solved the task of data processing in order to answer a user query and that 
  knowledge is re-used in subsequent turns. 
- **Standalone queries with ground-truth answers.** Each question can carry a
  human-written (or human-verified) ground-truth answer. The ChatBot's generated
  answer is then compared and graded against that reference.

It is important to note that a simple correct/incorrect label is not sufficient
to judge answer quality. An answer may be partly right, well structured but
incomplete, or correct but poorly focused. For this reason, we do not ask for a
single pass/fail verdict. Instead, a human grades each answer separately along
**five categories**, each scored independently from `0` to `3`:

- **Correctness** — how accurately the answer reflects the facts in the database
  and matches the ground-truth content.
- **Completeness** — how much of the information the question asks for is
  actually present in the answer.
- **Relevance** — how directly the answer addresses the question without drifting
  into unrelated material.
- **Clarity** — how clearly and understandably the answer is expressed and
  structured.
- **Language conformity to the query** — whether the answer is written in the
  same language as the user query and whether the database content it lists is
  translated into that language. Most LLMs are multilingual, but they sometimes
  fail to answer in the language the user used. In addition, CityGML datasets
  come from many different countries, so attribute values are localized per
  dataset: in an English dataset, residential buildings carry
  `function=residential`, while the same building in a Japanese or German
  dataset carries the localized value. If a user asks for a list of buildings
  together with their usage types, the answer should not only list the foreign
  attribute values, but also translate them into the language of the query.
  Scale: `0` — the answer is completely in a different language than the query;
  `1` — the answer is mostly in a different language; `2` — the answer is in the
  same language as the query, but attributes and data items were not translated;
  `3` — everything is provided in the correct translation according to the user
  query.

Running these evaluations is intended to be fully automated across many models.
You can run all queries for all test datasets against different LLMs and
configurations in an automated way: users provide the list of LLMs, their
operating modes, and further configuration options in the `models.csv` file.

When you use the CLI benchmarking tool, it runs — for each model and each
selected dataset — all queries intended for that dataset, in the defined order,
and possibly repeats each query `n` times to expose run-to-run variability. Every
answer is recorded in the `benchmark-results.csv` file. That file is then used to
evaluate the quality of the generated answers. The first step, which is currently
implemented, is **grading by a human** using the standalone grading tool. In the
future, we want to add **LLM-based automated grading** of the answers by
comparing each ground-truth answer against the individually generated answers,
reducing the need for manual grading.

## CSV-driven LLM benchmark

Run model configurations against the query rows for one dataset label. The
results CSV is appended and flushed after every answer:

The repository includes editable starter files in
[`examples/benchmark/`](../../examples/benchmark/). The Docker Compose files mount the
host folder `production/data` at `/app/data` in the chatbot container. From
PowerShell while your current directory is `examples\benchmark`, copy the
samples into that shared folder:

```powershell
Copy-Item .\models.csv ..\..\production\data\models.csv
Copy-Item .\questions.csv ..\..\production\data\questions.csv
```

Then run the benchmark in the chatbot container:

```bash
docker exec production-citydb-agent-1 3dcitydb-chat benchmark \
  --models-csv /app/data/models.csv \
  --queries-csv /app/data/questions.csv \
  --dataset demo \
  --repetitions 3 \
  --output /app/data/benchmark-results.csv \
  --temperature 0.2 --thinking off --prompt-mode auto
```

For a local source checkout, run `python -m webui.cli benchmark` from the
`production` directory and pass host paths for the input and output files. The
example model row uses `gpt-4.1-mini`; replace it with a model name available
from your configured provider/endpoint. The `demo` dataset value is only a
label used to select rows in the questions CSV; it does not need to match or
select database contents.

The models file requires `provider,model`; optional columns are
`base_url,temperature,thinking_level,prompt_mode`. When a corresponding CLI
option is provided, it overrides the value in the models file. Otherwise,
nonblank model-row settings are used, falling back to the CLI defaults
(temperature unset, thinking off, and prompt mode auto). Providers are
`openai`, `anthropic`, and `ollama`. Credentials remain in the environment and
must not be put in CSV files.

The questions file requires
`id,question,ground_truth,citygml_dataset`. Optional columns are
`category,group_number,sequence_number`. `--dataset` matches
`citygml_dataset` exactly; the label selects question rows only and does not
switch or filter the connected database. A nonblank `group_number` starts a
multi-turn conversation whose rows are sent in ascending `sequence_number`.
Each standalone query and each group repetition starts with fresh chat history.
The files use standard comma-delimited CSV: put double quotes around any field
containing a comma, and write a literal double quote inside a quoted field as
two double quotes (`""`). Semicolons are ordinary characters with this comma
delimiter, so they do not split a field. The sample questions demonstrate
quoted commas and semicolons; use a CSV-aware editor rather than splitting
question rows manually on punctuation. In both `models.csv` and
`questions.csv`, lines whose first cell starts with `#` are treated as
comments and not processed, but they still count toward the line numbers
referenced in error messages and progress output.

Use `--env-file /path/to/db.env` to override the benchmark's `CITYDB_*`
connection settings (also accepts `POSTGRES_*` keys and `DATABASE_URL`).
Results include input file provenance, query/model identity, generated answer,
status/error, latency, and context metrics. Failed calls are recorded as
failures and do not stop later cases. `--quiet-stderr` suppresses progress,
INFO logs, and backend diagnostics without suppressing the results CSV or
stdout summary. For each turn, stdout shows the model and its `models.csv` line,
group/query position, repetition, user query, and generated answer. Answer
line breaks are written as literal `\n`
sequences so each result remains on one physical CSV line for compatibility
with older spreadsheet applications. Pass `--no-escape-answer-newlines` to
preserve real line breaks in the results CSV directly. The CLI prints a blank
line after each answer and two additional blank lines after each model row.

Line-break normalization: the grading app automatically converts legacy
escaped line breaks (literal `\n` two-character sequences) in the text
columns — `question`, `ground_truth`, `generated_answer`, and
`human_comments` — into real line breaks every time the results file is
loaded. The conversion is applied as soon as the file is opened, so a file
containing a mix of escaped and real line breaks is unified on first load and
stays unified afterwards: any save (rating, comment, or ground-truth edit)
persists the normalized form. Columns that are not free text (for example
`base_url`, `query_file`) are never modified, so values that legitimately
contain backslash sequences — such as Windows paths — are left untouched.

Results CSVs are written as UTF-8 with a BOM and CRLF line endings, and any
multi-line cell (such as a ground-truth answer containing Markdown) is wrapped
in double quotes. This keeps the file importable by MS Excel and other
spreadsheet tools, while the grading app renders the stored line breaks as
normal line breaks.

## Human grading of benchmark answers

After a benchmark run, open its results CSV in the standalone local grading
application. From the `production` directory in a source checkout, run:

```powershell
python -m webui.grade --results-csv .\data\benchmark-results.csv
```

The app opens in your browser and binds only to `127.0.0.1` (default port
`7861`). It does not start the chatbot or connect to the database. Install the
dependencies from `production/webui/requirements.txt` in the local Python
environment if they are not already available. Use `--port` to select another
local port.

You can also run the grader inside the chatbot container, reusing its installed
Python environment and Gradio dependencies. The full-stack Compose file
publishes port `7861` on the host loopback interface only. From the repository
root, rebuild and start the agent service after updating the image:

```powershell
docker compose -f production\docker-compose.fullstack.yml up -d --build citydb-agent
docker exec -it production-citydb-agent-1 python -m webui.grade `
  --results-csv /app/data/benchmark-results.csv `
  --host 0.0.0.0
```

The `0.0.0.0` bind is inside the container; Compose limits host access to
`127.0.0.1:7861`. Open `http://127.0.0.1:7861` in the host browser. The
foreground grader process can be stopped with `Ctrl+C`; this leaves the
chatbot service running.

Each benchmark result is shown with the question, ground-truth answer, and
generated answer in three Markdown-rendered columns. Drag either vertical
separator to resize adjacent columns; the chosen widths are retained in that
browser. The displayed metadata identifies the run, query, model row, and
repetition. Use **Previous**, **Next**, and **Next ungraded** to navigate.

The Ground Truth column remains Markdown-rendered by default. Select the pen
icon beside its heading to edit the raw Markdown source and see a live preview,
then choose **Save Ground Truth** or **Cancel** before navigating. Saving
updates the `ground_truth` value for all model and repetition rows for that
query in the current run, while preserving other runs and existing human
grades.

Use the clipboard icon beside **Generated Answer** to copy its original
Markdown source, which can then be pasted into the Ground Truth editor. This is
useful for bootstrapping the creation of ground-truth answers: start with
empty `ground_truth` values in `questions.csv` and run all queries against a
reliable LLM that almost always answers correctly. Then open
`benchmark-results.csv`, copy the generated answers into the ground-truth
column, and edit them manually to strip unnecessary text and explanations.
Finally, use a text editor or a spreadsheet tool such as Excel to copy the
ground-truth answers back into the original `questions.csv` file.

Rate correctness, completeness, relevance, clarity, and language conformity
separately from `0` to `3`; the controls describe the scale for each dimension.
All five ratings are required for a result to count as fully graded. Comments are optional. Rating
changes and comments are saved to additional `human_*` columns in the same
results CSV, and **Save grading** is also available. Reopen the app with the
same CSV path to continue; saved grades are loaded from the file. Failed
benchmark rows remain visible with their status. Complete benchmark runs
before grading a file; do not edit the same CSV concurrently.

The benchmark runner accepts both older result files and files with human
grading columns. If it appends new rows to a graded file, it preserves existing
grades and leaves the new rows' grading fields blank.