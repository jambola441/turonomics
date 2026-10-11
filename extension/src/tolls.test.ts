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
  addDays,
  addMonths,
  amountsLookPositive,
  chooseStatement,
  classifyHeaders,
  cleanCell,
  dateCoverage,
  describeForm,
  COOLDOWN_MS,
  describePager,
  describeSubmitCandidates,
  windowApplied,
  HISTORY_MONTHS,
  looksBlocked,
  runGate,
  parsePagerRange,
  parseSiteDate,
  sameDate,
  describeShape,
  formatForField,
  MAX_PAGES,
  maskValue,
  mergeActivityPages,
  pageChanged,
  pickActivityTable,
  pickDateFields,
  pickPageSize,
  quarterRanges,
  rankNextControls,
  rankSubmitControls,
  shouldStopPaging,
  sumImportResults,
  tableSignature,
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

// ---------------------------------------------------------------------------
// Paging
// ---------------------------------------------------------------------------
// An account-activity page shows a few weeks at a time, so a statement had to
// be sent one page at a time by hand — and a page missed is money never billed.

test("prefers a rel=next link over anything else", () => {
  const ranked = rankNextControls([
    { text: "2" },
    { text: "Next", rel: "next" },
    { text: "Next" },
  ]);
  assert.equal(ranked[0], 1);
});

test("recognises the usual arrows and wordings", () => {
  for (const text of ["Next", "next page", ">", "›", "→", "Next >"]) {
    assert.deepEqual(rankNextControls([{ text }]), [0], `did not rank ${text}`);
  }
});

test("never ranks a backwards control", () => {
  for (const text of ["Previous", "prev", "<", "‹", "←", "First"]) {
    assert.deepEqual(rankNextControls([{ text }]), [], `ranked ${text} as next`);
  }
});

test("never ranks a jump-to-last control", () => {
  // Clicking ">>" would skip every page in between, and the rows on them.
  for (const text of [">>", "»", "Last"]) {
    assert.deepEqual(rankNextControls([{ text }]), [], `ranked ${text} as next`);
  }
});

test("skips a disabled next, however it says so", () => {
  assert.deepEqual(rankNextControls([{ text: "Next", disabled: true }]), []);
  assert.deepEqual(rankNextControls([{ text: "Next", className: "btn disabled" }]), []);
});

test("an aria-label can carry the meaning the text does not", () => {
  // A chevron with no text at all is common.
  assert.deepEqual(rankNextControls([{ text: "", ariaLabel: "Next page" }]), [0]);
});

test("ranks every candidate, so a dud click can be followed by another", () => {
  const ranked = rankNextControls([
    { text: "go next" },
    { text: "Next", rel: "next" },
    { text: ">" },
  ]);
  assert.deepEqual(ranked, [1, 2, 0]);
});

test("picks 'All' over any number of rows per page", () => {
  assert.equal(
    pickPageSize([
      { value: "10", label: "10" },
      { value: "100", label: "100" },
      { value: "-1", label: "All" },
    ]),
    "-1"
  );
});

test("otherwise picks the largest page size", () => {
  assert.equal(
    pickPageSize([
      { value: "a", label: "25" },
      { value: "b", label: "100 per page" },
      { value: "c", label: "50" },
    ]),
    "b"
  );
});

test("leaves the page size alone when there is nothing numeric to pick", () => {
  assert.equal(pickPageSize([{ value: "x", label: "Sort by date" }]), null);
});

test("a signature changes when the table does and not otherwise", () => {
  const page1: ScrapedTable = { headers: ["A"], rows: [["1"], ["2"]] };
  const same: ScrapedTable = { headers: ["A"], rows: [["1"], ["2"]] };
  const page2: ScrapedTable = { headers: ["A"], rows: [["3"], ["4"]] };
  assert.equal(tableSignature(page1), tableSignature(same));
  assert.notEqual(tableSignature(page1), tableSignature(page2));
});

test("the row count alone can be the only difference", () => {
  // Same first row, same last row, different length. Without the count in the
  // signature these two read as the same page, and paging would stop here —
  // the first version of this test used tables whose ends differed too, so it
  // passed with the count removed.
  const three: ScrapedTable = { headers: ["A"], rows: [["1"], ["2"], ["1"]] };
  const two: ScrapedTable = { headers: ["A"], rows: [["1"], ["1"]] };
  assert.notEqual(tableSignature(three), tableSignature(two));
});

test("text reading both ways is not treated as next", () => {
  // "< Prev | Next >" as one control is a coin toss, so it is excluded rather
  // than guessed at.
  assert.deepEqual(rankNextControls([{ text: "< Prev | Next >" }]), []);
});

test("an aria-label outranks text that disagrees with it", () => {
  // Written expecting the opposite, which was wrong. The text here is most
  // often the whole paginator's — "« Prev 1 2 3 Next »" picked up from a
  // wrapper that carries the real meaning in its label — so the label is the
  // more reliable of the two, and excluding it would silently truncate a
  // statement on exactly the markup this has to cope with.
  assert.deepEqual(rankNextControls([{ text: "Prev", ariaLabel: "Next page" }]), [0]);
});

test("paging continues while the pages keep changing", () => {
  assert.equal(shouldStopPaging(["a", "b", "c"]), null);
});

test("paging stops when a page repeats", () => {
  // A control that wraps to the first page would otherwise loop forever.
  assert.match(shouldStopPaging(["a", "b", "a"]) ?? "", /stopped changing/);
});

test("paging stops when the click changed nothing", () => {
  assert.match(shouldStopPaging(["a", "a"]) ?? "", /stopped changing/);
});

test("paging stops on an empty page", () => {
  assert.match(shouldStopPaging(["a", "empty"]) ?? "", /empty/);
});

test("an empty first page is not a reason to stop before starting", () => {
  assert.equal(shouldStopPaging(["empty"]), null);
});

test("paging gives up rather than clicking forever, and says so", () => {
  const many = Array.from({ length: MAX_PAGES }, (_, i) => `p${i}`);
  const reason = shouldStopPaging(many);
  assert.match(reason ?? "", /stopped at/);
  // The wording matters: rows may be missing, and that has to reach the
  // operator rather than looking like a clean read.
  assert.match(reason ?? "", new RegExp(String(MAX_PAGES)));
});

test("pages are merged into one table", () => {
  const p1: ScrapedTable = { headers: ACTIVITY.headers, rows: [ACTIVITY.rows[0]] };
  const p2: ScrapedTable = { headers: ACTIVITY.headers, rows: [ACTIVITY.rows[1]] };
  const merged = mergeActivityPages([p1, p2]);
  assert.ok(merged);
  assert.equal(merged.rows.length, 2);
  assert.deepEqual(merged.headers, ACTIVITY.headers);
});

test("a row seen on two pages is kept once", () => {
  // Paging controls re-render the same rows more often than an account is
  // charged twice in one second.
  const merged = mergeActivityPages([ACTIVITY, ACTIVITY]);
  assert.equal(merged?.rows.length, 2);
});

test("a page whose headers disagree is dropped, not appended", () => {
  // Appending it would put a plaza where an amount should be.
  const odd: ScrapedTable = {
    headers: ["Date", "Tag/Plate #", "Amount", "Something Else"],
    rows: [["12/29/2025", "99900000111", "$-2.86", "x"]],
  };
  const merged = mergeActivityPages([ACTIVITY, odd]);
  assert.equal(merged?.rows.length, 2, "only the first page's rows");
});

test("merging nothing is null rather than an empty statement", () => {
  assert.equal(mergeActivityPages([]), null);
  assert.equal(mergeActivityPages([{ headers: ["Date"], rows: [] }]), null);
});

test("a merged table is what chooseStatement uses", () => {
  const merged: ScrapedTable = {
    headers: ACTIVITY.headers,
    rows: [ACTIVITY.rows[0], ACTIVITY.rows[1], ACTIVITY.rows[0].map((c) => c + "x")],
  };
  const choice = chooseStatement(page({ tables: [ACTIVITY], merged }));
  assert.equal(choice.source, "table");
  assert.equal(choice.rowCount, 3, "the merged pages, not the one on screen");
});

test("a next-looking arrow with a jump-to-last label is excluded", () => {
  // The case the jump-to-last check exists for. ">" alone scores as next, so
  // without consulting the label this control would be clicked — and it skips
  // every page in between, and the crossings on them. Every earlier test here
  // had the two agreeing, so removing the check went unnoticed.
  assert.deepEqual(rankNextControls([{ text: ">", ariaLabel: "Last page" }]), []);
  assert.deepEqual(rankNextControls([{ text: ">", ariaLabel: "Jump to last" }]), []);
});

test("'>>' text beats a label claiming it is next", () => {
  // The case the symbol check exists for, and a deliberate asymmetry with the
  // aria-label test above. Treating a real "next" as "last" stops paging early,
  // which is reported and can be finished by hand. Treating a real "last" as
  // "next" silently skips pages and reads as a complete statement. The second
  // is the worse mistake, so for this one signal the text wins.
  assert.deepEqual(rankNextControls([{ text: ">>", ariaLabel: "Next page" }]), []);
  assert.deepEqual(rankNextControls([{ text: "»", ariaLabel: "Next" }]), []);
});

// ---------------------------------------------------------------------------
// Date windows
// ---------------------------------------------------------------------------
test("months are added with the day clamped to the month's length", () => {
  assert.equal(addMonths("2026-05-31", -3), "2026-02-28");
  assert.equal(addMonths("2024-05-31", -3), "2024-02-29");
  assert.equal(addMonths("2026-01-15", -3), "2025-10-15");
  assert.equal(addMonths("2026-11-30", 3), "2027-02-28");
});

test("days roll over month and year ends", () => {
  assert.equal(addDays("2026-03-01", -1), "2026-02-28");
  assert.equal(addDays("2026-01-01", -1), "2025-12-31");
});

test("windows run newest first, three months each, ending today", () => {
  const ranges = quarterRanges("2026-10-07", 12);
  assert.equal(ranges.length, 4);
  assert.deepEqual(ranges[0], { from: "2026-07-08", to: "2026-10-07" });
  assert.equal(ranges[3].from, "2025-10-08");
});

test("windows leave no gap and no overlap, whatever day it is", () => {
  // A gap is a crossing never read. Checked across every day of two years so
  // the month-end clamping cannot hide one.
  for (let i = 0; i < 730; i++) {
    const today = addDays("2025-01-01", i);
    const ranges = quarterRanges(today, 12);
    assert.equal(ranges[0].to, today);
    for (let w = 0; w < ranges.length; w++) {
      assert.ok(ranges[w].from <= ranges[w].to, `${today}: empty window ${w}`);
      if (w > 0) {
        assert.equal(addDays(ranges[w].to, 1), ranges[w - 1].from, `${today}: gap before window ${w - 1}`);
      }
    }
  }
});

test("a history that is not a multiple of three still covers it", () => {
  assert.equal(quarterRanges("2026-10-07", 4).length, 2);
});

test("finds the from and to fields by what they say", () => {
  const fields = pickDateFields([
    { index: 0, type: "text", hint: "q search" },
    { index: 1, type: "text", hint: "endDate End Date" },
    { index: 2, type: "text", hint: "startDate Start Date" },
  ]);
  assert.equal(fields?.from.index, 2);
  assert.equal(fields?.to.index, 1);
});

test("two unlabelled date inputs are taken in document order", () => {
  const fields = pickDateFields([
    { index: 4, type: "date", hint: "" },
    { index: 5, type: "date", hint: "" },
  ]);
  assert.equal(fields?.from.index, 4);
  assert.equal(fields?.to.index, 5);
});

test("never types a date into something that is not a date field", () => {
  assert.equal(
    pickDateFields([
      { index: 0, type: "text", hint: "tag number" },
      { index: 1, type: "text", hint: "plate" },
    ]),
    null
  );
});

test("a lone date field is not enough to drive a range", () => {
  assert.equal(pickDateFields([{ index: 0, type: "date", hint: "from" }]), null);
});

test("three unlabelled date fields are ambiguous, not guessed", () => {
  assert.equal(
    pickDateFields([
      { index: 0, type: "date", hint: "" },
      { index: 1, type: "date", hint: "" },
      { index: 2, type: "date", hint: "" },
    ]),
    null
  );
});

test("dates are written the way the field expects them", () => {
  assert.equal(formatForField("2026-07-08", { index: 0, type: "date", hint: "" }), "2026-07-08");
  assert.equal(formatForField("2026-07-08", { index: 0, type: "text", hint: "date" }), "07/08/2026");
  assert.equal(
    formatForField("2026-07-08", { index: 0, type: "text", hint: "", placeholder: "yyyy-mm-dd" }),
    "2026-07-08"
  );
});

test("the search button is found, and export, clear and paging never are", () => {
  const ranked = rankSubmitControls([
    { text: "Export to CSV" },
    { text: "Clear" },
    { text: "Next" },
    { text: "Search" },
    { text: "", ariaLabel: "Apply filter" },
  ]);
  assert.deepEqual(ranked, [3, 4]);
});

test("a control that starts like a search word but exports or pages is excluded", () => {
  // The exclusion list only matters for text the submit pattern would accept.
  assert.deepEqual(
    rankSubmitControls([
      { text: "Show previous" },
      { text: "View last page" },
      { text: "Get download" },
      { text: "Search" },
    ]),
    [3]
  );
});

test("a disabled search button is not clicked", () => {
  assert.deepEqual(rankSubmitControls([{ text: "Search", disabled: true }]), []);
});

test("counts add across windows and unbound tags are listed once", () => {
  const total = sumImportResults([
    { rows: 10, imported: 8, already_known: 2, matched: 6, unmatched: 2, unknown_tags: ["A", "B"] },
    { rows: 5, imported: 5, already_known: 0, matched: 4, unmatched: 1, unknown_tags: ["B", "C"] },
  ]);
  assert.deepEqual(total, {
    rows: 15, imported: 13, already_known: 2, matched: 10, unmatched: 3, unknown_tags: ["A", "B", "C"],
  });
});

// ---------------------------------------------------------------------------
// The run log
// ---------------------------------------------------------------------------
test("the form report names the fields it chose and the button it would click", () => {
  const lines = describeForm(
    [
      { index: 3, type: "text", hint: "startDate Start Date", placeholder: "mm/dd/yyyy" },
      { index: 4, type: "text", hint: "endDate End Date", placeholder: "mm/dd/yyyy" },
    ],
    [{ text: "Search" }]
  ).join("\n");
  assert.match(lines, /input\[3\].*FROM/);
  assert.match(lines, /input\[4\].*TO/);
  assert.match(lines, /submit candidate \[0\] "Search"/);
});

test("the form report says so when it recognised nothing", () => {
  const lines = describeForm([{ index: 0, type: "text", hint: "q" }], [{ text: "Export" }]).join("\n");
  assert.match(lines, /no from\/to pair recognised/);
  assert.match(lines, /no search button recognised/);
});

test("the form report leaves out the page's other buttons", () => {
  // A nav bar can carry an account holder's name; the report is for pasting.
  const lines = describeForm([], [{ text: "Welcome, Jane Doe" }, { text: "Search" }]).join("\n");
  assert.ok(!lines.includes("Jane Doe"));
});

// ---------------------------------------------------------------------------
// What a real run showed
// ---------------------------------------------------------------------------
test("a two-digit-year field gets a two-digit year", () => {
  // E-ZPass's fields are MM/DD/YY. Typing 07/08/2026 into one was the first
  // version's mistake, and a mask can read it as 2020.
  const field = { index: 0, type: "text", hint: "startDate Start Date MM/DD/YY", placeholder: "MM/DD/YY" };
  assert.equal(formatForField("2026-07-08", field), "07/08/26");
  assert.equal(formatForField("2025-12-31", field), "12/31/25");
});

test("a four-digit-year field still gets four digits", () => {
  const field = { index: 0, type: "text", hint: "Date", placeholder: "MM/DD/YYYY" };
  assert.equal(formatForField("2026-07-08", field), "07/08/2026");
});

test("site dates are read with either year width", () => {
  assert.equal(parseSiteDate("7/8/26"), "2026-07-08");
  assert.equal(parseSiteDate("07/08/2026"), "2026-07-08");
  assert.equal(parseSiteDate("PAYMENT"), null);
});

test("rows inside the window are not flagged, rows outside are", () => {
  const table: ScrapedTable = {
    headers: ["Tag/Plate #", "Date", "Amount"],
    rows: [
      ["a", "7/9/26", "$-1.00"],
      ["b", "10/7/26", "$-1.00"],
      ["c", "6/30/26", "$-1.00"],
      ["d", "no date", "$-1.00"],
    ],
  };
  const c = dateCoverage(table, { from: "2026-07-08", to: "2026-10-07" });
  assert.equal(c.outside, 1);
  assert.equal(c.undated, 1);
  assert.equal(c.first, "2026-06-30");
  assert.equal(c.last, "2026-10-07");
});

test("a search that ignored its dates shows up as rows outside the window", () => {
  // The failure that reads as success: every row imports as already known.
  const table: ScrapedTable = {
    headers: ["Tag/Plate #", "Date", "Amount"],
    rows: [["a", "10/1/25", "$-1.00"], ["b", "11/1/25", "$-1.00"]],
  };
  assert.equal(dateCoverage(table, { from: "2026-07-08", to: "2026-10-07" }).outside, 2);
});

test("a masked field's rewrite of a date is still that date", () => {
  // Straight from a real log: typed "07/08/26", read back "7<LRM>/8<LRM>/26".
  assert.equal(sameDate("7\u200e/8\u200e/26", "07/08/26"), true);
  assert.equal(sameDate("10/7\u200e/26", "10/07/26"), true);
});

test("a different date, or an empty field, is not the same", () => {
  assert.equal(sameDate("7\u200e/9\u200e/26", "07/08/26"), false);
  assert.equal(sameDate("", "07/08/26"), false);
  assert.equal(sameDate("<missing>", "07/08/26"), false);
});

test("a table that vanished while the next page loads is not a new page", () => {
  // The real bug: E-ZPass empties the table during the load, "empty" differs
  // from every signature, and paging stopped at page one.
  assert.equal(pageChanged("empty", "96#a#b"), false);
});

test("the same table is not a new page, a different one is", () => {
  assert.equal(pageChanged("96#a#b", "96#a#b"), false);
  assert.equal(pageChanged("90#c#d", "96#a#b"), true);
});

test("a table appearing after an empty one is a page", () => {
  assert.equal(pageChanged("90#c#d", "empty"), true);
});

test("reads MUI's pager count", () => {
  assert.deepEqual(parsePagerRange("1\u201310 of 96"), { from: 1, to: 10, total: 96 });
  assert.deepEqual(parsePagerRange("1-100 of 1,234"), { from: 1, to: 100, total: 1234 });
  assert.deepEqual(parsePagerRange(" 91\u201396 of 96 "), { from: 91, to: 96, total: 96 });
});

test("text that merely contains a count is not a pager", () => {
  assert.equal(parsePagerRange("Showing 1-10 of 96 results"), null);
  assert.equal(parsePagerRange("Rows 1-10 of 96"), null, "leading text");
  assert.equal(parsePagerRange(""), null);
  assert.equal(parsePagerRange("1-10 of many"), null);
});

test("the pager report lists its own controls and not the rest of the page", () => {
  const lines = describePager([
    { text: "Welcome, Jane Doe" },
    { text: "", ariaLabel: "Go to next page", className: "MuiPaginationItem-root Mui-disabled" },
    { text: "3", ariaLabel: "Go to page 3", className: "MuiPaginationItem-root" },
  ]).join("\n");
  assert.ok(!lines.includes("Jane Doe"));
  assert.match(lines, /\[1\].*Go to next page.*disabled=true/);
  assert.match(lines, /\[2\].*Go to page 3.*disabled=false/);
});

test("the pager report says so when nothing looked like a pager", () => {
  assert.match(describePager([{ text: "Home" }]).join("\n"), /no control looked like part of a pager/);
});

// ---------------------------------------------------------------------------
// Being gentle
// ---------------------------------------------------------------------------
test("a read covers one window unless asked for more", () => {
  // Everything older is already on file; a year per run is the burst that got
  // the account refused.
  assert.equal(HISTORY_MONTHS, 3);
  assert.equal(quarterRanges("2026-10-07").length, 1);
});

test("recognises the wording of a block or an outage, and says which words", () => {
  const blocked = looksBlocked("Header Welcome Jane Doe ... We detected unusual activity from your connection. Ref 12345");
  assert.ok(blocked);
  assert.match(blocked, /unusual activity/);
  assert.ok(!blocked.includes("12345"), "digits masked");
  assert.match(looksBlocked("The service is temporarily unavailable. Please try again later.") ?? "", /unavailable/);
  assert.match(looksBlocked("Please verify you are not a robot") ?? "", /robot/);
});

test("the snippet is the neighbourhood, not the page", () => {
  const long = "Welcome Jane Doe, account 99900000111. " + "x ".repeat(200) + "access denied " + "y ".repeat(200);
  const snippet = looksBlocked(long) ?? "";
  assert.ok(!snippet.includes("Jane Doe"));
  assert.ok(snippet.length < 250);
});

test("an ordinary activity page is not mistaken for a block", () => {
  assert.equal(looksBlocked("Account Activity Start Date End Date Filter 1-10 of 96 Rows per page"), null);
});

test("a run is refused straight after another, and says for how long", () => {
  const now = 1_000_000_000;
  assert.match(runGate(now, now - 60_000, null) ?? "", /wait 4 more minute/);
  assert.equal(runGate(now, now - COOLDOWN_MS - 1, null), null);
  assert.equal(runGate(now, null, null), null);
});

test("a block pauses everything, whatever the cooldown says", () => {
  const now = 1_000_000_000;
  assert.match(runGate(now, null, now + 30 * 60_000) ?? "", /30 minute.*block or outage/);
  assert.equal(runGate(now, null, now - 1), null, "expired");
});

// ---------------------------------------------------------------------------
// A search that never ran
// ---------------------------------------------------------------------------
test("a disabled Filter is listed, so its absence from the ranking can be explained", () => {
  const controls = [
    { text: "Home" },
    { text: "Filter", disabled: true },
    { text: "Search", className: "btn" },
  ];
  assert.deepEqual(rankSubmitControls(controls), [2], "only the enabled one is chosen");
  const lines = describeSubmitCandidates(controls).join("\n");
  assert.match(lines, /\[1\].*"Filter".*disabled=true/);
  assert.match(lines, /\[2\].*"Search".*disabled=false/);
  assert.ok(!lines.includes("Home"));
});

test("a control with filter or search in the middle of its text is listed too", () => {
  const lines = describeSubmitCandidates([{ text: "Date filter", disabled: true }]).join("\n");
  assert.match(lines, /"Date filter".*disabled=true/);
});

test("says so when nothing on the page looks like a search button", () => {
  assert.match(describeSubmitCandidates([{ text: "Home" }]).join("\n"), /nothing on the page reads like a search button/);
});

test("rows from other dates mean the search did not apply", () => {
  const table: ScrapedTable = {
    headers: ["Tag/Plate #", "Date", "Amount"],
    rows: [["a", "9/1/26", "$-1.00"], ["b", "8/20/26", "$-1.00"]],
  };
  // Still showing the previous window while asked for an older one.
  assert.equal(windowApplied(dateCoverage(table, { from: "2026-04-08", to: "2026-07-07" })), false);
  assert.equal(windowApplied(dateCoverage(table, { from: "2026-07-08", to: "2026-10-07" })), true);
});

test("an empty window is not mistaken for a search that never ran", () => {
  assert.equal(windowApplied(dateCoverage({ headers: ["Date"], rows: [] }, { from: "2026-01-01", to: "2026-03-31" })), true);
});

test("a row or two on the edge of the window does not fail it", () => {
  const rows = [
    ["a", "7/7/26", "$-1.00"], // a day before the window starts
    ["b", "7/9/26", "$-1.00"],
    ["c", "8/1/26", "$-1.00"],
    ["d", "9/1/26", "$-1.00"],
  ];
  const table: ScrapedTable = { headers: ["Tag/Plate #", "Date", "Amount"], rows };
  assert.equal(windowApplied(dateCoverage(table, { from: "2026-07-08", to: "2026-10-07" })), true);
});
