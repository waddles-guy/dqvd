# Code Walkthrough — Data Quality Violations Detector (dqvd)

This document walks through the whole project, starting from `main.py` and following
the data all the way to the interactive HTML report and the tests. It assumes you
know Python but have never used **Polars**, so every Polars concept used in the code
is explained where it first appears.

---

## 1. The big picture

The program reads a folder of weekly billing data, looks for data-quality problems,
and writes a single self-contained HTML report.

```
data/                          (input)
 ├── 20250106/                 one folder per week, named YYYYMMDD
 │    ├── Funding Report 20250106-1.xlsx     claim-level payment data
 │    ├── Funding Report 20250106-2.xlsx
 │    └── BS Weekly Bill.xlsx                one invoice summary per week
 ├── 20250113/
 │    └── ...
 └── ...

            │
            ▼
   dqvd/parsing.py      reads every Excel file → two Polars DataFrames
            │                 claims    (one row per claim line)
            │                 invoices  (one row per weekly invoice)
            ▼
   dqvd/violations.py   five aggregation functions that find the problems
            │
            ▼
   dqvd/report.py       converts results to one big JSON dict ("payload")
            │           and injects it into the HTML template
            ▼
   dqvd/templates/report.html   template with inline CSS + JS
            │
            ▼
   output/report.html   (output) open it in a browser — no server needed
```

Project layout:

| Path | Role |
|---|---|
| `main.py` | CLI entry point |
| `dqvd/parsing.py` | Excel → DataFrames |
| `dqvd/violations.py` | the actual business logic (aggregations) |
| `dqvd/report.py` | DataFrames → JSON payload → HTML file |
| `dqvd/templates/report.html` | the report UI (HTML + CSS + JS in one file) |
| `tests/test_parsing.py` | tests for the Excel readers |
| `tests/test_violations.py` | tests for the detectors |
| `tests/test_report.py` | tests for payload building / rounding |
| `tests/test_report_template.mjs` | Node tests for the JS inside the template |

---

## 2. `main.py` — the entry point

```python
python main.py --input-path ./data --output-path ./output
```

`main()` does four things, in order:

1. **Parses CLI arguments** with `argparse`. Both arguments have defaults
   (`data` and `output`), so plain `python main.py` works too.
2. **Configures logging** — `INFO` normally, `DEBUG` with `-v`. All modules log
   through the standard `logging` module, so this one call controls everything.
3. **Validates the input folder** exists; returns exit code `1` if not.
4. **Runs the pipeline**: `load_directory()` → `render_report()`, then logs where
   the report was written and returns `0`.

That's it — `main.py` contains no logic of its own; it just wires the pieces.

---

## 3. A short Polars primer (only what this code uses)

Polars is a DataFrame library like pandas, but faster and with a different API
style. A **DataFrame** is a table: named columns, each with one data type, and
rows. The key ideas used in this codebase:

### 3.1 Creating DataFrames and schemas

```python
pl.DataFrame(records, schema=CLAIM_SCHEMA)
```

`records` is a list of dicts (one per row). `schema` is a dict mapping column
names to Polars **dtypes** — e.g. `pl.Utf8` (string), `pl.Float64`, `pl.Date`,
`pl.Boolean`. Passing an explicit schema matters for two reasons:

- If `records` is **empty**, Polars still creates a valid 0-row table with the
  right columns and types (so downstream code never crashes on "no data").
- If a column happens to contain only `None`s in one file, the schema stops
  Polars from guessing a wrong type.

Missing values are **null** (Polars' equivalent of `None`/`NaN`), and most
operations propagate them: `null - 5` is `null`, `null > 3` is `null`.

### 3.2 Expressions: `pl.col(...)`

The heart of Polars. `pl.col("week")` does not fetch data — it builds an
**expression**, a description of a computation ("the column named week").
Expressions are combined and then handed to a DataFrame method that executes
them:

```python
claims.filter(pl.col("anomaly").is_not_null())      # keep rows where expr is true
df.sort("week")                                     # sort rows
df.with_columns((pl.col("a") - pl.col("b")).alias("delta"))   # add a column
```

`.alias("name")` names the resulting column. `pl.lit("text")` is a literal
value as an expression. `pl.len()` counts rows (within a group).

### 3.3 Group-by and aggregation

```python
claims.group_by("batch_id").agg(
    pl.len().alias("rows"),
    pl.col("total_payment_amount").sum().alias("total_payment"),
    pl.col("source_file").n_unique().alias("n_files"),
    pl.col("week").unique().sort().alias("weeks"),
)
```

This is like SQL `GROUP BY batch_id`. Inside `.agg(...)` every expression is
evaluated **per group**:

- `pl.len()` → number of rows in the group
- `.sum()`, `.first()`, `.n_unique()` → what they sound like
- `pl.col("week")` *without* an aggregation → collects the group's values into
  a **list column** (a cell whose value is a list, e.g. `["20250106", "20250113"]`)
- `.unique().sort()` → a sorted, de-duplicated list column

The output has one row per group. **Important:** group order is random unless
you `.sort(...)` afterwards — which is why every function in `violations.py`
ends with a sort.

### 3.4 Joins

```python
funding_weekly.join(invoices_selected, on="week", how="full", coalesce=True)
```

A **full (outer) join**: keeps every week that appears on *either* side. Weeks
present only in claims get nulls for the invoice columns and vice versa.
`coalesce=True` merges the two `week` key columns into one (otherwise you'd get
`week` and `week_right`).

### 3.5 Conditionals: `when / then / otherwise`

```python
pl.when(cond1).then(value1).when(cond2).then(value2).otherwise(default)
```

A vectorized if/elif/else evaluated row by row. First matching branch wins —
so the **order of the branches is a precedence rule** (this matters in
`detect_line_anomalies`, see §5.4).

### 3.6 Getting data out

- `df.height` → number of rows (an int)
- `df.iter_rows(named=True)` → iterate rows as plain Python dicts
- `df.row(0, named=True)` → one row as a dict
- `df["col"].to_list()` → one column as a Python list
- `pl.concat([df1, df2, ...])` → stack DataFrames vertically (same schema)

That is genuinely all the Polars you need for this codebase.

---

## 4. `dqvd/parsing.py` — reading the Excel files

Two very different file formats live in each week folder, matched by filename:

```python
FUNDING_PATTERN = re.compile(r"Funding Report (\d{8})-(\d+)\.xlsx$", re.IGNORECASE)
INVOICE_PATTERN = re.compile(r"^BS .*Bill\.xlsx$", re.IGNORECASE)
```

- `Funding Report 20250106-1.xlsx` → tabular claim data (`.search`, so a prefix
  in the name wouldn't matter; case-insensitive).
- `BS <anything>Bill.xlsx` → invoice (`.match`, anchored at the start).

### 4.1 The coercion helpers

Excel cells are untyped chaos — a "date" cell might hold a `datetime`, a string,
or junk. Three tiny functions normalize every cell before it enters a DataFrame:

- `_as_date(value)` — accepts `datetime` (takes `.date()`), `date`, or an ISO
  string like `"2025-01-06"` (whitespace tolerated). Anything else → `None`.
  Note `datetime` must be checked **before** `date`, because in Python
  `datetime` is a subclass of `date`.
- `_as_float(value)` — accepts `int`/`float` only. A string like `"5.5"`
  deliberately becomes `None` — we don't guess at malformed numbers.
- `_as_str(value)` — `str()` + `.strip()`; empty or whitespace-only becomes
  `None`. That last part is load-bearing for batch-id propagation below.

### 4.2 `read_funding_report(path, week)`

Opens the workbook with `openpyxl.load_workbook(read_only=True, data_only=True)`:

- `read_only=True` — streaming mode, low memory.
- `data_only=True` — if a cell contains a formula, read its last computed
  *value*, not the formula text.

Steps:

1. Read the first row as the **header** and build `col`, a name → column-index
   map. Header names are stripped of whitespace. Because of this map, columns
   are found **by name, not by position** — a file with reordered columns still
   parses correctly, and a missing column simply yields nulls.
2. The little `cell(row, name)` closure safely fetches a value: returns `None`
   if the column doesn't exist in this file or the row is short.
3. Iterate the remaining rows:
   - Rows where *every* cell is `None` are skipped (Excel files often contain
     trailing blank rows).
   - **Batch-id propagation** — the quirk this reader exists for:

     ```python
     batch_id = _as_str(cell(row, "batch_id")) or batch_id
     ```

     In the source files the batch id is only guaranteed to appear on the
     first data row; the rest of the rows leave it blank. This line says: "if
     this row has a (non-blank) batch id, use it and remember it; otherwise
     keep using the last one seen." Because `_as_str` turns `"  "` into
     `None`, whitespace-only cells also inherit the previous id. If a *new*
     batch id appears mid-file, propagation switches to it from that row on.
4. Every record is stamped with the `week` (taken from the **folder** name, not
   the file) and `source_file` (the file's name) — these two columns are what
   later lets the detectors say *where* a violation came from.
5. Return `pl.DataFrame(records, schema=CLAIM_SCHEMA)` — explicit schema, so
   even an empty file produces a typed, 0-row frame.

### 4.3 `read_invoice(path, week)`

Invoices are not tables — the numbers live at **fixed cell positions**:

| Cell | Field |
|---|---|
| F3 | invoice date |
| G3 | invoice number |
| F12 | carrier claims |
| F13 | TPA claims |
| G14 | total claims |
| G15 | carrier fees |
| G16 | **total amount due** (normally) |
| A16 | may contain the text "NEGATIVE BATCH" |
| G17 | total amount due **when** A16 says NEGATIVE BATCH |

The one piece of logic:

```python
negative_batch = "NEGATIVE BATCH" in str(ws.cell(row=16, column=1).value or "").upper()
total_due_row = 17 if negative_batch else 16
```

When a week has a negative batch, the invoice layout inserts an extra line, so
"total amount due" shifts down one row. The check is a case-insensitive
*substring* match (so `"note: negative batch"` also triggers it). The output is
always exactly **one row** per invoice file, again with an explicit schema.

### 4.4 `load_directory(input_path)`

The orchestrator:

1. Find all subdirectories that are valid week folders, sorted. A folder
   qualifies only if its name is all digits **and** passes
   `_is_valid_week_dir`: exactly 8 characters, parseable as a real calendar
   date via `strptime("%Y%m%d")` (so month 13, Feb 30, or day 0 are rejected),
   not in the future, and not before `EARLIEST_WEEK` (2015-01-01 — adjust the
   constant if older archives ever become legitimate). All-digit names that
   fail any check are skipped **with a logged warning** (a typo'd folder gets
   noticed), while non-digit folders like `archive/` are silently ignored as
   before. If no valid week folders remain → `FileNotFoundError` (fail loudly
   on a wrong path rather than producing an empty report).
2. For every file in every week folder (sorted, so runs are deterministic):
   funding pattern → `read_funding_report`; invoice pattern → `read_invoice`;
   any *other* `.xlsx` → a logged warning ("Unrecognized xlsx file skipped"),
   so a typo'd filename doesn't silently drop data.
3. `pl.concat(...)` stacks all the per-file frames into one big `claims` frame
   and one `invoices` frame. If a list is empty, an empty frame with the right
   schema is returned instead, so the rest of the pipeline never special-cases
   "no data".

---

## 5. `dqvd/violations.py` — the detectors (the core logic)

One module-level constant:

```python
RECONCILE_TOLERANCE = 0.01   # totals within one cent are considered reconciled
```

### 5.1 `detect_duplicate_batches(claims)`

**Question it answers:** which batches were submitted (and paid) in more than
one funding report file?

Two-stage aggregation:

```python
per_file = claims.group_by("batch_id", "source_file", "week").agg(
    pl.len().alias("rows"),
    pl.col("total_payment_amount").sum().alias("payment"),
).sort("week", "source_file")
```

Stage 1 collapses the claim rows to **one row per (batch, file)** with that
file's row count and payment total. The `.sort("week", "source_file")` here is
not cosmetic — it defines what "first" means in stage 2.

```python
per_file.group_by("batch_id").agg(
    pl.col("source_file").alias("files"),          # list of files, in week order
    pl.col("week").alias("weeks"),
    pl.col("source_file").n_unique().alias("n_files"),
    pl.col("rows").sum().alias("total_rows"),
    pl.col("payment").sum().alias("total_payment"),
    (pl.col("payment").sum() - pl.col("payment").first()).alias("overpaid"),
).filter(pl.col("n_files") > 1).sort("overpaid", descending=True)
```

Stage 2 groups those per-file rows by batch. The interesting line is
**overpaid**: `sum of all payments − the first file's payment`. Because stage 1
sorted by week, `.first()` is the *earliest-week* submission — treated as the
legitimate one — and everything paid after it is the estimated duplicate
payment. `filter(n_files > 1)` keeps only real duplicates (a batch with many
rows in a *single* file is normal), and the result is sorted by money at risk.

### 5.2 `detect_duplicate_claims(claims)`

**Question:** which individual claim numbers appear under more than one batch id?

A single group-by on `claim_number`, collecting: how many distinct batches
(`n_batches`), the sorted list of batch ids, total payment across all
occurrences, the files and weeks involved, and the raw occurrence count. Then
`filter(n_batches > 1)` — note this means a claim repeated *within the same
batch* is **not** flagged here (that's a resubmission of the batch, caught by
5.1). Sorted by total payment, biggest first.

### 5.3 `weekly_reconciliation(claims, invoices)`

**Question:** for each week, does the sum of funding-report payments equal the
invoice's "Total Claims" figure?

Step 1 — aggregate claims per week:

```python
funding_weekly = claims.group_by("week").agg(
    pl.col("total_payment_amount").sum().alias("funding_total"),
    pl.col("gross_billable_charge").sum().alias("billed_total"),
    pl.col("batch_id").n_unique().alias("batches"),
    pl.len().alias("claim_rows"),
    pl.col("source_file").n_unique().alias("funding_files"),
)
```

Step 2 — **full outer join** with the invoices on `week`. Full, because both
failure directions matter: a week with claims but no invoice, and a week with
an invoice but no claims. Either way the missing side's columns become null.

Step 3 — compute the verdict:

```python
.with_columns(
    (pl.col("funding_total") - pl.col("total_claims")).alias("delta"),
    ((pl.col("funding_total") - pl.col("total_claims")).abs() > RECONCILE_TOLERANCE)
        .fill_null(True)
        .alias("mismatch"),
)
```

- `delta` = funding − invoice. Positive delta ⇒ funding paid **more** than
  invoiced; negative ⇒ less. If either side is null, `delta` is null.
- `mismatch` = |delta| **strictly greater than** one cent. So a difference of
  exactly $0.01 still reconciles — the tolerance absorbs float rounding noise.
- `.fill_null(True)` is subtle and important: when delta is null (missing
  invoice or missing claims), the comparison is null too, and null would be
  falsy — the week would look fine. `fill_null(True)` says *a week where we
  can't even compare is by definition a mismatch*.

Sorted by week for the report's chart and table.

### 5.4 `detect_line_anomalies(claims)`

Row-level sanity checks via a `when/then` chain — remember, first match wins,
so this is a **precedence ladder**:

1. `batch_id` or `claim_number` is null → `"missing identifier"`
2. else `total_payment_amount <= 0` → `"non-positive payment"`
3. else `total_payment_amount > gross_billable_charge` → `"payment exceeds billed charge"`
4. else → null (clean)

`.filter(anomaly.is_not_null())` keeps only the flagged rows — so the output is
the original claim rows plus an `anomaly` label column. A row with several
problems gets only its highest-precedence label.

### 5.5 `batch_summaries(claims)`

Not a violation detector — it feeds the report's "Batch explorer": one row per
batch with its unique sorted weeks and files, row count, and payment total,
sorted by batch id.

---

## 6. `dqvd/report.py` — from DataFrames to a payload

### 6.1 `_round`

```python
def _round(v: float | None, digits: int = 2) -> float | None:
    # adding 0.0 normalizes -0.0 to 0.0 so zero deltas never render with a minus sign
    return round(v, digits) + 0.0 if v is not None else None
```

Why the weird `+ 0.0`? IEEE-754 floats have a **negative zero**. Weekly totals
are sums of many floats, so a week that *should* balance often produces a delta
like `-1e-9` (classic `0.1 + 0.2 ≠ 0.3` noise). `round(-1e-9, 2)` gives `-0.0`,
which serializes to JSON as `-0.0` and was the reason the report showed
**“-$0.00”** in the Delta column. In IEEE arithmetic `-0.0 + 0.0 == +0.0`, so
the addition flips the sign bit without changing any nonzero value. (`None` is
passed through so missing values stay `null` in JSON.)

### 6.2 `build_payload(claims, invoices)`

Runs all five detectors, then converts everything into one plain dict of
JSON-friendly lists (via `iter_rows(named=True)`), with all money passed
through `_round`. Sections of the payload:

- `summary` — the headline tile numbers (violation counts, mismatched weeks,
  totals, estimated overpaid).
- `duplicate_batches`, `duplicate_claims`, `weekly`, `anomalies`, `batches` —
  one list of row-dicts per report table. In `weekly`, the DataFrame column
  `total_claims` is exposed as `invoice_total`, and `mismatch` is cast with
  `bool(...)` (JSON has no Polars booleans).
- `claims_by_batch` — a dict `batch_id → list of claim rows`, used by the
  click-a-batch modal. To keep the HTML small, each claim is a **positional
  array** (`[claim_number, service_date, billed, paid, plan, provider, week,
  file]`) rather than a dict — the JS knows the positions (§7.6).

### 6.3 `render_report(claims, invoices, output_path)`

The whole "templating engine" is one `str.replace`:

```python
html = template.replace("/*__DATA__*/", "const DATA = " + json.dumps(payload, ...) + ";")
```

The template contains a literal `/*__DATA__*/` placeholder inside its
`<script>` block; it is replaced by the JSON payload assigned to a global
`DATA`. The template is loaded with `importlib.resources` (works even if the
package is zipped/installed), and the result is written to
`output/report.html`. No Jinja, no server — the report is one file you can
email or open from disk.

---

## 7. `dqvd/templates/report.html` — the report UI

One HTML file with three parts: CSS in `<style>`, the page skeleton, and all
the behavior in one `<script>`.

### 7.1 CSS notes (brief)

Colors are defined as CSS custom properties on `:root` (`--ink`, `--critical`,
`--s1`/`--s2` for the two chart series...), with a `@media (prefers-color-scheme:
dark)` block that redefines them — that's the whole dark-mode implementation.
`font-variant-numeric: tabular-nums` makes digits fixed-width so money columns
line up. Rows of mismatched weeks get a red wash via `tr.mismatch td`.

### 7.2 The skeleton

Header + subtitle, a tile grid (`#tiles`), a chart section, five table sections
(`#sec-dup-batches`, `#sec-dup-claims`, `#sec-weekly`, `#sec-anomalies`,
`#sec-batches`) that start **empty** — JS fills them — and a `<dialog>` element
(`#batch-modal`) for the batch drill-down.

### 7.3 Formatters (top of the script)

- `fmtMoney(v)`:

  ```js
  if (v == null) return "—";
  if (v > -0.005 && v <= 0) v = 0;   // values that would render as "-$0.00" show as $0.00
  return v.toLocaleString("en-US", {style: "currency", currency: "USD"});
  ```

  This is the display-side half of the negative-zero fix. `toLocaleString`
  formats to 2 decimals with half-away-from-zero rounding, so any value in the
  interval `(-0.005, 0]` — including `-0` itself — would have rendered as
  **“-$0.00”**. Those are snapped to `0` first. The bound is deliberately
  exclusive: `-0.005` legitimately rounds to `-$0.01` and keeps its sign. So
  even if some future data path bypasses Python's `_round`, the UI can never
  show a signed zero.

- `fmtInt(v)` — thousands separators, `—` for null.
- `fmtWeek(w)` — `"20250106"` → `"2025-01-06"` by string slicing.
- `searchText(v)` — builds the text a table search matches against:

  ```js
  const s = String(Array.isArray(v) ? v.join(" ") : v).toLowerCase();
  return s + " " + s.replace(/(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)/g, "$1-$2-$3");
  ```

  Array cells (e.g. a week list) are joined with spaces first. Then the raw
  lowercase text is concatenated with a copy where every standalone 8-digit
  run is rewritten as `yyyy-mm-dd`. Because *both* forms are in the searched
  string, typing `20250106` **or** `2025-01-06` (or partials like `2025-01`,
  `01-06`) finds the row. The regex details:
  - `(?<!\d)` / `(?!\d)` are look-arounds: the 8 digits must not be part of a
    longer digit run — so a 9-digit id like `123456789` is *not* mangled into
    a fake date.
  - Look-arounds match digits only, not word boundaries (`\b`), on purpose:
    `funding_20250106.xlsx` has `_` (a word character) before the digits, so
    `\b` would fail there — this exact bug was caught by the tests.

- `esc(s)` — HTML-escapes `& < > " '` before any data value is placed into
  `innerHTML` (defense against a claim number containing markup).

### 7.4 Summary tiles

Reads `DATA.summary`, fills the subtitle and footer, and renders seven tiles.
Each "problem" tile is classed `bad` (red) when its count is nonzero, `ok`
(green) when zero.

### 7.5 `makeTable(host, rows, cols, opts)` — the generic table widget

Every table in the report (all five sections *and* the modal) is one call to
this function. Arguments:

- `host` — the section element to append into
- `rows` — array of row objects from `DATA`
- `cols` — column specs: `{key, label, num?, wrap?, render?}` where `render`
  is an optional value→HTML formatter (e.g. `fmtMoney`) and `num` right-aligns
- `opts` — initial `sortKey`/`sortDir`, `pageSize` (default 15), and an
  optional `rowClass` callback (used to paint mismatch rows red)

Internally it keeps a tiny **state object**: `{q, sortKey, sortDir, page}` —
the search query, current sort, and current page. Every user interaction
mutates state and calls `render()`, which redraws the whole table from scratch
(simple and fast enough at these row counts).

- `filtered()` — computes what to show: first the search filter
  (`searchText(value).includes(state.q)` across all columns — the query is
  lowercased when stored, values are lowercased inside `searchText`), then the
  sort. Sorting compares raw values; array values compare by length; nulls
  always sink to the bottom regardless of direction.
- `render()` — rebuilds the header (click a header to sort by it; clicking the
  same header flips direction — the ▲/▼ arrow shows the current sort), slices
  the current page out of the filtered rows, and rebuilds the body. Each
  cell is `render(value, row)` if the column has a renderer, else escaped
  text. Finally updates the row count, "Page x of y", and disables
  Prev/Next at the edges. Searching resets to page 0.

The five `makeTable` calls just below configure each section — for example the
weekly table's Delta column paints the value red when the row is a mismatch,
and its Status column renders the reconciled/mismatch pill.

### 7.6 The batch modal

Any batch id in any table is rendered as a `.batch-link` button. One
**delegated** click listener on `document` catches clicks on any of them
(including ones created after page load) and calls `openBatch(id)`, which:

1. Looks up `DATA.claims_by_batch[id]` — the positional arrays from §6.2 — and
   maps them back to named objects (`{claim, date, billed, paid, ...}`).
2. Fills the modal title/meta (row count and summed payment) and builds — again
   with `makeTable` — a claims table inside the modal.
3. Opens the native `<dialog>` with `showModal()`. Clicking the backdrop or the
   ✕ closes it.

### 7.7 The chart

A hand-rolled SVG line chart (no library), inside an IIFE:

- Uses `DATA.weekly` rows that have at least one total. `x(i)` spreads points
  evenly across the width; `y(v)` scales linearly from 0 to 5% above the max
  value.
- Draws horizontal gridlines with `$Nk` labels, week labels on the x-axis
  (thinned with a `step` so at most ~10 labels), a solid line for
  `funding_total`, a dashed line for `invoice_total`, and a red dot on each
  mismatch week.
- The `path()` helper emits an SVG path string, starting a new subpath (`M`
  instead of `L`) after any null gap — so missing weeks show as breaks in the
  line rather than fake connections.
- A transparent `<rect id="hit">` over the plot area handles `mousemove`: it
  converts the mouse x back to the nearest point index, moves a dashed
  crosshair there, and fills/positions the tooltip (week, funding, invoice,
  delta — all through `fmtMoney`, so the tooltip also benefits from the
  negative-zero fix).

---

## 8. The two bug fixes, end to end

**Bug 1 — Delta showed “-$0.00”.** Root cause: float-noise deltas like `-1e-9`
rounding to IEEE negative zero. Fixed in *two independent layers*:
`_round` in `report.py` flips the sign bit at data-build time (§6.1), and
`fmtMoney` in the template snaps the `(-0.005, 0]` interval to `0` at display
time (§7.3). Either alone fixes the symptom; together the data is clean *and*
the UI is safe against future data paths.

**Bug 2 — table search rejected dashed dates.** Root cause: the filter matched
the query against raw cell values, and weeks are stored as `"20250106"`. Fixed
by the `searchText` helper (§7.3): every searched value now carries both its
raw and its dashed-date form, so both notations (and partials of either) match
— including dates embedded in filenames and batch ids.

---

## 9. The tests

Run everything:

```bash
.venv/bin/python -m unittest discover tests      # 70 Python tests
node --test tests/test_report_template.mjs       # 20 JS tests
```

### 9.1 `tests/test_parsing.py` (24 tests)

Builds **real `.xlsx` files** with openpyxl in temp directories, so the actual
workbook-reading path runs — no mocks. Helpers `write_funding` / `write_invoice`
create files with a given header+rows or given fixed-cell values. Covers: the
three coercion helpers exhaustively; funding-report parsing (schema, week and
source-file stamping, batch-id propagation incl. whitespace cells and mid-file
switches, blank-row skipping, header-name mapping with reordered/extra/missing
columns, string dates, numeric claim numbers, header-only files); invoice
parsing (normal layout, the negative-batch row shift, case-insensitive
substring marker, empty workbook → nulls); and `load_directory` (routing by
pattern, week-from-folder-name, case-insensitivity, warning on unrecognized
xlsx via `assertLogs`, ignoring non-digit folders, `FileNotFoundError` on no
week folders, empty folders → typed empty frames).

### 9.2 `tests/test_violations.py` (31 tests)

Uses two builder functions, `claims_df` / `invoices_df`, that take a list of
partial dicts and fill in defaults — so each test states only what it cares
about. Explicit Polars schemas keep all-null columns correctly typed. Covers
every detector: duplicate-batch detection (incl. the overpaid-vs-earliest-file
math and sort order), duplicate-claim detection (incl. same-batch repeats not
flagged), weekly reconciliation (both mismatch directions, the exactly-one-cent
boundary, sub-cent float noise, missing-invoice and missing-claims weeks via
the `fill_null(True)` path, sorting, one-row-per-week), anomaly labels and
their precedence ladder, and batch summaries.

### 9.3 `tests/test_report.py` (15 tests)

`RoundTests` pins down `_round`, checking negative zero with
`math.copysign(1.0, x)` — the only reliable way, since `-0.0 == 0.0` is `True`
in Python. `PayloadDeltaTests` runs `build_payload`/`render_report` on
synthetic frames: the `0.1 + 0.2` vs `0.3` week must produce a *positive* zero
delta and no `"delta":-0.0` anywhere in the rendered HTML; genuine ±deltas
survive; missing weeks give null deltas.

### 9.4 `tests/test_report_template.mjs` (20 tests)

Node's built-in test runner. The clever bit: an `extract(name)` helper pulls
the *actual source* of `fmtMoney`, `fmtWeek`, and `searchText` out of
`report.html` with a regex and materializes each with `new Function` — so the
tests exercise the exact code shipped inside the template, and any edit to the
template is automatically what gets tested. Covers `fmtMoney` boundaries
(`-0`, tiny negatives, `-0.005` keeping its sign, plus a 2,001-value sweep
asserting `-$0.00` can never appear) and every `searchText` case (raw/dashed/
partial queries, arrays, filenames, embedded ids, 9-digit runs, case
insensitivity).

---

## 10. Quick reference — how to do things

```bash
# regenerate the report
.venv/bin/python main.py --input-path ./data --output-path ./output

# run all tests
.venv/bin/python -m unittest discover tests -v
node --test tests/test_report_template.mjs

# run one test class / one test
.venv/bin/python -m unittest tests.test_violations.WeeklyReconciliationTests -v
.venv/bin/python -m unittest tests.test_report.RoundTests.test_negative_zero_normalized
```

Dependencies (in `.venv`): `polars` (aggregations), `openpyxl` (Excel IO).
The generated report has zero runtime dependencies — it's one static HTML file.
