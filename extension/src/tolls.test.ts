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
  MAX_PAGES,
  maskValue,
  mergeActivityPages,
  pickActivityTable,
  pickPageSize,
  rankNextControls,
  shouldStopPaging,
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
