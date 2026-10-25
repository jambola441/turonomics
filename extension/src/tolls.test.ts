/**
 * Tests for the E-ZPass statement logic.
 *
 * The extension had lint and a build in CI and nothing that ran it. This is the
 * part worth running: every one of these functions decides something about
 * money, and the site it reads is behind a WAF that nobody here can open in CI.
 *
 * Run with `npm test`.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  apiAmount,
  apiDate,
  apiRowsToCsv,
  apiTime,
  API_MAX_CALLS,
  nextStartIndex,
  readTransactionAnswer,
  transactionRequest,
  describeTollsCommand,
  addDays,
  addMonths,
  cleanCell,
  HISTORY_MONTHS,
  WINDOW_DAYS,
  looksBlocked,
  runGate,
  quarterRanges,
  sumImportResults,
  toCsv,
} from "./tolls.js";

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------
test("collapses the whitespace a table cell carries and a CSV cannot", () => {
  assert.equal(cleanCell("  05:13:32 PM\n  "), "05:13:32 PM");
});

test("quotes a cell containing a comma rather than splitting the row", () => {
  const csv = toCsv(["Plaza", "Amount"], [["Yonkers, NY", "$-2.86"]]);
  assert.equal(csv, 'Plaza,Amount\n"Yonkers, NY",$-2.86\n');
});

test("doubles a quote inside a cell", () => {
  assert.equal(toCsv(["A"], [['say "hi"']]), 'A\n"say ""hi"""\n');
});

// ---------------------------------------------------------------------------
// The contract with the Python importer
// ---------------------------------------------------------------------------
// `examples/ezpass/scraped-from-page.csv` is read by both sides: this test
// asserts the CSV writer still produces it byte for byte, and
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

test("windows run newest first, 90 days each, ending today", () => {
  const ranges = quarterRanges("2026-10-07", 12);
  assert.equal(ranges.length, 4);
  assert.deepEqual(ranges[0], { from: "2026-07-10", to: "2026-10-07" });
  assert.equal(ranges[3].to, addDays(ranges[2].from, -1));
});

test("no window is longer than the filter accepts", () => {
  // A real run: 90 and 91 days were taken, 92 was not. Calendar quarters are 91
  // or 92 days, which is why the oldest window of that run was refused.
  const days = (r: { from: string; to: string }): number => {
    let n = 1;
    for (let d = r.from; d < r.to; d = addDays(d, 1)) n++;
    return n;
  };
  for (let i = 0; i < 400; i++) {
    for (const r of quarterRanges(addDays("2025-01-01", i), 24)) {
      assert.ok(days(r) <= WINDOW_DAYS, `${r.from}..${r.to} is ${days(r)} days`);
    }
  }
  assert.equal(WINDOW_DAYS, 90);
});

test("3 months is one window and 9 is three", () => {
  assert.equal(quarterRanges("2026-10-10", 3).length, 1);
  assert.equal(quarterRanges("2026-10-10", 9).length, 3);
  assert.equal(quarterRanges("2026-10-10", 24).length, 8);
});

test("windows leave no gap and no overlap, whatever day it is", () => {
  // A gap is a crossing never read. Checked across every day of two years.
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

test("a history that is not a multiple of a window still covers it", () => {
  assert.equal(quarterRanges("2026-10-07", 4).length, 2);
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

test("there is no minimum gap between runs any more", () => {
  const now = 1_000_000_000;
  assert.equal(runGate(now, null), null);
});

test("a block pauses everything", () => {
  const now = 1_000_000_000;
  assert.match(runGate(now, now + 30 * 60_000) ?? "", /30 minute.*block or outage/);
  assert.equal(runGate(now, now - 1), null, "expired");
});

// ---------------------------------------------------------------------------
// Read E-ZPass, from the site
// ---------------------------------------------------------------------------
test("a Read E-ZPass command says what the run read", () => {
  const out = describeTollsCommand({
    result: { rows: 120, imported: 7, already_known: 113, matched: 5 },
    source: "api",
    windows: [{}],
  });
  assert.equal(out.ok, true);
  assert.equal(out.result, "120 rows read over 1 date range · 7 new · 113 already on file · 5 attributed");
});

test("a Read E-ZPass command passes a problem on as it was said", () => {
  const out = describeTollsCommand({ result: null, source: "none", problem: "paused for an hour" });
  assert.deepEqual(out, { ok: false, result: "paused for an hour" });
});

test("a Read E-ZPass command calls an empty page probably logged out", () => {
  const out = describeTollsCommand({ result: null, source: "none" });
  assert.equal(out.ok, false);
  assert.match(out.result, /logged out/);
});

// ---------------------------------------------------------------------------
// The E-ZPass API
// ---------------------------------------------------------------------------
// The shape the site's transaction call answers with, names and values made up.
const API_TOLL = {
  laneTransactionId: "33232151931",
  transactionNumber: "777",
  tagOrPlateNumber: "99900000111",
  agency: "NYSTA",
  entryPlaza: "15",
  exitPlaza: "19",
  vehicleClass: "2L",
  exitDate: "12/29/2025",
  exitTime: "17:13:32.000",
  transactionDate: "12/29/2025 17:13:32.000",
  amount: -2.86,
  activity: "TOLL POSTING",
  type: "ETOL",
  name: "A HOLDER",
  address: "1 SOME ST",
};

test("an API window asks for every row of its dates, counting from 1", () => {
  assert.deepEqual(transactionRequest({ from: "2026-07-28", to: "2026-10-25" }), {
    agency: "",
    startIndex: "1",
    count: 500,
    startDate: "07/28/2026",
    endDate: "10/25/2026",
    searchDate: "Transaction date",
    type: "",
    transponderOrPlateNumber: "",
    transactionType: "ALL",
  });
  assert.equal(transactionRequest({ from: "2026-07-28", to: "2026-10-25" }, 501).startIndex, "501");
  assert.equal(apiDate("2026-01-05"), "01/05/2026");
});

test("the API's 24-hour time reads as the download's", () => {
  assert.equal(apiTime("17:13:32.000"), "05:13:32 PM");
  assert.equal(apiTime("00:05:01.000"), "12:05:01 AM");
  assert.equal(apiTime("12:00:00.000"), "12:00:00 PM");
  assert.equal(apiTime("09:30"), "09:30:00 AM");
  assert.equal(apiTime("soon"), "");
});

test("the API's amount keeps its sign, as the download writes it", () => {
  assert.equal(apiAmount(-2.86), "$-2.86");
  assert.equal(apiAmount(-9.1), "$-9.10");
  assert.equal(apiAmount(25), "$25.00");
  assert.equal(apiAmount("-1.50"), "$-1.50");
  assert.equal(apiAmount(null), "");
  assert.equal(apiAmount(""), "");
  assert.equal(apiAmount("n/a"), "");
});

test("only toll postings become CSV rows, in the download's columns", () => {
  const rows = [
    API_TOLL,
    { ...API_TOLL, laneTransactionId: "", activity: "PAYMENT", amount: 25 },
    { ...API_TOLL, activity: "CBD CREDIT", amount: 1.5 },
    { ...API_TOLL, activity: "PARKING", amount: -20 },
  ];
  const out = apiRowsToCsv(rows);
  assert.equal(out.rowCount, 1);
  assert.equal(out.skipped, 3);
  assert.equal(
    out.csv,
    "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount\n" +
      "33232151931,99900000111,NYSTA,15,19,2L,12/29/2025,05:13:32 PM,$-2.86\n"
  );
});

test("the account holder's name and address never reach the CSV", () => {
  const { csv } = apiRowsToCsv([API_TOLL]);
  assert.doesNotMatch(csv, /HOLDER|SOME ST|777/);
});

test("a toll posting is recognised whatever its case or padding", () => {
  assert.equal(apiRowsToCsv([{ ...API_TOLL, activity: " Toll Posting " }]).rowCount, 1);
});

test("the API's CSV is one the API's parser has a test for", () => {
  // The same header and row as the contract file the Python parser reads.
  const contract = readFileSync(new URL("../../examples/ezpass/scraped-from-page.csv", import.meta.url), "utf8");
  assert.equal(contract.split(/\r?\n/)[0], apiRowsToCsv([]).csv.trim());
});

test("an answer gives its rows and total; anything else is not an answer", () => {
  const body = { message: "ok", statusCode: 200, transactionList: { totalCount: 2, count: 2, transaction: [API_TOLL, API_TOLL] } };
  assert.deepEqual(readTransactionAnswer(body), { totalCount: 2, rows: [API_TOLL, API_TOLL] });
  assert.deepEqual(readTransactionAnswer({ transactionList: { totalCount: 0 } }), { totalCount: 0, rows: [] });
  assert.deepEqual(readTransactionAnswer({ transactionList: { transaction: [API_TOLL] } }), { totalCount: 1, rows: [API_TOLL] });
  assert.equal(readTransactionAnswer({ message: "error" }), null);
  assert.equal(readTransactionAnswer(null), null);
});

test("a window is read on until the total is in hand", () => {
  const page = (n: number, total: number) => ({ totalCount: total, rows: Array.from({ length: n }, () => API_TOLL) });
  assert.equal(nextStartIndex(500, page(500, 700), 1), 501);
  assert.equal(nextStartIndex(700, page(200, 700), 2), null);
  assert.equal(nextStartIndex(3, page(3, 3), 1), null);
  // An empty page stops it, whatever the total claims.
  assert.equal(nextStartIndex(500, page(0, 900), 2), null);
  // And so does the call limit.
  assert.equal(nextStartIndex(500, page(500, 99999), API_MAX_CALLS), null);
  assert.equal(nextStartIndex(500, page(500, 99999), API_MAX_CALLS - 1), 501);
});
