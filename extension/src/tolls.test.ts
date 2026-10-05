/**
 * Tests for the statement-from-a-page logic.
 *
 * The extension had lint and a build in CI and nothing that ran it. This is the
 * part worth running: every one of these functions decides something about
 * money, and the markup it works against belongs to a site behind a WAF that
 * nobody here can open in CI.
 *
 * Run with `npm test`.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  amountsLookPositive,
  chooseStatement,
  classifyHeaders,
  cleanCell,
  describeShape,
  maskValue,
  pickActivityTable,
  toCsv,
  usableRows,
  type ScrapedPage,
  type ScrapedTable,
} from "./tolls.js";

// The column names a real NY E-ZPass download uses. The page's own table may
// name them differently, which is the thing these tests are loose about.
const ACTIVITY: ScrapedTable = {
  headers: ["Lane Txn ID", "Tag/Plate #", "Agency", "Entry Plaza", "Exit Plaza",
            "Class", "Date", "Exit Time", "Amount"],
  rows: [
    ["1", "99900000111", "NYSTA", "15", "19", "2L", "12/29/2025", "05:13:32 PM", "$-2.86"],
    ["2", "NY LZA7293", "MTAB&T", "", "RKB", "31", "12/31/2025", "03:10:36 PM", "$-9.11"],
  ],
};

function page(over: Partial<ScrapedPage> = {}): ScrapedPage {
  return { url: "https://www.e-zpassny.com/activity", tables: [], downloadLinks: [], ...over };
}

// ---------------------------------------------------------------------------
// Finding the table
// ---------------------------------------------------------------------------
test("recognises the columns of a real statement header", () => {
  const found = classifyHeaders(ACTIVITY.headers);
  assert.ok(found.has("date"));
  assert.ok(found.has("amount"));
  assert.ok(found.has("tagPlate"));
  assert.ok(found.has("plaza"));
});

test("a table with no tag or plate column is not a statement", () => {
  // The balance-history table on the same page looks similar and must not win.
  const balance: ScrapedTable = {
    headers: ["Date", "Description", "Amount"],
    rows: [["12/29/2025", "Replenishment", "$25.00"]],
  };
  assert.equal(pickActivityTable([balance]), null);
});

test("picks the activity table out of several", () => {
  const summary: ScrapedTable = {
    headers: ["Tag/Plate #", "Date", "Amount"],
    rows: [["99900000111", "12/29/2025", "$-2.86"]],
  };
  // The summary has the required three; the activity table has those plus a
  // plaza, so it wins on recognised columns rather than on row count.
  assert.equal(pickActivityTable([summary, ACTIVITY]), ACTIVITY);
});

test("a longer table wins when the columns tie", () => {
  const short: ScrapedTable = { headers: ACTIVITY.headers, rows: [ACTIVITY.rows[0]] };
  const long: ScrapedTable = {
    headers: ACTIVITY.headers,
    rows: [ACTIVITY.rows[0], ACTIVITY.rows[1], ACTIVITY.rows[0]],
  };
  assert.equal(pickActivityTable([short, long]), long);
});

test("an empty table is never chosen", () => {
  assert.equal(pickActivityTable([{ headers: ACTIVITY.headers, rows: [] }]), null);
});

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------
test("collapses the whitespace a table cell carries and a CSV cannot", () => {
  assert.equal(cleanCell("  05:13:32 PM\n  "), "05:13:32 PM");
});

test("drops a row whose width does not match the header", () => {
  // A "no records found" row spans every column as one cell. Repairing it would
  // mean guessing which column its single value belongs to.
  const table: ScrapedTable = {
    headers: ACTIVITY.headers,
    rows: [...ACTIVITY.rows, ["No records found"]],
  };
  assert.equal(usableRows(table).length, 2);
});

test("drops a row that is entirely empty", () => {
  const table: ScrapedTable = {
    headers: ["A", "B"],
    rows: [["1", "2"], ["", "  "]],
  };
  assert.deepEqual(usableRows(table), [["1", "2"]]);
});

test("quotes a cell containing a comma rather than splitting the row", () => {
  const csv = toCsv(["Plaza", "Amount"], [["Yonkers, NY", "$-2.86"]]);
  assert.equal(csv, 'Plaza,Amount\n"Yonkers, NY",$-2.86\n');
});

test("doubles a quote inside a cell", () => {
  assert.equal(toCsv(["A"], [['say "hi"']]), 'A\n"say ""hi"""\n');
});

// ---------------------------------------------------------------------------
// The sign trap
// ---------------------------------------------------------------------------
// A download writes charges negative and the API skips positive rows as
// payments. A page that shows charges positive therefore imports as nothing,
// and the only thing worse than noticing late would be flipping the signs and
// billing a guest for a replenishment.
test("says nothing when the amounts carry minus signs", () => {
  assert.equal(amountsLookPositive(ACTIVITY.headers, ACTIVITY.rows), false);
});

test("flags a table whose amounts are all positive", () => {
  const rows = ACTIVITY.rows.map((row) => row.map((c) => c.replace("$-", "$")));
  assert.equal(amountsLookPositive(ACTIVITY.headers, rows), true);
});

test("treats accounting parentheses as a negative", () => {
  const rows = [["1", "99900000111", "NYSTA", "15", "19", "2L", "12/29/2025", "05:13 PM", "($2.86)"]];
  assert.equal(amountsLookPositive(ACTIVITY.headers, rows), false);
});

test("a table with no numbers at all is not called positive", () => {
  assert.equal(amountsLookPositive(["Date", "Amount"], [["", ""]]), false);
});

// ---------------------------------------------------------------------------
// Choosing what to send
// ---------------------------------------------------------------------------
test("prefers a CSV the site generated over the rendered table", () => {
  const downloaded = "Date,Tag/Plate #,Exit Plaza,Amount\n12/29/2025,99900000111,19,$-2.86\n";
  const choice = chooseStatement(page({ tables: [ACTIVITY], downloadedCsv: downloaded }));
  assert.equal(choice.source, "download");
  assert.equal(choice.csv, downloaded);
  assert.equal(choice.rowCount, 1);
});

test("falls back to the table when the page offers no download", () => {
  const choice = chooseStatement(page({ tables: [ACTIVITY] }));
  assert.equal(choice.source, "table");
  assert.equal(choice.rowCount, 2);
  assert.ok(choice.csv?.startsWith("Lane Txn ID,Tag/Plate #"));
  // Verbatim, including the dollar signs and the leading zeros: the API's
  // parser is the one that understands them.
  assert.ok(choice.csv?.includes("$-2.86"));
});

test("reports having found nothing rather than sending an empty CSV", () => {
  const choice = chooseStatement(page({ tables: [] }));
  assert.equal(choice.source, "none");
  assert.equal(choice.csv, null);
});

test("carries the header names out, so a rejected import can be explained", () => {
  const odd: ScrapedTable = {
    headers: ["Posted", "Transponder", "Facility", "Toll"],
    rows: [["12/29/2025", "99900000111", "Yonkers", "$-2.86"]],
  };
  const choice = chooseStatement(page({ tables: [odd] }));
  assert.equal(choice.source, "table");
  assert.deepEqual(choice.headers, ["Posted", "Transponder", "Facility", "Toll"]);
});

test("an empty download is not preferred over a table that has rows", () => {
  const choice = chooseStatement(page({ tables: [ACTIVITY], downloadedCsv: "   \n" }));
  assert.equal(choice.source, "table");
});

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------
test("masking keeps the shape and loses the value", () => {
  assert.equal(maskValue(" 00414500433 "), "###########");
  assert.equal(maskValue("NY LZA7293"), "aa aaa####");
  assert.equal(maskValue("$-2.86"), "$-#.##");
});

test("the shape report names the columns but not the crossings", () => {
  const report = describeShape(page({ tables: [ACTIVITY], downloadLinks: ["/export.csv?x=1"] }));
  assert.ok(report.includes("Tag/Plate #"), "column names are what is worth knowing");
  assert.ok(report.includes("tagPlate"), "says which columns were recognised");
  assert.ok(!report.includes("99900000111"), "no tag number in a report meant for sharing");
  assert.ok(!report.includes("LZA7293"), "no plate either");
  assert.ok(report.includes("/export.csv"), "the download link, without its query");
  assert.ok(!report.includes("x=1"), "a query string can carry a session id");
});

// ---------------------------------------------------------------------------
// The contract with the Python importer
// ---------------------------------------------------------------------------
// `examples/ezpass/scraped-from-page.csv` is read by both sides: this test
// asserts the scraper still produces it byte for byte, and
// `api/tests/test_parsing_ezpass.py` asserts the importer still reads it. A
// change to either end that the other has not agreed to fails here or there,
// which is the only way to keep two languages honest about one file format.
test("produces exactly the CSV the Python importer is tested against", () => {
  const fixture = readFileSync(
    new URL("../../examples/ezpass/scraped-from-page.csv", import.meta.url),
    "utf8"
  );
  const headers = ["Lane Txn ID", "Tag/Plate #", "Agency", "Entry Plaza", "Exit Plaza",
                   "Class", "Date", "Exit Time", "Amount"];
  const rows = [
    ["33232151931", "99900000111", "NYSTA", "15", "19", "2L", "12/29/2025", "05:13:32 PM", "$-2.86"],
    ["33237138399", "NY LZA7293", "MTAB&T", "", "Yonkers, NY", "31", "12/31/2025", "03:10:36 PM", "$-9.11"],
    ["", "", "", "", "PAYMENT", "", "12/29/2025", "", "$25.00"],
    ["33240000001", "99900000111", "NYSTA", "", "19", "2L", "12/30/2025", "11:00:00 AM", "$1.50"],
  ];
  assert.equal(toCsv(headers, rows), fixture);
});
