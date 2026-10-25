/**
 * Turning E-ZPass account activity into the CSV the API already reads.
 *
 * Why the extension and not the server: www.e-zpassny.com sits behind Imperva
 * bot protection — an unauthenticated curl gets a JavaScript challenge rather
 * than HTML. A headless browser on Render would be arguing with that WAF, and
 * it would need the operator's E-ZPass password and a way past whatever
 * two-factor step the login does. The browser already logged in is none of
 * those things: the session is theirs, and the calls are a real browser's
 * because they are made from one.
 *
 * Everything here is a pure function over plain data, imported by the service
 * worker and by `tolls.test.ts`.
 *
 * Why it emits the download's CSV format rather than posting rows: the Python
 * side already has a tested parser for an E-ZPass statement, with column-name
 * aliases, the payment-row skip, and the sign convention. A second parser here
 * would be a second thing to get wrong.
 */

import type { ImportResult } from "./types.js";

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------
export function cleanCell(value: string): string {
  // Table cells wrap, carry non-breaking spaces and sometimes a second line of
  // small print. The CSV wants one line.
  return value.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}

export function toCsv(headers: string[], rows: string[][]): string {
  const escape = (value: string): string =>
    /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  const lines = [headers.map(cleanCell).map(escape).join(",")];
  for (const row of rows) lines.push(row.map(escape).join(","));
  return lines.join("\n") + "\n";
}

// ---------------------------------------------------------------------------
// Date ranges
// ---------------------------------------------------------------------------
// E-ZPass answers for at most three months at a time. So the statement is read
// in windows, newest first (an interrupted run has then got the part most likely
// to still be unbilled), each posted before the next is started.
//
// Dates are "YYYY-MM-DD" strings throughout. A `Date` would drag the machine's
// time zone into arithmetic that is only ever about calendar days.

export interface DateRange {
  /** Inclusive. */
  from: string;
  to: string;
}

/**
 * The longest range E-ZPass's filter accepts, in days, both ends included.
 *
 * Found from a real run, not assumed. Windows of 90 and 91 days (end minus
 * start 89 and 90) were accepted; one of 92 days (91 apart) left the page
 * showing the previous range. "Three months" is 91 or 92 days depending on the
 * month, so calendar quarters were over the limit about half the time.
 */
export const WINDOW_DAYS = 90;
/** A "month" of history, for sizing the read. Deliberately not calendar months. */
export const DAYS_PER_MONTH = 30;
/**
 * How far back a read goes unless told otherwise: one window. Everything older
 * is already on file after the first read, and re-reading a year of it on every
 * run is a burst of requests to a site that has begun refusing them.
 */
export const HISTORY_MONTHS = 3;

// ---------------------------------------------------------------------------
// Being gentle with the site
// ---------------------------------------------------------------------------
// The operator's own account in their own browser, but still a script clicking
// faster than a person, on a site that answered with "you look like a bot, or we
// are down". The response is to ask for less, slower, and to stop at the first
// refusal — not to look less like a script.

/** Pause before each click that makes the site fetch something. */
export const PACE_MS = 2000;
/** Pause between date windows. */
export const WINDOW_PACE_MS = 4000;
/** After a refusal, do not touch the site at all for this long. */
export const BLOCK_PAUSE_MS = 60 * 60_000;

const BLOCK_PHRASES = new RegExp(
  [
    "unusual (?:activity|traffic)",
    "automated (?:access|requests?|traffic|activity)",
    "suspicious",
    "\\bbots?\\b",
    "robot",
    "captcha",
    "access denied",
    "request (?:blocked|rejected|denied)",
    "(?:has been|was|been) blocked",
    "too many (?:requests|attempts)",
    "rate limit",
    "temporarily unavailable",
    "(?:service|site|system) (?:is )?(?:currently )?(?:un)?available",
    "unavailable",
    "try again later",
    "scheduled maintenance",
    "outage",
    "something went wrong",
    "error\\s*(?:code)?:?\\s*5\\d\\d",
  ].join("|"),
  "i"
);

/**
 * Whether a page's text reads like a refusal or an outage, and if so the few
 * words around it, with digits masked.
 *
 * Returns the *snippet*, not just a yes: a block page's wording is the only way
 * to tell "you look automated" from "we are down", and which of those it is
 * decides what to do next. Only the neighbourhood of the match is returned, so
 * a page header with an account holder's name is not carried out with it.
 */
export function looksBlocked(text: string): string | null {
  const flat = text.replace(/\s+/g, " ");
  const match = BLOCK_PHRASES.exec(flat);
  if (!match) return null;
  const start = Math.max(0, match.index - 70);
  return flat.slice(start, match.index + match[0].length + 90).replace(/\d/g, "#");
}

/**
 * A reason not to start a run now, or null.
 *
 * Only one: a recent refusal by the site. There used to be a minimum gap
 * between runs as well; it was removed at the operator's request, and the pause
 * after a refusal is what remains. Checked before the page is touched, so a
 * run refused here makes no requests.
 */
export function runGate(now: number, blockedUntil: number | null): string | null {
  if (blockedUntil !== null && now < blockedUntil) {
    const minutes = Math.ceil((blockedUntil - now) / 60_000);
    return `paused for another ${minutes} minute(s) because the last run met what looked like a block or outage`;
  }
  return null;
}

function parseIso(iso: string): { y: number; m: number; d: number } {
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}

function formatIso(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** `iso` moved by whole months, the day clamped to the target month's length. */
export function addMonths(iso: string, months: number): string {
  const { y, m, d } = parseIso(iso);
  const index = y * 12 + (m - 1) + months;
  const year = Math.floor(index / 12);
  const month = index - year * 12; // 0-based
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  return formatIso(Date.UTC(year, month, Math.min(d, lastDay)));
}

export function addDays(iso: string, days: number): string {
  const { y, m, d } = parseIso(iso);
  return formatIso(Date.UTC(y, m - 1, d + days));
}

/**
 * Consecutive windows of at most `WINDOW_DAYS` ending on `today`, newest first.
 *
 * Each window starts the day after the previous (older) one ends, both ends
 * inclusive, so together they cover the history exactly once: a gap would be a
 * crossing never read, and an overlap only costs the API a fingerprint check.
 * History is sized as 30-day months so that 3 months is one window and 9 is
 * three, rather than four with a 3-day tail.
 */
export function quarterRanges(
  today: string,
  historyMonths = HISTORY_MONTHS,
  windowDays = WINDOW_DAYS
): DateRange[] {
  const count = Math.max(1, Math.ceil((historyMonths * DAYS_PER_MONTH) / windowDays));
  const ranges: DateRange[] = [];
  let to = today;
  for (let i = 0; i < count; i++) {
    const from = addDays(to, -(windowDays - 1));
    ranges.push({ from, to });
    to = addDays(from, -1);
  }
  return ranges;
}

/** One window's outcome, for the popup to say what was and was not read. */
export interface WindowReport {
  from: string;
  to: string;
  rows: number;
  /** Set when the API refused this window's CSV, verbatim. */
  problem?: string;
}

/** Counts added across windows; the tag list is a union. */
export function sumImportResults(results: ImportResult[]): ImportResult {
  const total: ImportResult = {
    rows: 0,
    imported: 0,
    already_known: 0,
    matched: 0,
    unmatched: 0,
    unknown_tags: [],
  };
  for (const r of results) {
    total.rows += r.rows;
    total.imported += r.imported;
    total.already_known += r.already_known;
    total.matched += r.matched;
    total.unmatched += r.unmatched;
    for (const tag of r.unknown_tags) {
      if (!total.unknown_tags.includes(tag)) total.unknown_tags.push(tag);
    }
  }
  return total;
}

/**
 * What a Read E-ZPass command from the site reports back, in one line.
 *
 * Run from the phone, with nobody at the computer to see the popup, so the
 * line has to say what happened and, when nothing did, the likeliest reason:
 * an E-ZPass page that is not logged in shows no activity at all.
 */
export function describeTollsCommand(run: {
  result: { rows: number; imported: number; already_known: number; matched: number } | null;
  source: string;
  problem?: string;
  windows?: unknown[];
}): { ok: boolean; result: string } {
  if (run.problem) return { ok: false, result: run.problem };
  if (run.result) {
    const r = run.result;
    const windows = run.windows?.length;
    return {
      ok: true,
      result:
        `${r.rows} rows read${windows ? ` over ${windows} date range${windows === 1 ? "" : "s"}` : ""}` +
        ` · ${r.imported} new · ${r.already_known} already on file · ${r.matched} attributed`,
    };
  }
  return {
    ok: false,
    result:
      "nothing came back from E-ZPass — it is probably logged out. Log in at e-zpassny.com " +
      "on the computer, then try again.",
  };
}

// ---------------------------------------------------------------------------
// Reading through the site's own API
// ---------------------------------------------------------------------------
// The Account Activity page fills its table from one JSON call, made with the
// bearer token the site's session route hands out. Making that call ourselves,
// from inside any e-zpassny.com page, needs only that the operator is logged in
// — no particular page open, no form to drive, no pager to click through.
//
// What comes back carries the account holder's name and address on every row.
// Only the columns of the site's own CSV download are kept, so nothing else
// leaves the page.

/** Rows asked for per call. The site gave every row in one answer at 500. */
export const API_PAGE = 500;
/** Calls per window at most, so a misreported total cannot loop forever. */
export const API_MAX_CALLS = 20;

/** "2026-10-25" as "10/25/2026", the way the API wants its dates. */
export function apiDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${m}/${d}/${y}`;
}

/** The body of one transaction call: `startIndex` counts rows from 1. */
export function transactionRequest(range: DateRange, startIndex = 1, count = API_PAGE): Record<string, unknown> {
  return {
    agency: "",
    startIndex: String(startIndex),
    count,
    startDate: apiDate(range.from),
    endDate: apiDate(range.to),
    searchDate: "Transaction date",
    type: "",
    transponderOrPlateNumber: "",
    transactionType: "ALL",
  };
}

export interface ApiPage {
  totalCount: number;
  rows: Record<string, unknown>[];
}

/** The rows and the total out of an answer, or null when it is not one. */
export function readTransactionAnswer(body: unknown): ApiPage | null {
  if (!body || typeof body !== "object") return null;
  const list = (body as { transactionList?: unknown }).transactionList;
  if (!list || typeof list !== "object") return null;
  const raw = (list as { transaction?: unknown }).transaction;
  // A window with no activity has been seen to leave the list out entirely.
  const rows = Array.isArray(raw) ? raw.filter((r): r is Record<string, unknown> => !!r && typeof r === "object") : [];
  const total = Number((list as { totalCount?: unknown }).totalCount);
  return { totalCount: Number.isFinite(total) ? total : rows.length, rows };
}

/** Where the next call starts, or null when the window is read. */
export function nextStartIndex(read: number, page: ApiPage, calls: number): number | null {
  if (!page.rows.length || read >= page.totalCount || calls >= API_MAX_CALLS) return null;
  return read + 1;
}

/** "17:13:32.000" as "05:13:32 PM", the download's time format. */
export function apiTime(value: string): string {
  const match = /^(\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(value.trim());
  if (!match) return "";
  const hour = Number(match[1]);
  const twelve = hour % 12 === 0 ? 12 : hour % 12;
  return `${String(twelve).padStart(2, "0")}:${match[2]}:${match[3] ?? "00"} ${hour < 12 ? "AM" : "PM"}`;
}

/** -2.86 as "$-2.86", the download's amount format and sign. */
export function apiAmount(value: unknown): string {
  const cleaned = typeof value === "number" ? String(value) : String(value ?? "").replace(/[$,\s]/g, "");
  const number = cleaned === "" ? NaN : Number(cleaned);
  if (!Number.isFinite(number)) return "";
  return `$${number < 0 ? "-" : ""}${Math.abs(number).toFixed(2)}`;
}

/** The columns of the site's CSV download, which the API parser reads. */
export const API_CSV_HEADER = [
  "Lane Txn ID",
  "Tag/Plate #",
  "Agency",
  "Entry Plaza",
  "Exit Plaza",
  "Class",
  "Date",
  "Exit Time",
  "Amount",
];

const text = (value: unknown): string => (value === null || value === undefined ? "" : String(value).trim());

/**
 * The crossings in an answer, as the download's CSV.
 *
 * Only "TOLL POSTING" rows: payments, CBD credits and dispute credits are
 * money coming back, and parking and the stolen-tag fee are not crossings a
 * guest drove through. The lane transaction id is the key — it is the "Lane
 * Txn ID" the page shows; `transactionNumber` is a different number.
 */
export function apiRowsToCsv(rows: Record<string, unknown>[]): { csv: string; rowCount: number; skipped: number } {
  const kept: string[][] = [];
  for (const row of rows) {
    if (text(row.activity).toUpperCase() !== "TOLL POSTING") continue;
    kept.push([
      text(row.laneTransactionId),
      text(row.tagOrPlateNumber),
      text(row.agency),
      text(row.entryPlaza),
      text(row.exitPlaza),
      text(row.vehicleClass),
      text(row.exitDate),
      apiTime(text(row.exitTime)),
      apiAmount(row.amount),
    ]);
  }
  return { csv: toCsv(API_CSV_HEADER, kept), rowCount: kept.length, skipped: rows.length - kept.length };
}
