# Data Quality Violations Detector

Reads weekly healthcare billing data (funding reports + invoices in Excel),
detects data quality violations, and generates a standalone interactive HTML
report.

## What it detects

- **Duplicate batch violations** — the same `batch_id` appearing in more than
  one funding report file. The report shows the files, weeks, row counts, and
  an estimated overpaid amount (all payments beyond the batch's first
  submission).
- **Duplicate claim violations** — the same `claim_number` submitted under
  more than one `batch_id`, with the batches, occurrences, payments, and
  source files.
- **Weekly reconciliation** — per-week comparison of the summed funding-report
  payments against the invoice "Total Claims" figure, flagging any mismatch
  beyond one cent.
- **Row-level anomalies** (bonus) — missing identifiers, non-positive
  payments, and payments exceeding the billed charge.

The HTML report includes summary tiles, a weekly funding-vs-invoice chart
with hover tooltips, searchable/sortable/paginated tables, and a batch
explorer — click any batch id anywhere in the report to see its claims.
It is fully self-contained (inline CSS/JS, data embedded) and opens directly
in a browser with no server.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python main.py --input-path ./data --output-path ./output
```

Then open `output/report.html` in a browser.

Options:

| Flag | Default | Description |
|---|---|---|
| `--input-path` | `./data` | Folder containing the weekly `YYYYMMDD` subfolders |
| `--output-path` | `./output` | Folder where `report.html` is written |
| `-v` / `--verbose` | off | Debug logging |

## Project structure

```
main.py                       CLI entry point
dqvd/parsing.py               Excel readers (funding reports, fixed-layout invoices)
dqvd/violations.py            Polars detectors + weekly reconciliation
dqvd/report.py                Payload assembly + HTML rendering
dqvd/templates/report.html    Report template (inline CSS/JS)
output/report.html            Generated report
```

## Implementation notes

- **Polars** is used for all aggregation; **openpyxl** (read-only mode) for
  Excel I/O, including the fixed-cell invoice layout.
- The batch id is propagated down from the top of each funding report, per the
  spec (it is only guaranteed to appear once, in row 2).
- The invoice `NEGATIVE BATCH` variant is handled: when row 16 column A
  carries the marker, Total Amount Due is read from row 17 instead of row 16.
- Unrecognized `.xlsx` files are skipped with a warning rather than failing
  the run.
