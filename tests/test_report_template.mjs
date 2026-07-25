// Tests for the inline JS helpers in dqvd/templates/report.html.
// The helpers are extracted from the template itself, so these tests run
// against the exact code shipped in the report.
//
// Run with:  node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const templatePath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..", "dqvd", "templates", "report.html",
);
const template = readFileSync(templatePath, "utf-8");

function extract(name) {
  // Matches `const name = v => "...";` or a `const name = v => { ... };` block.
  const re = new RegExp(`const ${name} = [^{\\n]*(\\{[\\s\\S]*?\\n\\})?;`, "m");
  const m = template.match(re);
  assert.ok(m, `could not find "const ${name} = ..." in report.html`);
  return new Function(`${m[0]} return ${name};`)();
}

const fmtMoney = extract("fmtMoney");
const fmtWeek = extract("fmtWeek");
const searchText = extract("searchText");

// ---------- fmtMoney: no "-$0.00" ----------

test("fmtMoney: null and undefined render as em dash", () => {
  assert.equal(fmtMoney(null), "—");
  assert.equal(fmtMoney(undefined), "—");
});

test("fmtMoney: zero renders positive", () => {
  assert.equal(fmtMoney(0), "$0.00");
});

test("fmtMoney: negative zero renders positive", () => {
  assert.equal(fmtMoney(-0), "$0.00");
});

test("fmtMoney: tiny negatives that would show -$0.00 render as $0.00", () => {
  assert.equal(fmtMoney(-0.001), "$0.00");
  assert.equal(fmtMoney(-0.0049), "$0.00");
  assert.equal(fmtMoney(-1e-9), "$0.00");
});

test("fmtMoney: values that round to a real negative cent keep the sign", () => {
  assert.equal(fmtMoney(-0.005), "-$0.01");
  assert.equal(fmtMoney(-0.01), "-$0.01");
  assert.equal(fmtMoney(-10), "-$10.00");
  assert.equal(fmtMoney(-1234.56), "-$1,234.56");
});

test("fmtMoney: tiny positives round to $0.00 (no sign issue)", () => {
  assert.equal(fmtMoney(0.001), "$0.00");
  assert.equal(fmtMoney(0.0049), "$0.00");
});

test("fmtMoney: ordinary positive values unchanged", () => {
  assert.equal(fmtMoney(0.01), "$0.01");
  assert.equal(fmtMoney(1234.5), "$1,234.50");
});

test("fmtMoney: never produces the string -$0.00 across a value sweep", () => {
  for (let i = -1000; i <= 1000; i++) {
    const out = fmtMoney(i / 10000); // -0.1 .. 0.1 in 0.0001 steps
    assert.ok(!out.includes("-$0.00"), `fmtMoney(${i / 10000}) => ${out}`);
  }
});

// ---------- fmtWeek ----------

test("fmtWeek: formats yyyymmdd with dashes", () => {
  assert.equal(fmtWeek("20260713"), "2026-07-13");
});

test("fmtWeek: empty and null render as em dash", () => {
  assert.equal(fmtWeek(null), "—");
  assert.equal(fmtWeek(""), "—");
});

// ---------- searchText: date search with dashes ----------

const matches = (value, query) => searchText(value).includes(query.toLowerCase());

test("search: raw yyyymmdd query still matches", () => {
  assert.ok(matches("20260713", "20260713"));
  assert.ok(matches("20260713", "202607"));
});

test("search: dashed full date matches", () => {
  assert.ok(matches("20260713", "2026-07-13"));
});

test("search: dashed partial dates match", () => {
  assert.ok(matches("20260713", "2026-07"));
  assert.ok(matches("20260713", "07-13"));
  assert.ok(matches("20260713", "26-07-13"));
});

test("search: non-matching dates do not match", () => {
  assert.ok(!matches("20260713", "2026-07-14"));
  assert.ok(!matches("20260713", "2025-07-13"));
  assert.ok(!matches("20260713", "20260714"));
});

test("search: arrays of weeks match in both forms (week-list columns)", () => {
  const weeks = ["20260713", "20260720"];
  assert.ok(matches(weeks, "2026-07-20"));
  assert.ok(matches(weeks, "20260713"));
  assert.ok(!matches(weeks, "2026-07-27"));
});

test("search: 8-digit runs inside identifiers also match dashed (e.g. batch ids)", () => {
  assert.ok(matches("batch20260713x", "2026-07-13"));
  assert.ok(matches("batch20260713x", "20260713"));
});

test("search: longer digit runs are not split into fake dates", () => {
  assert.ok(!matches("123456789", "1234-56-78"));
  assert.ok(matches("123456789", "123456789"));
});

test("search: matching is case-insensitive on the value", () => {
  assert.ok(matches("INVOICE_20260713.XLSX", "invoice"));
  assert.ok(matches("INVOICE_20260713.XLSX", "2026-07-13"));
});

test("search: non-date text and numbers still match normally", () => {
  assert.ok(matches("reconciled", "recon"));
  assert.ok(matches(1234.56, "1234.56"));
  assert.ok(matches(-10.5, "-10.5"));
  assert.ok(!matches("reconciled", "mismatch"));
});

test("search: date inside a filename matches with dashes", () => {
  assert.ok(matches("funding_20260713.xlsx", "2026-07-13"));
});
