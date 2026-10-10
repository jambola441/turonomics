/**
 * Turning an E-ZPass account-activity page into the CSV the API already reads.
 *
 * Why the extension and not the server: www.e-zpassny.com sits behind Imperva
 * bot protection — an unauthenticated curl gets a JavaScript challenge rather
 * than HTML. A headless browser on Render would be arguing with that WAF, and
 * it would need the operator's E-ZPass password and a way past whatever
 * two-factor step the login does. The browser already logged in is none of
 * those things: the session is theirs, the click is theirs, and the page is a
 * real browser because it is one.
 *
 * Why this file is separate from the content script: content scripts cannot use
 * ES modules, so anything in one is untestable without a browser. Everything
 * here is a pure function over plain data, imported by the service worker and
 * by `tolls.test.ts`. The content script's whole job is to read the DOM into
 * `ScrapedTable`s and hand them over.
 *
 * Why it emits the download's CSV format rather than parsing the rows: the
 * Python side already has a tested parser for an E-ZPass statement, with
 * column-name aliases, the payment-row skip, and the sign convention. A second
 * parser here would be a second thing to get wrong. If the page's column names
 * are ones the API does not know, the import fails with a 422 that lists the
 * names it found — which is how to learn them, rather than by guessing.
 */

import type { ImportResult } from "./types.js";

export interface ScrapedTable {
  /** Header cell text, verbatim. These are column names, not account data. */
  headers: string[];
  /** Body rows, cell text, verbatim. */
  rows: string[][];
}

export interface ScrapedPage {
  url: string;
  tables: ScrapedTable[];
  /** Hrefs that look like a CSV/Excel export, best first. */
  downloadLinks: string[];
  /** Set when the worker fetched a download link itself. */
  downloadedCsv?: string;
  /**
   * The activity table assembled from every page that was read, when more than
   * one was. Preferred over `tables` so a paged statement arrives whole.
   */
  merged?: ScrapedTable;
  /** How many pages were read, and why reading stopped. */
  pagesRead?: number;
  pagingStopped?: string;
}

/** What `chooseStatement` concluded, so the popup can say something useful. */
export interface StatementChoice {
  csv: string | null;
  /** Where it came from, for the popup and the logs. */
  source: "download" | "table" | "none";
  /** Matched column names, for when the API rejects the headers. */
  headers: string[];
  rowCount: number;
  /**
   * True when every amount on the chosen table reads as zero or positive.
   *
   * This matters more than it looks. An E-ZPass download writes charges as
   * negative ("$-2.86") and the API skips anything positive as a payment, so a
   * page that displays charges positive would import as nothing at all. Not
   * corrected here: an activity table mixes tolls with replenishments, so
   * flipping every sign would turn payments into charges and bill a guest for
   * one. Reported instead, loudly, and the import's own "read 40, imported 0"
   * says the same thing from the other end.
   */
  amountsLookPositive: boolean;
}

// ---------------------------------------------------------------------------
// Column recognition
// ---------------------------------------------------------------------------
// Deliberately loose. The job is not to parse the column but to recognise that
// a table is the activity table, and the API's own alias list decides the rest.
const COLUMN_PATTERNS: Record<string, RegExp> = {
  date: /\bdate\b|posted|transaction\s*date/i,
  amount: /amount|debit|credit|charge|\bfee\b|\btoll\b|balance/i,
  tagPlate: /\btag\b|plate|transponder|vehicle/i,
  plaza: /plaza|exit|entry|\bpoint\b|agency|location|facility/i,
};

/** The columns without which a table cannot be a statement. */
const REQUIRED = ["date", "amount", "tagPlate"] as const;

export function classifyHeaders(headers: string[]): Set<string> {
  const found = new Set<string>();
  for (const header of headers) {
    for (const [name, pattern] of Object.entries(COLUMN_PATTERNS)) {
      if (pattern.test(header)) found.add(name);
    }
  }
  return found;
}

/**
 * The table most likely to be the account activity, or null.
 *
 * Structural rather than selector-based on purpose: a CSS class can be
 * rewritten by a deploy, but the activity table will always be the table with a
 * date, an amount and a tag or plate in its header and the most rows under it.
 */
export function pickActivityTable(tables: ScrapedTable[]): ScrapedTable | null {
  let best: ScrapedTable | null = null;
  let bestScore = -1;
  for (const table of tables) {
    if (!table.rows.length) continue;
    const found = classifyHeaders(table.headers);
    if (!REQUIRED.every((name) => found.has(name))) continue;
    // More recognised columns wins; a tie goes to the longer table, because a
    // summary box can carry the same headers over a single row.
    const score = found.size * 1000 + Math.min(table.rows.length, 999);
    if (score > bestScore) {
      best = table;
      bestScore = score;
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Row cleanup
// ---------------------------------------------------------------------------
export function cleanCell(value: string): string {
  // Table cells wrap, carry non-breaking spaces and sometimes a second line of
  // small print. The CSV wants one line.
  return value.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}

/**
 * Rows worth writing out.
 *
 * Drops the ones a table has that a CSV does not: a "no records found" row
 * spanning every column, a totals row, and anything whose cell count does not
 * line up with the header. A short row is not repaired — a row whose columns
 * cannot be identified is a row whose amount cannot be either.
 */
export function usableRows(table: ScrapedTable): string[][] {
  const width = table.headers.length;
  return table.rows
    .map((row) => row.map(cleanCell))
    .filter((row) => row.length === width && row.some((cell) => cell !== ""));
}

export function toCsv(headers: string[], rows: string[][]): string {
  const escape = (value: string): string =>
    /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  const lines = [headers.map(cleanCell).map(escape).join(",")];
  for (const row of rows) lines.push(row.map(escape).join(","));
  return lines.join("\n") + "\n";
}

const AMOUNT_HEADER = /amount|debit|credit|charge|\bfee\b|\btoll\b/i;

/** True when no amount cell on the table carries a minus sign. */
export function amountsLookPositive(headers: string[], rows: string[][]): boolean {
  const columns = headers
    .map((header, index) => (AMOUNT_HEADER.test(header) ? index : -1))
    .filter((index) => index >= 0);
  if (!columns.length) return false;
  let sawANumber = false;
  for (const row of rows) {
    for (const index of columns) {
      const cell = row[index] ?? "";
      if (!/\d/.test(cell)) continue;
      sawANumber = true;
      // "($2.86)" is accounting for negative, and so is a leading or trailing
      // minus. Any of them means the page does distinguish charges from
      // credits and there is nothing to warn about.
      if (/-|\(.*\d.*\)/.test(cell)) return false;
    }
  }
  return sawANumber;
}

/**
 * The statement to send, from whatever the page offered.
 *
 * A download the site generated beats a table scraped out of the DOM every
 * time: it is the format the API's parser was written against, it carries
 * columns the page may not show, and it is not paginated. The table is the
 * fallback for when no such link exists.
 */
export function chooseStatement(page: ScrapedPage): StatementChoice {
  if (page.downloadedCsv && page.downloadedCsv.trim()) {
    const [firstLine = ""] = page.downloadedCsv.split(/\r?\n/);
    const lines = page.downloadedCsv.split(/\r?\n/).filter((line) => line.trim());
    return {
      csv: page.downloadedCsv,
      source: "download",
      headers: firstLine.split(",").map((h) => h.replace(/^"|"$/g, "").trim()),
      rowCount: Math.max(lines.length - 1, 0),
      amountsLookPositive: false,
    };
  }

  const table = page.merged ?? pickActivityTable(page.tables);
  if (!table) {
    return { csv: null, source: "none", headers: [], rowCount: 0, amountsLookPositive: false };
  }
  const rows = usableRows(table);
  if (!rows.length) {
    return {
      csv: null,
      source: "none",
      headers: table.headers.map(cleanCell),
      rowCount: 0,
      amountsLookPositive: false,
    };
  }
  const headers = table.headers.map(cleanCell);
  return {
    csv: toCsv(headers, rows),
    source: "table",
    headers,
    rowCount: rows.length,
    amountsLookPositive: amountsLookPositive(headers, rows),
  };
}


// ---------------------------------------------------------------------------
// Paging
// ---------------------------------------------------------------------------
// An account-activity page shows a few weeks at a time. Reading only what is on
// screen meant a statement had to be sent a page at a time, by hand, and a page
// missed was money never billed.
//
// The decisions live here; the clicking lives in the service worker, because a
// "next" link that navigates tears down a content script half way through the
// loop. These functions see descriptors of the controls, never elements, which
// is what makes them testable without a browser.

export interface ControlDescriptor {
  text: string;
  rel?: string;
  ariaLabel?: string;
  /** Disabled by attribute, aria-disabled, or a class saying so. */
  disabled?: boolean;
  className?: string;
}

export interface PageSizeOption {
  value: string;
  label: string;
}

// Jumps to the last page rather than the next one, so paging would skip every
// page in between and the crossings on them.
//
// Checked before anything scores, so it beats an aria-label claiming "next".
// That is a deliberate asymmetry: treating a real next as last stops paging
// early, which is reported and can be finished by hand, while treating a real
// last as next silently skips pages and reads as a complete statement.
//
// Two patterns, because the symbols have to be exact and the words must not be.
// Anchoring the words meant a control labelled "Last page" was not recognised
// at all — and since ">" on its own scores as next, an arrow with that label
// would have been clicked. Every test here had the text and the label agreeing,
// so nothing noticed.
const SKIPS_SYMBOL = /^(?:>>|\u00bb)$/;
const SKIPS_WORDS = /\b(?:last|jump to last|go to last)\b/i;

function skipsPages(value: string): boolean {
  const text = value.trim();
  return SKIPS_SYMBOL.test(text) || SKIPS_WORDS.test(text);
}

const NEXT_TEXT = /^(?:next|next page|next >|>|\u203a|\u2192)$/i;
const BACKWARDS = /prev|previous|back|first|earlier|<|\u2039|\u2190/i;

function disabledLooking(control: ControlDescriptor): boolean {
  if (control.disabled) return true;
  return /\b(?:disabled|inactive|is-disabled|pagination-disabled)\b/i.test(
    control.className ?? ""
  );
}

/**
 * Indices of the controls that plausibly advance one page, best first.
 *
 * Ranked rather than chosen, so the worker can try the next candidate when a
 * click changes nothing. Anything that reads as backwards is excluded, and so
 * is anything that jumps to the last page: paging one at a time is slower and
 * cannot skip a page of crossings.
 */
export function rankNextControls(controls: ControlDescriptor[]): number[] {
  const scored: { index: number; score: number }[] = [];
  controls.forEach((control, index) => {
    const text = control.text.trim();
    const label = (control.ariaLabel ?? "").trim();
    if (disabledLooking(control)) return;
    if (skipsPages(text) || skipsPages(label)) return;
    if (BACKWARDS.test(text) || BACKWARDS.test(label)) {
      // "<" and "previous" are not next, whatever else they match.
      if (!NEXT_TEXT.test(text) && !/next/i.test(label)) return;
    }
    let score = 0;
    if ((control.rel ?? "").toLowerCase() === "next") score = 100;
    else if (/^next\b|\bnext page\b/i.test(label)) score = 90;
    else if (NEXT_TEXT.test(text)) score = 80;
    else if (/next/i.test(text)) score = 50;
    else return;
    scored.push({ index, score });
  });
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  return scored.map((entry) => entry.index);
}

/**
 * The option that shows the most rows at once, or null to leave it alone.
 *
 * Worth doing before any clicking: a page-size control that offers "All" turns
 * a twelve-page statement into one, and every page not loaded is a page that
 * cannot go wrong.
 */
export function pickPageSize(options: PageSizeOption[]): string | null {
  let bestAll: PageSizeOption | null = null;
  let bestNumber: { option: PageSizeOption; n: number } | null = null;
  for (const option of options) {
    if (/^\s*(?:all|show all|everything)\s*$/i.test(option.label)) {
      bestAll = bestAll ?? option;
      continue;
    }
    const n = parseInt(option.label.replace(/[^\d]/g, ""), 10);
    if (!isNaN(n) && (bestNumber === null || n > bestNumber.n)) {
      bestNumber = { option, n };
    }
  }
  if (bestAll) return bestAll.value;
  return bestNumber ? bestNumber.option.value : null;
}

/**
 * A short string that changes when the table does.
 *
 * Used to tell "the next page loaded" from "the click did nothing", and to
 * notice a control that cycles back to the first page. Built from the ends and
 * the count rather than the whole table: enough to distinguish pages, cheap
 * enough to compare on every iteration.
 */
export function tableSignature(table: ScrapedTable | null): string {
  if (!table || !table.rows.length) return "empty";
  const first = (table.rows[0] ?? []).map(cleanCell).join("|");
  const last = (table.rows[table.rows.length - 1] ?? []).map(cleanCell).join("|");
  return `${table.rows.length}#${first}#${last}`;
}

export interface PagerRange {
  from: number;
  to: number;
  total: number;
}

/**
 * "1–10 of 96", as MUI's table pagination prints it, or null.
 *
 * The one authoritative statement of where in the result the page is. With it
 * the end is known rather than inferred from a control going missing, and a
 * stop short of the total can be called incomplete instead of "no next page" —
 * which is what a run that "stopped after 4 pages for no reason" looked like.
 */
export function parsePagerRange(text: string): PagerRange | null {
  const match = /^\s*(\d[\d,]*)\s*[\u2013\u2014-]\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s*$/i.exec(text);
  if (!match) return null;
  const n = (value: string): number => Number(value.replace(/,/g, ""));
  return { from: n(match[1]), to: n(match[2]), total: n(match[3]) };
}

/** The pager's own controls, described for the log when paging stops. */
export function describePager(controls: ControlDescriptor[]): string[] {
  const lines: string[] = [];
  controls.forEach((control, index) => {
    const label = control.ariaLabel ?? "";
    const cls = control.className ?? "";
    if (!/page|pagination/i.test(label) && !/pagination/i.test(cls)) return;
    lines.push(
      `    [${index}] text="${control.text.slice(0, 12)}" aria="${label}" ` +
        `disabled=${disabledLooking(control)} class="${cls.slice(0, 60)}"`
    );
  });
  return lines.length ? lines : ["    (no control looked like part of a pager)"];
}

/**
 * Whether a re-read table is a new page rather than the old one or a gap.
 *
 * "empty" is the table gone while the next page loads, and it differs from
 * every real signature — so a plain inequality called it a page. That is how
 * paging stopped at page one on E-ZPass with "the next page was empty".
 */
export function pageChanged(signature: string, before: string): boolean {
  return signature !== "empty" && signature !== before;
}

/** Beyond this many pages something is wrong, and it is not worth finding out
 * by clicking three hundred times on somebody's tolling account. */
export const MAX_PAGES = 40;

/**
 * Why to stop paging, or null to continue.
 *
 * A reason rather than a boolean, because the popup should be able to say "read
 * 9 pages" differently from "gave up after 40" — the second means rows may be
 * missing, and a reconciliation that silently stops early is worse than one
 * that says it stopped.
 */
export function shouldStopPaging(signatures: string[], maxPages = MAX_PAGES): string | null {
  if (signatures.length >= maxPages) return `stopped at ${maxPages} pages`;
  const latest = signatures[signatures.length - 1];
  if (latest === "empty" && signatures.length > 1) return "the next page was empty";
  if (signatures.indexOf(latest) < signatures.length - 1) {
    // Seen before: the control wrapped around, or stopped advancing.
    return "the page stopped changing";
  }
  return null;
}

/**
 * One table out of the pages read, or null if none of them was a statement.
 *
 * Pages whose headers disagree with the first are dropped rather than
 * concatenated: a different shape is a different table, and appending its rows
 * would put a plaza where an amount should be. Identical rows are collapsed —
 * paging controls re-render the same rows more often than an account is charged
 * twice in the same second, and the server's own fingerprint is the authority
 * either way.
 */
export function mergeActivityPages(pages: ScrapedTable[]): ScrapedTable | null {
  let headers: string[] | null = null;
  const rows: string[][] = [];
  const seen = new Set<string>();
  for (const page of pages) {
    const table = pickActivityTable([page]);
    if (!table) continue;
    const cleaned = table.headers.map(cleanCell);
    if (headers === null) headers = cleaned;
    else if (cleaned.join("|") !== headers.join("|")) continue;
    for (const row of usableRows(table)) {
      const key = row.join("\u0000");
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push(row);
    }
  }
  if (headers === null || !rows.length) return null;
  return { headers, rows };
}

// ---------------------------------------------------------------------------
// Date ranges
// ---------------------------------------------------------------------------
// An account-activity page answers for the range in its date fields, and a long
// range is slow or refused. So the statement is read three months at a time,
// newest first (an interrupted run has then got the part most likely to still be
// unbilled), each window paged through and posted before the next is started.
//
// Dates are "YYYY-MM-DD" strings throughout. A `Date` would drag the machine's
// time zone into arithmetic that is only ever about calendar days.

export interface DateRange {
  /** Inclusive. */
  from: string;
  to: string;
}

export const WINDOW_MONTHS = 3;
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
/** Refuse to start another run within this long of the last one. */
export const COOLDOWN_MS = 5 * 60_000;
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
 * Checked before the page is touched: a run refused here makes no requests.
 */
export function runGate(
  now: number,
  lastRun: number | null,
  blockedUntil: number | null
): string | null {
  if (blockedUntil !== null && now < blockedUntil) {
    const minutes = Math.ceil((blockedUntil - now) / 60_000);
    return `paused for another ${minutes} minute(s) because the last run met what looked like a block or outage`;
  }
  if (lastRun !== null && now - lastRun < COOLDOWN_MS) {
    const minutes = Math.ceil((COOLDOWN_MS - (now - lastRun)) / 60_000);
    return `the last run was under ${Math.round(COOLDOWN_MS / 60_000)} minutes ago; wait ${minutes} more minute(s)`;
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
 * Consecutive three-month windows ending on `today`, newest first.
 *
 * Each window starts the day after the previous (older) one ends, both ends
 * inclusive, so together they cover the history exactly once: a gap would be a
 * crossing never read, and an overlap only costs the API a fingerprint check.
 */
export function quarterRanges(
  today: string,
  historyMonths = HISTORY_MONTHS,
  windowMonths = WINDOW_MONTHS
): DateRange[] {
  const count = Math.max(1, Math.ceil(historyMonths / windowMonths));
  const ranges: DateRange[] = [];
  let to = today;
  for (let i = 0; i < count; i++) {
    const from = addDays(addMonths(to, -windowMonths), 1);
    ranges.push({ from, to });
    to = addDays(from, -1);
  }
  return ranges;
}

export interface InputDescriptor {
  /** Position among the page's inputs, so the worker can find it again. */
  index: number;
  type: string;
  /** name, id, label text, placeholder and aria-label, space-joined. */
  hint: string;
  placeholder?: string;
}

const FROM_HINT = /\b(?:from|start|begin|since)/i;
const TO_HINT = /\b(?:to|end|through|thru|until)\b/i;

/**
 * Which inputs are the start and end of the range, or null.
 *
 * Two inputs that both look like dates are taken in document order when
 * neither says which it is; one that does not look like a date is never picked.
 * Better to read what the page shows and say so than to type a date into a
 * search box.
 */
export function pickDateFields(
  inputs: InputDescriptor[]
): { from: InputDescriptor; to: InputDescriptor } | null {
  const dates = inputs.filter(
    (input) =>
      input.type === "date" ||
      (["text", "", "search"].includes(input.type) &&
        (/date/i.test(input.hint) || /mm\W*dd|yyyy/i.test(input.placeholder ?? "")))
  );
  const from = dates.find((input) => FROM_HINT.test(input.hint));
  const to = dates.find((input) => input !== from && TO_HINT.test(input.hint));
  if (from && to) return { from, to };
  if (!from && !to && dates.length === 2) return { from: dates[0], to: dates[1] };
  return null;
}

/**
 * The date as the field wants it.
 *
 * ISO for a date input or a yyyy-first placeholder; a two-digit year when the
 * placeholder ends in "YY" rather than "YYYY". E-ZPass's fields say MM/DD/YY,
 * and typing a four-digit year into a two-digit mask is read as year 20xx at
 * best and silently truncated at worst — the first version did exactly that.
 */
export function formatForField(iso: string, field: InputDescriptor): string {
  const shown = `${field.placeholder ?? ""} ${field.hint}`;
  if (field.type === "date" || /y{4}\W*m{2}/i.test(field.placeholder ?? "")) return iso;
  const { y, m, d } = parseIso(iso);
  const pad = (n: number): string => String(n).padStart(2, "0");
  const twoDigitYear = /d{2}\W*y{2}(?!y)/i.test(shown);
  return `${pad(m)}/${pad(d)}/${twoDigitYear ? pad(y % 100) : y}`;
}

/** "7/8/26" or "07/08/2026" as "2026-07-08"; null for anything else. */
export function parseSiteDate(text: string): string | null {
  const match = /^\s*(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})\s*$/.exec(text);
  if (!match) return null;
  const year = match[3].length === 2 ? 2000 + Number(match[3]) : Number(match[3]);
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${year}-${pad(Number(match[1]))}-${pad(Number(match[2]))}`;
}

/**
 * Whether a field holds the date that was typed into it.
 *
 * Compared as dates, not as text: E-ZPass's field is a masked date component
 * that rewrites "07/08/26" as "7‎/8‎/26" with invisible left-to-right marks
 * between the parts. That is the same date, and reporting it as a mismatch
 * sent a whole debugging round after the wrong thing.
 */
export function sameDate(read: string, typed: string): boolean {
  const clean = (value: string): string => value.replace(/[\u200e\u200f\u202a-\u202e]/g, "");
  const a = parseSiteDate(clean(read));
  return a !== null && a === parseSiteDate(clean(typed));
}

export interface DateCoverage {
  /** Earliest and latest row date, ISO; null when no row had a readable date. */
  first: string | null;
  last: string | null;
  /** Rows dated outside the range asked for. */
  outside: number;
  /** Rows with no readable date at all. */
  undated: number;
  total: number;
}

/**
 * Where the rows actually fall, against the range that was asked for.
 *
 * This is the check that the date filter did what it was told. A search that
 * ignores its dates still returns a table, and every row in it still imports
 * as "already known" — so without looking at the dates, a read that covered one
 * range four times is indistinguishable from one that covered four.
 */
export function dateCoverage(table: ScrapedTable | null, range: DateRange): DateCoverage {
  const result: DateCoverage = { first: null, last: null, outside: 0, undated: 0, total: 0 };
  if (!table) return result;
  let column = table.headers.findIndex((h) => /^\s*date\s*$/i.test(h));
  if (column < 0) column = table.headers.findIndex((h) => /date/i.test(h) && !/time/i.test(h));
  for (const row of table.rows) {
    result.total++;
    const iso = column >= 0 ? parseSiteDate(cleanCell(row[column] ?? "")) : null;
    if (!iso) {
      result.undated++;
      continue;
    }
    if (result.first === null || iso < result.first) result.first = iso;
    if (result.last === null || iso > result.last) result.last = iso;
    if (iso < range.from || iso > range.to) result.outside++;
  }
  return result;
}

const SUBMIT_TEXT = /^(?:search|submit|go|view|apply|filter|show|update|display|get|find|refresh)\b/i;
const NOT_SUBMIT = /export|download|print|reset|clear|cancel|prev|next|last|first|logout|log out/i;

/**
 * Indices of the controls that plausibly run the date search, best first.
 *
 * Ranked so the worker has an order to try; anything that exports, resets or
 * pages is excluded outright, since clicking "Clear" after typing the dates
 * would put the old range back and read it as the new one.
 */
export function rankSubmitControls(controls: ControlDescriptor[]): number[] {
  const scored: { index: number; score: number }[] = [];
  controls.forEach((control, index) => {
    const text = control.text.trim();
    const label = (control.ariaLabel ?? "").trim();
    if (disabledLooking(control)) return;
    if (NOT_SUBMIT.test(text) || NOT_SUBMIT.test(label)) return;
    let score = 0;
    if (SUBMIT_TEXT.test(text)) score = 100;
    else if (SUBMIT_TEXT.test(label)) score = 90;
    else return;
    scored.push({ index, score });
  });
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  return scored.map((entry) => entry.index);
}

/**
 * The form as the run saw it, for a log meant to be pasted into a bug report.
 *
 * Lists only the date-looking inputs and the controls that ranked as a search
 * button. The page's other links and buttons are left out: a nav bar can carry
 * an account holder's name, and what was wrong is almost always in the fields.
 */
export function describeForm(
  inputs: InputDescriptor[],
  controls: ControlDescriptor[]
): string[] {
  const lines = [`inputs on page: ${inputs.length}, controls: ${controls.length}`];
  const fields = pickDateFields(inputs);
  const dateLike = inputs.filter(
    (i) => i.type === "date" || /date|from|to\b|start|end/i.test(i.hint) || i.placeholder
  );
  for (const input of dateLike) {
    const role = fields?.from === input ? "FROM" : fields?.to === input ? "TO" : "-";
    lines.push(
      `  input[${input.index}] type=${input.type} ${role} hint="${input.hint}" placeholder="${input.placeholder ?? ""}"`
    );
  }
  if (!fields) lines.push("  no from/to pair recognised");
  const ranked = rankSubmitControls(controls);
  for (const index of ranked.slice(0, 3)) {
    lines.push(`  submit candidate [${index}] "${controls[index].text.slice(0, 40)}"`);
  }
  if (!ranked.length) lines.push("  no search button recognised");
  return lines;
}

/**
 * Every control that reads like a search button, disabled or not, for the log.
 *
 * `rankSubmitControls` drops a disabled button, which is right for choosing
 * one and wrong for explaining why a search never ran: "Filter is disabled
 * after typing the dates" and "there is no Filter button" are different
 * problems, and the ranking alone cannot tell them apart.
 */
export function describeSubmitCandidates(controls: ControlDescriptor[]): string[] {
  const lines: string[] = [];
  controls.forEach((control, index) => {
    const text = control.text.trim();
    const label = (control.ariaLabel ?? "").trim();
    if (!SUBMIT_TEXT.test(text) && !SUBMIT_TEXT.test(label) && !/filter|search/i.test(`${text} ${label}`)) {
      return;
    }
    lines.push(
      `    [${index}] text="${text.slice(0, 20)}" aria="${label.slice(0, 30)}" ` +
        `disabled=${disabledLooking(control)} excluded=${NOT_SUBMIT.test(text) || NOT_SUBMIT.test(label)}`
    );
  });
  return lines.length ? lines : ["    (nothing on the page reads like a search button)"];
}

/**
 * Whether a search took, judged by what it returned.
 *
 * Rows dated outside the window mean the page is still showing some earlier
 * range — a search that never ran leaves the previous window's last page up,
 * and reading that as this window's data is what this exists to prevent. An
 * empty result is a legitimate window, so it passes; the dates cannot speak.
 */
export function windowApplied(coverage: DateCoverage): boolean {
  // Most rows, not any: a crossing posted a day outside the range is ordinary,
  // while a search that never ran leaves the *whole* previous window up.
  return coverage.outside * 2 < coverage.total || coverage.total === 0;
}

/** One window's outcome, for the popup to say what was and was not read. */
export interface WindowReport {
  from: string;
  to: string;
  rows: number;
  pagesRead?: number;
  pagingStopped?: string;
  /** Set when the API refused this window's CSV, verbatim. */
  problem?: string;
  /** Rows dated outside the window: the date filter did not take. */
  outside?: number;
  /** "first to last" date actually seen. */
  span?: string;
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

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------
/**
 * Mask the values but keep their shape.
 *
 * For when nothing on the page matched and the markup has to be described back
 * to somebody who cannot see it. Digits become `#` and letters `a`, so a tag
 * number, a plate and a dollar amount are each recognisable as a format
 * without being readable as data. Header text is left alone: a column name is
 * not account data, and it is the whole thing worth knowing.
 */
export function maskValue(value: string): string {
  return cleanCell(value).replace(/\d/g, "#").replace(/[A-Za-z]/g, "a");
}

export function describeShape(page: ScrapedPage): string {
  const lines = [`url: ${page.url.replace(/\?.*$/, "")}`];
  lines.push(`download links: ${page.downloadLinks.length}`);
  for (const href of page.downloadLinks.slice(0, 5)) {
    lines.push(`  ${href.replace(/\?.*$/, "")}`);
  }
  lines.push(`tables: ${page.tables.length}`);
  page.tables.forEach((table, index) => {
    const found = [...classifyHeaders(table.headers)].sort().join(",") || "none";
    lines.push(`  [${index}] ${table.rows.length} rows, recognised: ${found}`);
    lines.push(`      headers: ${table.headers.map(cleanCell).join(" | ")}`);
    const sample = table.rows[0];
    if (sample) lines.push(`      shape:   ${sample.map(maskValue).join(" | ")}`);
  });
  return lines.join("\n");
}
