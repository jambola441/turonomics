/**
 * Background service worker (Manifest V3).
 *
 * Responsibilities:
 * - Receive trip data from the popup (which relays it from the content script)
 * - Assemble a CSV string
 * - Trigger a file download via chrome.downloads
 * - Turn a scraped E-ZPass page into a statement and post it to the API
 *
 * The E-ZPass half lives here rather than in the content script because the
 * logic has to be testable, and a content script cannot import a module. The
 * content script reads the DOM; this decides what the DOM meant.
 */

import {
  chooseStatement,
  dateCoverage,
  describeForm,
  BLOCK_PAUSE_MS,
  describePager,
  describeSubmitCandidates,
  HISTORY_MONTHS,
  nextIsDisabled,
  looksBlocked,
  PACE_MS,
  runGate,
  WINDOW_PACE_MS,
  windowApplied,
  parsePagerRange,
  type PagerRange,
  describeShape,
  formatForField,
  maskValue,
  mergeActivityPages,
  pageChanged,
  pickDateFields,
  pickPageSize,
  quarterRanges,
  sameDate,
  rankNextControls,
  rankSubmitControls,
  shouldStopPaging,
  sumImportResults,
  tableSignature,
  usableRows,
  type ControlDescriptor,
  type DateRange,
  type InputDescriptor,
  type PageSizeOption,
  type ScrapedPage,
  type ScrapedTable,
  type WindowReport,
} from "./tolls.js";
import {
  describeEmbedded,
  listPath,
  nextPage,
  describeFiling,
  describePull,
  hubsToRead,
  invoicePath,
  invoicesToRead,
  reimbursementRequestPath,
  summariseCalls,
  EZPASS_SUMMARY,
  type SummaryOptions,
  type Embedded,
  type SeenCall,
} from "./turo.js";
import type {
  Draft,
  FileInvoiceResult,
  SiteCommand,
  TuroDiscovery,
  TuroInvoicesResult,
  TuroPullResult,
  TuroWanted,
} from "./types.js";
import type { ImportResult, MessageType, SendTollsResult, TuroTrip } from "./types.js";

// ---------------------------------------------------------------------------
// CSV assembly
// ---------------------------------------------------------------------------
const CSV_HEADER = "trip_id,start_time,end_time,license_plate";

function escapeField(value: string): string {
  if (value.includes(",") || value.includes('"') || value.includes("\n")) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function tripsToCSV(trips: TuroTrip[]): string {
  const rows = trips.map((t) =>
    [t.tripId, t.startTime, t.endTime, t.licensePlate]
      .map(escapeField)
      .join(",")
  );
  return [CSV_HEADER, ...rows].join("\n");
}

const LOG = (...args: unknown[]) => console.log("[turonomics:bg]", ...args);

/**
 * The E-ZPass run's own log, handed back to the popup.
 *
 * Separate from the console because the console is in a service worker most
 * people cannot find, and a run that went wrong should be debuggable from one
 * paste. Nothing here may carry account data: row counts and masked shapes
 * only, never a cell.
 */
let trace: string[] = [];
let traceStart = 0;
function note(line: string): void {
  trace.push(`+${((Date.now() - traceStart) / 1000).toFixed(1)}s ${line}`);
  LOG(line);
}
function startTrace(): void {
  trace = [];
  traceStart = Date.now();
}

// ---------------------------------------------------------------------------
// Download helper
// ---------------------------------------------------------------------------
function downloadCSV(csv: string): void {
  // NOTE: URL.createObjectURL is not available in MV3 service workers.
  // Use a data URL instead — chrome.downloads accepts it fine.
  const dataUrl = "data:text/csv;charset=utf-8," + encodeURIComponent(csv);
  const filename = `turo-trips-${new Date().toISOString().slice(0, 10)}.csv`;

  LOG("CSV content that will be downloaded:\n" + csv);

  chrome.downloads.download({ url: dataUrl, filename, saveAs: false }, (downloadId) => {
    if (chrome.runtime.lastError) {
      LOG("download error:", chrome.runtime.lastError.message);
    } else {
      LOG("download started, id:", downloadId, "filename:", filename);
    }
  });
}

// ---------------------------------------------------------------------------
// Detail scraper — opens a hidden tab, waits for SPA render, scrapes times
// ---------------------------------------------------------------------------
function fetchDetailInTab(
  tripId: string
): Promise<{ scheduleDates: string[]; scheduleTimes: string[] }> {
  return new Promise((resolve) => {
    const url = `https://turo.com/us/en/reservation/${tripId}`;
    LOG(`fetchDetailInTab[${tripId}] — creating tab for ${url}`);

    chrome.tabs.create({ url, active: false }, (tab) => {
      const tabId = tab.id!;

      const onUpdated = (
        updatedId: number,
        info: chrome.tabs.TabChangeInfo
      ) => {
        if (updatedId !== tabId || info.status !== "complete") return;
        chrome.tabs.onUpdated.removeListener(onUpdated);

        // Poll for schedule elements — the SPA needs time to render after load
        chrome.scripting.executeScript(
          {
            target: { tabId },
            func: () =>
              new Promise<{ dates: string[]; times: string[] }>((res) => {
                let attempts = 0;
                const poll = () => {
                  const dates = Array.from(
                    document.querySelectorAll("[data-testid='schedule-date']")
                  ).map((el) => el.textContent?.trim() ?? "");
                  const times = Array.from(
                    document.querySelectorAll("[data-testid='schedule-time']")
                  ).map((el) => el.textContent?.trim() ?? "");
                  if (dates.length >= 2 && times.length >= 2) {
                    res({ dates, times });
                  } else if (attempts++ < 20) {
                    setTimeout(poll, 500);
                  } else {
                    res({ dates: [], times: [] });
                  }
                };
                poll();
              }),
          },
          (results) => {
            chrome.tabs.remove(tabId);
            if (chrome.runtime.lastError || !results?.[0]?.result) {
              LOG(`fetchDetailInTab[${tripId}] — script error:`, chrome.runtime.lastError?.message);
              resolve({ scheduleDates: [], scheduleTimes: [] });
            } else {
              const { dates, times } = results[0].result as { dates: string[]; times: string[] };
              LOG(`fetchDetailInTab[${tripId}] — dates:`, dates, "times:", times);
              resolve({ scheduleDates: dates, scheduleTimes: times });
            }
          }
        );
      };

      chrome.tabs.onUpdated.addListener(onUpdated);
    });
  });
}


// ---------------------------------------------------------------------------
// E-ZPass → the API
// ---------------------------------------------------------------------------
const DEFAULT_API = "https://turonomics.onrender.com";

interface Settings {
  apiBase: string;
  tollsToken: string;
  /** How many months of E-ZPass activity a run reads. */
  tollsMonths: number;
}

async function settings(): Promise<Settings> {
  const stored = await chrome.storage.local.get(["apiBase", "tollsToken", "tollsMonths"]);
  const months = Number(stored.tollsMonths);
  return {
    apiBase: String(stored.apiBase || DEFAULT_API).replace(/\/$/, ""),
    tollsToken: String(stored.tollsToken || ""),
    tollsMonths: Number.isFinite(months) && months >= 1 ? Math.min(Math.round(months), 36) : HISTORY_MONTHS,
  };
}

/**
 * Post a statement to the API and report what it said it did.
 *
 * The CSV goes up as a file rather than as a JSON blob so it lands on the same
 * endpoint, and in the same shape, as a statement the operator uploads by hand
 * from the tolls page. One code path for both means the parser, the duplicate
 * check and the attribution are the ones already under test — and re-sending
 * the same page is harmless, because the importer fingerprints each crossing
 * and skips the ones it has.
 */
async function postStatement(csv: string): Promise<ImportResult> {
  const { apiBase, tollsToken } = await settings();
  const form = new FormData();
  form.append("statement", new Blob([csv], { type: "text/csv" }), "ezpass-activity.csv");

  const response = await fetch(`${apiBase}/api/tolls/import`, {
    method: "POST",
    body: form,
    headers: tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {},
  });

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    // A 422 carries the parser's own message, which lists the column names it
    // found. On a page nobody here can open, that message is the only way to
    // learn what the site calls its columns, so it is passed through whole.
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : `the API said ${response.status}`;
    throw new Error(detail);
  }
  return body as ImportResult;
}

async function sendTolls(page: ScrapedPage): Promise<SendTollsResult> {
  const paging = { pagesRead: page.pagesRead, pagingStopped: page.pagingStopped };
  const choice = chooseStatement(page);
  if (!choice.csv) {
    // Nothing matched. The masked report describes the page's shape without
    // carrying tag numbers or amounts out of it, so it can be pasted into a
    // bug report.
    return { ...paging, result: null, source: "none", rowCount: 0, report: describeShape(page) };
  }
  LOG(`sending ${choice.rowCount} row(s) from the ${choice.source}`);
  try {
    const result = await postStatement(choice.csv);
    return {
      ...paging,
      result,
      source: choice.source,
      rowCount: choice.rowCount,
      amountsLookPositive: choice.amountsLookPositive,
    };
  } catch (error) {
    return {
      ...paging,
      result: null,
      source: choice.source,
      rowCount: choice.rowCount,
      problem: error instanceof Error ? error.message : String(error),
      report: describeShape(page),
      amountsLookPositive: choice.amountsLookPositive,
    };
  }
}


// ---------------------------------------------------------------------------
// Reading the E-ZPass statement, all of it
// ---------------------------------------------------------------------------
// Driven from here rather than from a content script, because a "next" link
// that navigates tears a content script down half way through the loop. The
// worker survives that; it re-injects and carries on.
//
// The three functions below are serialised and run inside the page, so they
// are self-contained by necessity: no imports, no closure over anything here.
// They do the least possible — read the DOM, click a thing, set a select — and
// every decision is made back in `tolls.ts`, where it can be tested.

interface PageDescriptor {
  url: string;
  tables: ScrapedTable[];
  downloadLinks: string[];
  nextControls: ControlDescriptor[];
  pageSizes: PageSizeOption[];
  /** The pager's "1–10 of 96", verbatim, or "" when the page has none. */
  pagerText: string;
}

function readPageInPage(): PageDescriptor {
  const text = (node: Element | null): string =>
    (node?.textContent ?? "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();

  const tables: ScrapedTable[] = [];
  document.querySelectorAll("table").forEach((element) => {
    const table = element as HTMLTableElement;
    const rows = Array.from(table.rows);
    if (!rows.length) return;
    let headerIndex = rows.findIndex((row) => row.querySelector("th"));
    if (headerIndex < 0) headerIndex = 0;
    const headers = Array.from(rows[headerIndex].cells).map((cell) => text(cell));
    if (headers.length < 2) return;
    tables.push({
      headers,
      rows: rows.slice(headerIndex + 1).map((row) => Array.from(row.cells).map((c) => text(c))),
    });
  });

  const hint = /\.csv|\.xlsx?|csv|excel|export|download/i;
  const downloadLinks = [
    ...new Set(
      Array.from(document.querySelectorAll<HTMLAnchorElement>("a[href]"))
        .filter((a) => {
          const href = a.getAttribute("href") ?? "";
          if (!href || href.startsWith("#") || href.startsWith("javascript:")) return false;
          return hint.test(href) || hint.test(text(a));
        })
        .map((a) => a.href)
    ),
  ].sort((a, b) => Number(/\.csv/i.test(b)) - Number(/\.csv/i.test(a)));

  // Anything clickable that might advance a page. Ranked back in the worker.
  const nextControls: ControlDescriptor[] = Array.from(
    document.querySelectorAll<HTMLElement>(
      "a[href], button, [role='button'], [class*='pag'] *[onclick]"
    )
  ).map((node) => ({
    text: text(node),
    rel: node.getAttribute("rel") ?? undefined,
    ariaLabel: node.getAttribute("aria-label") ?? undefined,
    disabled:
      (node as HTMLButtonElement).disabled === true ||
      node.getAttribute("aria-disabled") === "true",
    className: node.className ? String(node.className) : undefined,
  }));

  // A select whose options are all numbers (or "All") is a rows-per-page
  // control. Taking the largest turns a twelve-page statement into one.
  let pageSizes: PageSizeOption[] = [];
  document.querySelectorAll("select").forEach((element) => {
    const select = element as HTMLSelectElement;
    const options = Array.from(select.options).map((o) => ({
      value: o.value,
      label: text(o),
    }));
    if (!options.length || pageSizes.length) return;
    const looksLikeSize = options.every((o) => /^\s*(?:all|show all|\d+[^\d]*)\s*$/i.test(o.label));
    if (looksLikeSize) pageSizes = options;
  });

  // MUI prints "1–10 of 96" in its own element; match on the whole text so a
  // sentence that merely contains such a phrase is not taken for it.
  let pagerText = "";
  for (const node of Array.from(document.querySelectorAll("p, span, div"))) {
    if (node.children.length > 0) continue;
    const content = text(node);
    if (/^\d[\d,]*\s*[\u2013\u2014-]\s*\d[\d,]*\s+of\s+\d[\d,]*$/i.test(content)) {
      pagerText = content;
      break;
    }
  }

  return { url: location.href, tables, downloadLinks, nextControls, pageSizes, pagerText };
}

function clickControlInPage(index: number): boolean {
  const nodes = Array.from(
    document.querySelectorAll<HTMLElement>(
      "a[href], button, [role='button'], [class*='pag'] *[onclick]"
    )
  );
  const node = nodes[index];
  if (!node) return false;
  node.click();
  return true;
}

function setPageSizeInPage(value: string): boolean {
  const selects = Array.from(document.querySelectorAll("select")) as HTMLSelectElement[];
  for (const select of selects) {
    if (Array.from(select.options).some((o) => o.value === value)) {
      select.value = value;
      // Both, because some pages listen for one and some for the other.
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
  }
  return false;
}

interface FormDescriptor {
  inputs: InputDescriptor[];
  controls: ControlDescriptor[];
}

// The selector for anything that might run a search. Duplicated in the click
// function below, because both are serialised into the page and cannot share it.
function readFormInPage(): FormDescriptor {
  const text = (node: Element | null): string =>
    (node?.textContent ?? "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();

  const inputs: InputDescriptor[] = Array.from(document.querySelectorAll("input")).map(
    (input, index) => {
      const id = input.id;
      const label =
        (id ? text(document.querySelector(`label[for="${CSS.escape(id)}"]`)) : "") ||
        text(input.closest("label"));
      return {
        index,
        type: (input.getAttribute("type") ?? "text").toLowerCase(),
        hint: [input.name, id, label, input.placeholder, input.getAttribute("aria-label")]
          .filter(Boolean)
          .join(" "),
        placeholder: input.placeholder || undefined,
      };
    }
  );

  const controls: ControlDescriptor[] = Array.from(
    document.querySelectorAll<HTMLElement>(
      "button, input[type='submit'], input[type='button'], [role='button'], a[href]"
    )
  ).map((node) => ({
    text: (node as HTMLInputElement).value && node.tagName === "INPUT"
      ? (node as HTMLInputElement).value
      : text(node),
    ariaLabel: node.getAttribute("aria-label") ?? undefined,
    disabled:
      (node as HTMLButtonElement).disabled === true ||
      node.getAttribute("aria-disabled") === "true",
    className: node.className ? String(node.className) : undefined,
  }));

  return { inputs, controls };
}

function fillDatesInPage(fromIndex: number, toIndex: number, fromValue: string, toValue: string): boolean {
  const inputs = Array.from(document.querySelectorAll("input"));
  const set = (index: number, value: string): boolean => {
    const input = inputs[index];
    if (!input) return false;
    // Focus, set, blur: a date field that commits on blur needs a real one,
    // and a dispatched "blur" event is not the same thing.
    input.focus();
    // The native setter, so a framework that wraps `value` still sees a change.
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.blur();
    return true;
  };
  return set(fromIndex, fromValue) && set(toIndex, toValue);
}

/**
 * MUI's rows-per-page control is a div, not a <select>: it opens a listbox on
 * mousedown. These four do the least possible and every decision stays in
 * `pickPageSize`.
 */
function findRowsPerPageInPage(): { index: number; current: string } | null {
  const nodes = Array.from(
    document.querySelectorAll<HTMLElement>("[role='combobox'], [aria-haspopup='listbox']")
  );
  const labelled = nodes.findIndex((node) => {
    const scope = node.closest("[class*='Pagination'], [class*='pagination']") ?? node.parentElement;
    return /rows per page/i.test(scope?.textContent ?? "");
  });
  // A control showing nothing but a number is the next best thing; one that
  // merely contains a digit (a year picker) is not.
  const index =
    labelled >= 0
      ? labelled
      : nodes.findIndex((node) => /^\s*\d+\s*$/.test(node.textContent ?? ""));
  if (index < 0) return null;
  return { index, current: (nodes[index].textContent ?? "").replace(/\s+/g, " ").trim() };
}

function openRowsPerPageInPage(index: number): boolean {
  const node = Array.from(
    document.querySelectorAll<HTMLElement>("[role='combobox'], [aria-haspopup='listbox']")
  )[index];
  if (!node) return false;
  node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  return true;
}

function readListboxInPage(): { value: string; label: string }[] {
  return Array.from(document.querySelectorAll<HTMLElement>("[role='listbox'] [role='option']")).map(
    (node, i) => ({ value: String(i), label: (node.textContent ?? "").replace(/\s+/g, " ").trim() })
  );
}

function chooseListboxOptionInPage(index: number): boolean {
  const node = Array.from(
    document.querySelectorAll<HTMLElement>("[role='listbox'] [role='option']")
  )[index];
  if (!node) return false;
  node.click();
  return true;
}

function readValuesInPage(indices: number[]): { value: string; invalid: boolean }[] {
  const inputs = Array.from(document.querySelectorAll("input"));
  return indices.map((i) => ({
    value: inputs[i]?.value ?? "<missing>",
    // MUI marks a date it will not accept (out of range, unparseable) here.
    invalid: inputs[i]?.getAttribute("aria-invalid") === "true",
  }));
}

/** Enter in a field is how most forms are submitted without their button. */
function pressEnterInPage(index: number): boolean {
  const input = document.querySelectorAll("input")[index] as HTMLInputElement | undefined;
  if (!input) return false;
  input.focus();
  for (const type of ["keydown", "keypress", "keyup"]) {
    input.dispatchEvent(
      new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true })
    );
  }
  return true;
}

function submitFormInPage(index: number): boolean {
  const input = document.querySelectorAll("input")[index] as HTMLInputElement | undefined;
  const form = input?.closest("form");
  if (!form) return false;
  form.requestSubmit();
  return true;
}

/** Validation messages near the form: the page's own account of a refused search. */
function readFormMessagesInPage(): string[] {
  const nodes = Array.from(
    document.querySelectorAll<HTMLElement>(
      "[role='alert'], p.Mui-error, [class*='FormHelperText'], [class*='helperText']"
    )
  );
  const seen = new Set<string>();
  for (const node of nodes) {
    const text = (node.textContent ?? "").replace(/\s+/g, " ").trim();
    if (text && text.length < 160) seen.add(text);
  }
  return [...seen].slice(0, 5);
}

function clickSubmitInPage(index: number): boolean {
  const nodes = Array.from(
    document.querySelectorAll<HTMLElement>(
      "button, input[type='submit'], input[type='button'], [role='button'], a[href]"
    )
  );
  const node = nodes[index];
  if (!node) return false;
  node.click();
  return true;
}

async function fetchExportInPage(url: string): Promise<string | null> {
  try {
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) return null;
    if (/html/i.test(response.headers.get("content-type") ?? "")) return null;
    const body = await response.text();
    if (!body.trim() || /^\s*</.test(body)) return null;
    return body.split(/\r?\n/)[0].includes(",") ? body : null;
  } catch {
    return null;
  }
}

/**
 * Pull Turo's own reservation detail for the rentals the API asks about.
 *
 * The API decides which reservations and names the route; this fetches them
 * with the session the browser already holds and posts the bodies back
 * unmodified. Every judgement about what they mean — a moved booking, a plate
 * that disagrees, whether to re-attribute crossings — is Python with tests,
 * rather than TypeScript that has to be rebuilt and side-loaded before anyone
 * can see whether it worked.
 */
async function pullTuro(tabId: number): Promise<TuroPullResult> {
  const { apiBase, tollsToken } = await settings();
  const getWanted = () =>
    fetch(`${apiBase}/api/turo/wanted`).then((response) => response.json() as Promise<TuroWanted>);
  let wanted = await getWanted();
  // Every reservation on the account first, so the rentals the mail never
  // mentioned are on file before their detail is fetched below.
  const discovered = await discoverReservations(tabId, wanted, apiBase, tollsToken);
  if (discovered.created) wanted = await getWanted();
  const details: unknown[] = [];
  let failed = 0;
  for (const id of wanted.reservations) {
    const path = wanted.detail_path.replace("{id}", encodeURIComponent(id));
    const body = await inPage(tabId, fetchJsonInPage, [path], 12, "MAIN");
    if (body === null) failed += 1;
    else details.push(body);
    // Turo's own pages fire these in bursts, but a loop of forty is not a
    // page load, and being noticed is the one failure mode that cannot be
    // retried.
    await new Promise((resolve) => setTimeout(resolve, PULL_GAP_MS));
  }
  const response = await fetch(`${apiBase}/api/turo/details`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
    },
    body: JSON.stringify({ details }),
  });
  const result: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      result && typeof result === "object" && "detail" in result
        ? String((result as { detail: unknown }).detail)
        : `the API said ${response.status}`;
    throw new Error(detail);
  }
  const extrasRead = await readExtras(tabId, wanted, apiBase, tollsToken);
  const invoices = await pullInvoices(
    tabId,
    wanted,
    (result as TuroPullResult | null)?.invoice_hubs,
    apiBase,
    tollsToken
  );
  return {
    ...(result as TuroPullResult),
    asked: wanted.reservations.length,
    failed,
    discovered,
    extras_read: extrasRead,
    ...(invoices ? { invoices } : {}),
  };
}

const LIST_PAGE_SIZE = 50;
const EXTRAS_BATCH = 10;

/**
 * Each wanted trip's photos and message thread, posted in batches for the
 * trip view. A fetch Turo refuses goes up as null, which the API does not
 * store over what it had: "Turo did not answer" is not "no photos".
 */
async function readExtras(
  tabId: number,
  wanted: TuroWanted,
  apiBase: string,
  tollsToken: string | undefined
): Promise<number> {
  if (!wanted.extras?.length || !wanted.photos_path || !wanted.messages_path) return 0;
  let stored = 0;
  let batch: { reservation_id: string; photos: unknown; messages: unknown }[] = [];
  const flush = async () => {
    if (!batch.length) return;
    const response = await fetch(`${apiBase}/api/turo/extras`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
      },
      body: JSON.stringify({ items: batch }),
    });
    if (!response.ok) throw new Error(`the API said ${response.status} to the photos`);
    stored += ((await response.json()) as { stored: number }).stored;
    batch = [];
  };
  for (const id of wanted.extras) {
    if (!/^\d+$/.test(id)) continue;
    const photos = await inPage(
      tabId, fetchJsonInPage, [wanted.photos_path.replace("{id}", id)], 12, "MAIN"
    );
    const messages = await inPage(
      tabId, fetchJsonInPage, [wanted.messages_path.replace("{id}", id)], 12, "MAIN"
    );
    batch.push({ reservation_id: id, photos, messages });
    if (batch.length >= EXTRAS_BATCH) await flush();
    await new Promise((resolve) => setTimeout(resolve, PULL_GAP_MS));
  }
  await flush();
  return stored;
}

/**
 * Read Turo's own lists of the account's reservations, page by page, and let
 * the API add any it lacks.
 *
 * Trips used to arrive only by email, and the pull could only refresh trips
 * already on file — so a rental the mail never mentioned was never here.
 */
async function discoverReservations(
  tabId: number,
  wanted: TuroWanted,
  apiBase: string,
  tollsToken: string | undefined
): Promise<TuroDiscovery> {
  const summary: TuroDiscovery = { found: 0, created: 0, unmatched: [], pages: 0, failed: [] };
  const seen = new Set<string>();
  for (const template of wanted.reservation_lists ?? []) {
    const paged = template.includes("{page}");
    let page: number | null = 0;
    let quiet = 0;
    while (page !== null) {
      const path = listPath(template, page, LIST_PAGE_SIZE);
      const body = await inPage(tabId, fetchJsonInPage, [path], 12, "MAIN");
      if (body === null) {
        if (page === 0) summary.failed.push(template.split("?")[0]);
        break;
      }
      const response = await fetch(`${apiBase}/api/turo/reservations`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
        },
        body: JSON.stringify({ body }),
      });
      if (!response.ok) throw new Error(`the API said ${response.status} to a reservation list`);
      const answer = (await response.json()) as {
        ids: string[];
        created: string[];
        unmatched: string[];
        num_pages: number | null;
      };
      summary.pages += 1;
      summary.created += answer.created.length;
      summary.unmatched.push(...answer.unmatched.filter((u) => !summary.unmatched.includes(u)));
      const fresh = answer.ids.filter((id) => !seen.has(id));
      fresh.forEach((id) => seen.add(id));
      if (!paged) break;
      const step = nextPage(page, fresh.length, quiet, answer.num_pages);
      page = step.next;
      quiet = step.quiet;
      await new Promise((resolve) => setTimeout(resolve, PULL_GAP_MS));
    }
  }
  summary.found = seen.size;
  return summary;
}

/**
 * Read the invoices the mail could not break down, and the one on screen.
 *
 * The open page is included because an invoice seen only through the
 * "charged" email has no id the API knows — opening it on Turo and pressing
 * Pull is how that one gets read.
 */
async function pullInvoices(
  tabId: number,
  wanted: TuroWanted,
  hubsNamed: string[] | undefined,
  apiBase: string,
  tollsToken: string | undefined
): Promise<TuroInvoicesResult | null> {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const post = async (route: string, payload: unknown): Promise<unknown> => {
    const response = await fetch(`${apiBase}${route}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
      },
      body: JSON.stringify(payload),
    });
    const result: unknown = await response.json().catch(() => null);
    if (!response.ok) throw new Error(`the API said ${response.status} to ${route}`);
    return result;
  };

  // Each rental's invoice hub lists its invoices, so no invoice needs an id
  // from the mail — or a person opening its page — to be found.
  let fromHubs: [string, string][] = [];
  let hubsRead = 0;
  let listed = 0;
  const hubs = wanted.hub_path ? hubsToRead(hubsNamed, tab?.url) : [];
  if (hubs.length && wanted.hub_path) {
    const bodies: { reservation_id: string; body: unknown }[] = [];
    for (const id of hubs) {
      const body = await inPage(
        tabId,
        fetchJsonInPage,
        [wanted.hub_path.replace("{id}", id)],
        12,
        "MAIN"
      );
      if (body !== null) bodies.push({ reservation_id: id, body });
      await new Promise((resolve) => setTimeout(resolve, PULL_GAP_MS));
    }
    const answer = (await post("/api/turo/hubs", { hubs: bodies })) as {
      seen: number;
      listed: number;
      to_read: [string, string][];
    };
    hubsRead = answer.seen;
    listed = answer.listed;
    fromHubs = answer.to_read;
  }

  const pairs = invoicesToRead([...(wanted.invoices ?? []), ...fromHubs], tab?.url);
  if (!pairs.length || !wanted.invoice_path) {
    return hubsRead
      ? {
          seen: 0, unparsed: 0, matched: 0, created: 0, itemised: [],
          tolls_asked: 0, tolls_recovered: 0, statuses: [],
          asked: 0, failed: 0, hubs: hubsRead, listed,
        }
      : null;
  }
  const bodies: { reservation_id: string; body: unknown }[] = [];
  let failed = 0;
  for (const [reservation, invoice] of pairs) {
    const path = invoicePath(wanted.invoice_path, reservation, invoice);
    const body = await inPage(tabId, fetchJsonInPage, [path], 12, "MAIN");
    if (body === null) failed += 1;
    else bodies.push({ reservation_id: reservation, body });
    await new Promise((resolve) => setTimeout(resolve, PULL_GAP_MS));
  }
  const result = (await post("/api/turo/invoices", { invoices: bodies })) as TuroInvoicesResult;
  return { ...result, asked: pairs.length, failed, hubs: hubsRead, listed };
}

const PULL_GAP_MS = 250;

/**
 * Read the rental's invoice hub straight after filing.
 *
 * The filing is recorded under Turo's reimbursement id, and Turo's email
 * about it arrives minutes later keyed by the invoice id. Until something
 * ties the two together the ledger counts the toll line twice — James's
 * $37.40 read as $74.80. Reading the hub now finds the invoice by its
 * reimbursement id and records its invoice id on the same row, so the email
 * lands there instead of beside it.
 *
 * Never fails the filing: Turo has already said yes, and the next pull does
 * the same merge if this one could not.
 */
async function readFiledInvoice(
  tabId: number,
  reservation: string,
  apiBase: string,
  tollsToken: string | undefined
): Promise<void> {
  try {
    // A moment for Turo to list the invoice it has just accepted.
    await new Promise((resolve) => setTimeout(resolve, FILED_SETTLE_MS));
    const wanted = (await fetch(`${apiBase}/api/turo/wanted`).then((r) =>
      r.json()
    )) as TuroWanted;
    await pullInvoices(tabId, { ...wanted, invoices: [] }, [reservation], apiBase, tollsToken);
  } catch (error) {
    LOG("reading the filed invoice:", error instanceof Error ? error.message : String(error));
  }
}

const FILED_SETTLE_MS = 3_000;

/**
 * File the invoice the API says is most worth filing.
 *
 * Which rental, how much, and what the guest reads are all decided by the API.
 * This does the two things only a logged-in browser can: rasterise the sheet
 * and talk to Turo as the host.
 */
async function fileInvoice(tabId: number, tripId?: string): Promise<FileInvoiceResult> {
  const { apiBase, tollsToken } = await settings();
  // One rental when the site picked it, otherwise the API's choice.
  const route = tripId
    ? `/api/invoices/${encodeURIComponent(tripId)}/draft`
    : "/api/invoices/next-draft";
  const response = await fetch(`${apiBase}${route}`);
  if (response.status === 404) return { filed: false, reason: "nothing to file" };
  if (!response.ok) throw new Error(`the API said ${response.status}`);
  const draft = (await response.json()) as Draft;
  if (draft.fileable === false) {
    // Checked again here and not only when the command was queued: a pull or
    // a mail sync between the click and now can have changed the answer.
    return {
      filed: false,
      reason: `held back — ${draft.held_because ?? "the ledger would not file it"}`,
      guest: draft.guest_name ?? undefined,
      amountCents: draft.total_cents,
    };
  }
  if (!draft.turo_trip_id) {
    return { filed: false, reason: "that rental is off-platform — file it yourself" };
  }
  if (!draft.can_file) {
    return { filed: false, reason: "Turo will not take an invoice for that rental" };
  }

  const outcome = await inPage(
    tabId,
    fileInvoiceInPage,
    [
      draft.evidence_svg,
      draft.turo_trip_id,
      reimbursementRequestPath(draft.turo_trip_id),
      draft.amount_dollars,
      draft.message,
    ],
    12,
    "MAIN"
  );
  if (!outcome.ok) {
    return {
      filed: false,
      reason: `${outcome.stage}: ${outcome.error}`,
      guest: draft.guest_name ?? undefined,
      amountCents: draft.total_cents,
    };
  }

  // Recorded only after Turo has said yes, and before anything else: until
  // this lands the page still lists these crossings as money to collect, and
  // a second filing is a dispute with a guest.
  await fetch(`${apiBase}/api/invoices/${draft.trip_id}/filed`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
    },
    body: JSON.stringify({
      reimbursement_id: outcome.reimbursementId,
      amount_cents: draft.total_cents,
    }),
  });
  await readFiledInvoice(tabId, draft.turo_trip_id, apiBase, tollsToken);
  return {
    filed: true,
    guest: draft.guest_name ?? undefined,
    amountCents: draft.total_cents,
    reservation: draft.turo_trip_id,
    daysLeft: draft.days_left ?? undefined,
  };
}


/**
 * File one toll invoice, from inside the page.
 *
 * Two calls, in this order, both same-origin and both needing the session the
 * browser already holds:
 *
 *   POST /api/reservation/image              multipart {file, reservationId}
 *   POST /api/v3/reimbursement/<id>/request  with that image's uuid
 *
 * The second path is built in the worker by `reimbursementRequestPath` and
 * passed in, because this function runs in the page and can import nothing.
 *
 * The SVG is rasterised here rather than in the worker because a service
 * worker has no DOM to draw one with, and the sheet is self-contained — no
 * external images or fonts — so the canvas stays untainted and `toBlob` works.
 *
 * Returns a discriminated result rather than throwing: a filing that failed
 * halfway (image uploaded, request refused) has to say so, because the caller
 * must not record it as filed.
 */
function fileInvoiceInPage(
  svg: string,
  reservationId: string,
  requestPath: string,
  amount: number,
  message: string
): Promise<{ ok: true; reimbursementId: number } | { ok: false; error: string; stage: string }> {
  const fail = (stage: string, error: string) =>
    ({ ok: false as const, error, stage });

  const rasterise = (): Promise<Blob> =>
    new Promise((resolve, reject) => {
      const image = new Image();
      const encoded =
        "data:image/svg+xml;base64," +
        btoa(String.fromCharCode(...new TextEncoder().encode(svg)));
      image.onload = () => {
        // Twice the natural size: Turo shows evidence at a few hundred pixels
        // wide and a guest reading a toll time should not have to squint.
        const scale = 2;
        const canvas = document.createElement("canvas");
        canvas.width = image.width * scale;
        canvas.height = image.height * scale;
        const context = canvas.getContext("2d");
        if (!context) return reject(new Error("no 2d context"));
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error("canvas produced nothing"))),
          "image/png"
        );
      };
      image.onerror = () => reject(new Error("the evidence sheet would not render"));
      image.src = encoded;
    });

  return (async () => {
    let png: Blob;
    try {
      png = await rasterise();
    } catch (error) {
      return fail("rasterise", error instanceof Error ? error.message : String(error));
    }

    const form = new FormData();
    form.append("file", png, `tolls-${reservationId}.png`);
    form.append("reservationId", reservationId);
    let uuid: string;
    try {
      const response = await fetch("/api/reservation/image", {
        method: "POST",
        credentials: "include",
        body: form,
      });
      if (!response.ok) {
        return fail("upload", `Turo said ${response.status} to /api/reservation/image`);
      }
      const body = (await response.json()) as { uuid?: string };
      if (!body.uuid) return fail("upload", "no uuid came back");
      uuid = body.uuid;
    } catch (error) {
      return fail("upload", error instanceof Error ? error.message : String(error));
    }

    try {
      const response = await fetch(requestPath, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: [
            {
              amount,
              itemType: "TOLLS",
              invoiceItemType: null,
              mediaEvidenceUuids: [uuid],
            },
          ],
          message,
          automatedTollTransactionsDto: { transactionUuids: [] },
          automatedOnTripEVTransactionsDto: null,
          evPostTripRechargingBatteryLevelsDto: null,
        }),
      });
      if (!response.ok) {
        // The path is in the message because that is the thing most likely to
        // be wrong, and a bare status sends somebody back to the network tab.
        return fail(
          "request",
          `Turo said ${response.status} to ${requestPath}`
        );
      }
      const body = (await response.json()) as { reimbursementId?: number };
      if (typeof body.reimbursementId !== "number") {
        // The image is uploaded and Turo may or may not have taken the
        // filing. Saying so is the only safe outcome: recording it as filed
        // would stop the crossings being chased, and retrying blind could ask
        // the guest twice.
        return fail("request", "no reimbursementId came back");
      }
      return { ok: true as const, reimbursementId: body.reimbursementId };
    } catch (error) {
      return fail("request", error instanceof Error ? error.message : String(error));
    }
  })();
}

/** Fetch one of Turo's own JSON endpoints, from inside the page. */
function fetchJsonInPage(path: string): Promise<unknown | null> {
  return fetch(path, { credentials: "include", headers: { Accept: "application/json" } })
    .then((response) => (response.ok ? response.json() : null))
    .catch(() => null);
}

/**
 * Every blob of JSON the document carries.
 *
 * Injected, so it runs in the page and is kept deliberately dumb: collect
 * text, decide nothing. Which of these matters is a judgement, and judgements
 * live in turo.ts where `npm test` can reach them.
 */
function readEmbeddedInPage(): Embedded[] {
  // A hydration blob for a whole page runs to a megabyte or so; past this it
  // is a bundle, and parsing it would block the page.
  const MAX_TEXT = 4_000_000;
  const found: Embedded[] = [];
  const keep = (label: string, text: string | null | undefined) => {
    if (!text || !text.trim() || text.length > MAX_TEXT) return;
    if (found.length >= 12) return;
    found.push({ label, text });
  };

  document.querySelectorAll("script").forEach((element, index) => {
    const script = element as HTMLScriptElement;
    const type = (script.type || "").toLowerCase();
    const id = script.id ? `#${script.id}` : `[${index}]`;
    if (type.includes("json")) {
      keep(`script${id} type=${type}`, script.textContent);
      return;
    }
    if (type && type !== "text/javascript" && type !== "module") return;
    // An inline assignment: `window.__APOLLO_STATE__ = {...};`. The text is
    // taken as it is and parsed back in the worker, which is the only place
    // that knows how to say what it found without saying what it was.
    const text = script.textContent ?? "";
    const match = /window\.(__[A-Za-z0-9_]+__|__[A-Za-z0-9_]+)\s*=\s*(\{[\s\S]*\})\s*;?\s*$/.exec(
      text.trim()
    );
    if (match) keep(`script${id} window.${match[1]}`, match[2]);
  });

  // And the globals themselves, which is where a framework leaves it after
  // hydrating — by then the script tag may be gone.
  const globals = window as unknown as Record<string, unknown>;
  for (const name of [
    "__NEXT_DATA__",
    "__APOLLO_STATE__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__NUXT__",
    "__remixContext",
  ]) {
    const value = globals[name];
    if (value === undefined || value === null) continue;
    try {
      keep(`window.${name}`, JSON.stringify(value));
    } catch {
      // Circular, or a live React handle. Not readable, not fatal.
    }
  }
  return found;
}

/**
 * Find out which endpoints a Turo page gets its data from.
 *
 * The recorder has to be in place before React makes its first call, and a
 * reload wipes anything injected into the current document. So the hook is
 * registered as a document_start content script in the page's own world, the
 * tab is reloaded, and it is unregistered afterwards — a probe that outlives
 * the question would sit in every Turo page the host opens.
 */
async function watchTuro(tabId: number): Promise<void> {
  // Install and leave installed. The one-shot probe reloads, listens for a few
  // seconds and uninstalls, which is right for "what does this page load" and
  // useless for "what happens when I submit this form" — the submit is minutes
  // away, long after it has stopped listening.
  const target = await hookTarget(tabId);
  await chrome.scripting.unregisterContentScripts({ ids: [target.id] }).catch(() => undefined);
  await chrome.scripting.registerContentScripts([
    {
      id: target.id,
      matches: target.matches,
      js: ["dist/turo-hook.js"],
      runAt: "document_start",
      world: "MAIN",
    },
  ]);
  // The reload is what puts the hook in front of the page's first call. It also
  // clears any form already filled in, which is why this is a separate button
  // pressed *before* the work rather than after it.
  await chrome.tabs.reload(tabId);
  await waitForTab(tabId);
}

/** Collect what the watch recorded, without reloading away the page. */
async function reportWatch(tabId: number): Promise<string> {
  const target = await hookTarget(tabId);
  try {
    const calls = await inPage(tabId, () => {
      const w = window as Window & { __turonomicsCalls?: unknown[] };
      return (w.__turonomicsCalls ?? []) as SeenCall[];
    }, [], 12, "MAIN");
    return [
      "=== what it fetched ===",
      summariseCalls(calls, target.summary),
    ].join("\n");
  } finally {
    await chrome.scripting.unregisterContentScripts({ ids: [target.id] }).catch(() => undefined);
  }
}

async function probeTuro(tabId: number): Promise<string> {
  const target = await hookTarget(tabId);
  await chrome.scripting.unregisterContentScripts({ ids: [target.id] }).catch(() => undefined);
  await chrome.scripting.registerContentScripts([
    {
      id: target.id,
      matches: target.matches,
      js: ["dist/turo-hook.js"],
      runAt: "document_start",
      world: "MAIN",
    },
  ]);
  try {
    await chrome.tabs.reload(tabId);
    await waitForTab(tabId);
    // The page is "complete" before its data arrives: a React route fetches
    // after it paints. Polling until the count stops growing beats a fixed
    // wait, which was either too short for the invoice hub or too long for
    // everything else.
    let previous = -1;
    for (let attempt = 0; attempt < HOOK_POLLS; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, HOOK_POLL_MS));
      const seen = await inPage(tabId, () => {
        const w = window as Window & { __turonomicsCalls?: unknown[] };
        return w.__turonomicsCalls?.length ?? 0;
      }, [], 12, "MAIN");
      if (seen > 0 && seen === previous) break;
      previous = seen;
    }
    const calls = await inPage(tabId, () => {
      const w = window as Window & { __turonomicsCalls?: unknown[] };
      return (w.__turonomicsCalls ?? []) as SeenCall[];
    }, [], 12, "MAIN");
    // Both halves, always. The first probe of a real trip page found no call
    // carrying a trip: the pages are server-rendered, so the document is
    // where the data is, and a report that only covered the network read as
    // "there is nothing here".
    const embedded = await inPage(tabId, readEmbeddedInPage, [], 12, "MAIN");
    return [
      "=== what it fetched ===",
      summariseCalls(calls, target.summary),
      "",
      "=== what the document carries ===",
      describeEmbedded(embedded),
    ].join("\n");
  } finally {
    await chrome.scripting.unregisterContentScripts({ ids: [target.id] }).catch(() => undefined);
  }
}

/**
 * Which site a tab is on, and so which hook to install and how to report it.
 * One recorder serves both: it wraps fetch and XHR and knows nothing of the
 * host, and the report is where the host's own backend is told from analytics.
 */
interface HookTarget {
  id: string;
  matches: string[];
  summary: SummaryOptions;
}

async function hookTarget(tabId: number): Promise<HookTarget> {
  const tab = await chrome.tabs.get(tabId);
  if ((tab.url ?? "").includes("e-zpassny.com")) {
    return {
      id: "turonomics-ezpass-hook",
      matches: ["https://*.e-zpassny.com/*"],
      summary: EZPASS_SUMMARY,
    };
  }
  return { id: HOOK_ID, matches: ["https://turo.com/*"], summary: {} };
}

const HOOK_ID = "turonomics-turo-hook";
const HOOK_POLLS = 10;
const HOOK_POLL_MS = 900;

/** Resolve once the tab has finished loading. */
async function waitForTab(tabId: number): Promise<void> {
  for (let attempt = 0; attempt < 40; attempt++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "complete") return;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
}

/** Run a function in the page, retrying while a navigation is in flight. */
async function inPage<Args extends unknown[], R>(
  tabId: number,
  func: (...args: Args) => R,
  args: Args,
  tries = 12,
  world?: chrome.scripting.ExecutionWorld
): Promise<R> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < tries; attempt++) {
    try {
      const [result] = await chrome.scripting.executeScript({
        target: { tabId },
        func,
        args,
        ...(world ? { world } : {}),
      });
      return result.result as R;
    } catch (error) {
      // "Frame was removed", "cannot access a chrome:// URL" while the next
      // page loads. Waiting is the whole remedy.
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

function activityOf(descriptor: PageDescriptor): ScrapedTable | null {
  return mergeActivityPages(descriptor.tables);
}

/**
 * Read every page of the statement the tab is showing.
 *
 * A download the site generates still wins outright — it is not paginated and
 * it is the format the parser was written against. Paging is the fallback's
 * fallback.
 */
async function readStatement(tabId: number): Promise<ScrapedPage> {
  const descriptor = await inPage(tabId, readPageInPage, []);

  for (const url of descriptor.downloadLinks.slice(0, 3)) {
    const csv = await inPage(tabId, fetchExportInPage, [url]);
    if (csv) {
      LOG("got a CSV from the site; no paging needed");
      return {
        url: descriptor.url,
        tables: descriptor.tables,
        downloadLinks: descriptor.downloadLinks,
        downloadedCsv: csv,
      };
    }
  }

  return pageThrough(tabId, descriptor);
}

/**
 * Page through the statement the tab is showing, from the page it is on.
 *
 * Shared by the one-shot read and by each date window: both end up looking at a
 * table with a "next" under it.
 */
/**
 * Ask a MUI table for the most rows per page it offers.
 *
 * The default is ten, so a statement of a hundred crossings is ten pages and
 * every page is a chance to stop early. Returns the descriptor to carry on
 * from, which is the old one when nothing could be done.
 */
async function raiseRowsPerPage(tabId: number, descriptor: PageDescriptor): Promise<PageDescriptor> {
  const found = await inPage(tabId, findRowsPerPageInPage, []);
  if (!found) {
    note("  rows per page: no MUI selector found");
    return descriptor;
  }
  if (!(await inPage(tabId, openRowsPerPageInPage, [found.index]))) {
    note("  rows per page: could not open the selector");
    return descriptor;
  }
  await sleep(350);
  const options = await inPage(tabId, readListboxInPage, []);
  const pick = pickPageSize(options);
  note(
    `  rows per page: currently "${found.current}", options [${options.map((o) => o.label).join(", ")}]` +
      `, choosing ${pick === null ? "nothing" : `"${options[Number(pick)]?.label}"`}`
  );
  if (pick === null) return descriptor;
  const before = tableSignature(activityOf(descriptor));
  const alreadyThere = options[Number(pick)]?.label === found.current;
  if (!(await inPage(tabId, chooseListboxOptionInPage, [Number(pick)]))) return descriptor;
  if (alreadyThere) {
    // Choosing the value it already has closes the menu and changes nothing, so
    // there is nothing to wait for. Waiting anyway cost 8s per window.
    note("  rows per page: already at that, nothing to wait for");
    return descriptor;
  }
  const after = await waitForChange(tabId, before, descriptor, 8000);
  note(`  rows per page: now ${activityOf(after)?.rows.length ?? 0} rows on the page, pager "${after.pagerText}"`);
  return after;
}

async function pageThrough(tabId: number, start: PageDescriptor): Promise<ScrapedPage> {
  let descriptor = start;
  // Fewer pages beats cleverer paging.
  const size = pickPageSize(descriptor.pageSizes);
  if (size !== null) {
    const before = tableSignature(activityOf(descriptor));
    if (await inPage(tabId, setPageSizeInPage, [size])) {
      descriptor = await waitForChange(tabId, before, descriptor);
      note(`  page size: asked for value "${size}" of ${descriptor.pageSizes.length} options`);
    } else {
      note(`  page size: could not set "${size}"`);
    }
  } else {
    descriptor = await raiseRowsPerPage(tabId, descriptor);
  }

  const pages: ScrapedTable[] = [];
  const signatures: string[] = [];
  let stopped: string | null = null;
  let lastRange: PagerRange | null = null;

  for (;;) {
    pages.push(...descriptor.tables);
    signatures.push(tableSignature(activityOf(descriptor)));
    const here = activityOf(descriptor);
    lastRange = parsePagerRange(descriptor.pagerText);
    note(
      `  page ${signatures.length}: ${here?.rows.length ?? 0} rows` +
        (lastRange ? `, pager says ${lastRange.from}-${lastRange.to} of ${lastRange.total}` : ", no pager text") +
        (here?.rows[0] ? `, first row shaped ${here.rows[0].map(maskValue).join("|")}` : "") +
        `, next candidates: ${rankNextControls(descriptor.nextControls).length}`
    );

    stopped = shouldStopPaging(signatures);
    if (stopped) break;

    // The pager's own count is the authority on the end: a next control going
    // missing is also what a re-render looks like.
    if (lastRange && lastRange.to >= lastRange.total) {
      stopped = "last page";
      break;
    }

    let ranked = rankNextControls(descriptor.nextControls);
    if (!ranked.length && !nextIsDisabled(descriptor.nextControls)) {
      // No next control at all, and no disabled one saying "this is the end".
      // That is also what a pager looks like while it re-renders after a page
      // loads, so look again for a few seconds before believing it. DOM reads
      // only; nothing is requested from the site.
      for (let attempt = 0; attempt < 8 && !ranked.length; attempt++) {
        await sleep(500);
        descriptor = await inPage(tabId, readPageInPage, []);
        ranked = rankNextControls(descriptor.nextControls);
        if (nextIsDisabled(descriptor.nextControls)) break;
      }
      note(`  no next control at first; after looking again: ${ranked.length ? "it reappeared" : nextIsDisabled(descriptor.nextControls) ? "it is disabled (the last page)" : "still none"}`);
    }
    if (!ranked.length) {
      if (nextIsDisabled(descriptor.nextControls)) {
        // The site's own word that this is the end. Nothing to explain.
        stopped = "last page";
        break;
      }
      stopped = lastRange
        ? `incomplete: no next control, but the pager says ${lastRange.to} of ${lastRange.total}`
        : "no next page";
      note("  no control looked like next, and none is a disabled next; pager controls:");
      for (const line of describePager(descriptor.nextControls)) note(line);
      break;
    }

    const before = signatures[signatures.length - 1];
    let advanced = false;
    for (const index of ranked) {
      await sleep(PACE_MS);
      if (!(await inPage(tabId, clickControlInPage, [index]))) continue;
      const clicked = descriptor.nextControls[index];
      note(`  clicked next candidate ${index}: text "${clicked?.text.slice(0, 30)}" aria "${clicked?.ariaLabel ?? ""}" class "${(clicked?.className ?? "").slice(0, 40)}"`);
      const after = await waitForChange(tabId, before, descriptor);
      const landed = tableSignature(activityOf(after));
      if (pageChanged(landed, before)) {
        descriptor = after;
        advanced = true;
        break;
      }
      // That control did nothing, or the page never finished loading. Try the
      // next candidate rather than concluding the statement ends here.
      note(`  clicked candidate ${index}: ${landed === "empty" ? "the next page never loaded" : "the table did not change"}`);
    }
    if (!advanced) {
      stopped = lastRange
        ? `incomplete: clicking next changed nothing at ${lastRange.to} of ${lastRange.total}`
        : "no next page";
      await stopIfBlocked(tabId);
      break;
    }
  }

  const merged = mergeActivityPages(pages);
  note(
    `  read ${signatures.length} page(s), ${merged?.rows.length ?? 0} rows merged: ${stopped}` +
      (lastRange ? ` (pager total ${lastRange.total}; refill credits are not shown, so fewer rows than that is expected)` : "")
  );
  return {
    url: descriptor.url,
    tables: descriptor.tables,
    downloadLinks: descriptor.downloadLinks,
    merged: merged ?? undefined,
    pagesRead: signatures.length,
    pagingStopped: stopped ?? undefined,
  };
}

/**
 * Read the statement three months at a time and post each window as it is read.
 *
 * Posting per window rather than at the end means a run that dies on window
 * three has still delivered one and two, and re-running is harmless because the
 * API fingerprints each crossing. When the page has no date fields or no search
 * button the tool recognises, this falls back to reading what is on screen, as
 * before — a page it cannot drive is no worse off than it was.
 *
 * The site's own CSV download is not used here: its link is built for whatever
 * range the page last showed, and a stale link would deliver the same rows for
 * every window and read as complete.
 */
async function sendStatement(tabId: number): Promise<SendTollsResult> {
  startTrace();
  const stored = await chrome.storage.local.get(["tollsLastRun", "tollsBlockedUntil"]);
  const refusal = runGate(
    Date.now(),
    typeof stored.tollsLastRun === "number" ? stored.tollsLastRun : null,
    typeof stored.tollsBlockedUntil === "number" ? stored.tollsBlockedUntil : null
  );
  if (refusal) {
    note(`not starting: ${refusal}. No request was made to E-ZPass.`);
    return { result: null, source: "none", rowCount: 0, problem: refusal, log: trace };
  }
  await chrome.storage.local.set({ tollsLastRun: Date.now() });
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  note(
    `extension ${chrome.runtime.getManifest().version}, page ${(tab?.url ?? "?").replace(/\?.*$/, "")}`
  );
  let result: SendTollsResult;
  try {
    result = await sendStatementTraced(tabId);
  } catch (error) {
    if (error instanceof SiteRefused) {
      await chrome.storage.local.set({ tollsBlockedUntil: Date.now() + BLOCK_PAUSE_MS });
      note(`STOPPED: the site looks like it is refusing or down. No more requests for ${BLOCK_PAUSE_MS / 60_000} minutes.`);
      return {
        result: null,
        source: "none",
        rowCount: 0,
        problem: `E-ZPass answered with what reads as a block or outage ("${error.message}"). Stopped, and paused for an hour.`,
        log: trace,
      };
    }
    // Keep the trace: the steps before the throw are the whole diagnosis.
    const message = error instanceof Error ? error.message : String(error);
    note(`THREW: ${message}`);
    return { result: null, source: "none", rowCount: 0, problem: `the run failed: ${message}`, log: trace };
  }
  note(
    result.problem
      ? `finished with a problem: ${result.problem}`
      : result.result
        ? `finished: ${result.result.rows} rows read, ${result.result.imported} new, ${result.result.already_known} known, ${result.result.matched} attributed`
        : "finished with nothing sent"
  );
  return { ...result, log: trace };
}

async function sendStatementTraced(tabId: number): Promise<SendTollsResult> {
  const form = await inPage(tabId, readFormInPage, []);
  const fields = pickDateFields(form.inputs);
  for (const line of describeForm(form.inputs, form.controls)) note(line);
  if (!fields || !rankSubmitControls(form.controls).length) {
    note("falling back: reading what the page shows (no date range driven)");
    return sendTolls(await readStatement(tabId));
  }

  const now = new Date();
  const today = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");

  const windows: WindowReport[] = [];
  const results: ImportResult[] = [];
  let amountsLookPositive = false;
  let firstReport: string | undefined;
  let source: SendTollsResult["source"] = "none";
  let problem: string | undefined;

  const { tollsMonths } = await settings();
  const ranges = quarterRanges(today, tollsMonths);
  note(`today is ${today}; reading ${tollsMonths} month(s) as ${ranges.length} window(s), newest first`);
  for (const [position, range] of ranges.entries()) {
    if (position > 0) await sleep(WINDOW_PACE_MS);
    note(`window ${range.from} to ${range.to}`);
    const sent = await sendWindow(tabId, range);
    note(
      `  window done: ${sent.rowCount} rows` +
        (sent.result
          ? `, API: ${sent.result.imported} new, ${sent.result.already_known} known, ${sent.result.matched} attributed`
          : "") +
        (sent.problem ? `, PROBLEM: ${sent.problem}` : "")
    );
    const report: WindowReport = {
      ...range,
      rows: sent.rowCount,
      pagesRead: sent.pagesRead,
      pagingStopped: sent.pagingStopped,
      problem: sent.problem,
      outside: sent.outside,
      span: sent.span,
    };
    windows.push(report);
    firstReport ??= sent.report;
    amountsLookPositive ||= sent.amountsLookPositive === true;
    if (sent.result) {
      results.push(sent.result);
      source = sent.source;
    }
    if (sent.problem) {
      // The same headers would be refused again; stop and say which window.
      problem = `${range.from} to ${range.to}: ${sent.problem}`;
      break;
    }
  }

  const rowCount = windows.reduce((n, w) => n + w.rows, 0);
  const pagesRead = windows.reduce((n, w) => n + (w.pagesRead ?? 0), 0);
  return {
    windows,
    pagesRead,
    result: results.length ? sumImportResults(results) : null,
    source,
    rowCount,
    amountsLookPositive,
    ...(problem ? { problem } : {}),
    ...(!results.length && !problem ? { report: firstReport } : {}),
  };
}

/** Put one range in the form, run the search, page through it, post it. */
async function sendWindow(tabId: number, range: DateRange): Promise<SendTollsResult> {
  // Re-read the form every time: a search that reloads the page can renumber
  // the inputs, and indices from the last window would then type into the wrong
  // box.
  const form = await inPage(tabId, readFormInPage, []);
  const fields = pickDateFields(form.inputs);
  const submit = rankSubmitControls(form.controls)[0];
  if (!fields || submit === undefined) {
    note("  the date fields or search button are gone from the page");
    return {
      result: null,
      source: "none",
      rowCount: 0,
      problem: "the date fields went away after the previous search",
    };
  }

  // Not while the last page is still loading: a search typed into a page that
  // is mid-render lands on nothing, and the late page is then read as ours.
  const before = await waitForRows(tabId, await inPage(tabId, readPageInPage, []));
  const fromText = formatForField(range.from, fields.from);
  const toText = formatForField(range.to, fields.to);
  const filled = await inPage(tabId, fillDatesInPage, [
    fields.from.index,
    fields.to.index,
    fromText,
    toText,
  ]);
  note(`  typed "${fromText}" into input[${fields.from.index}], "${toText}" into input[${fields.to.index}]: ${filled ? "ok" : "FAILED"}`);
  // What the fields hold once the page has had a moment to react: a mask or a
  // date picker can rewrite or reject what was typed.
  await sleep(400);
  const [fromNow, toNow] = await inPage(tabId, readValuesInPage, [[fields.from.index, fields.to.index]]);
  note(
    `  fields now read "${fromNow.value}" and "${toNow.value}"` +
      (sameDate(fromNow.value, fromText) && sameDate(toNow.value, toText) ? "" : "  <-- NOT WHAT WAS TYPED") +
      (fromNow.invalid ? "  <-- THE PAGE MARKS THE START DATE INVALID" : "") +
      (toNow.invalid ? "  <-- THE PAGE MARKS THE END DATE INVALID" : "")
  );
  if (!filled) {
    return { result: null, source: "none", rowCount: 0, problem: "could not type the dates" };
  }

  // Look for the button NOW, after the dates are in. It was previously chosen
  // from a read taken before typing and clicked by position seconds later: a
  // Filter that is disabled until the dates are valid was then skipped for some
  // other control, and the search never ran.
  await sleep(PACE_MS);
  const messages = await inPage(tabId, readFormMessagesInPage, []);
  if (messages.length) note(`  the page shows: ${messages.map((m) => `"${m}"`).join(" | ")}`);

  const baseline = tableSignature(activityOf(before));
  // Submitting the form first: in a real run, clicking Filter and pressing Enter
  // each did nothing, and requestSubmit was the one that ran the search. The
  // other two stay as fallbacks, in case the page changes.
  const attempts: { name: string; run: () => Promise<boolean> }[] = [
    { name: "submitting the form", run: () => inPage(tabId, submitFormInPage, [fields.to.index]) },
    {
      name: "the search button",
      run: async () => {
        const fresh = await inPage(tabId, readFormInPage, []);
        for (const line of describeSubmitCandidates(fresh.controls)) note(line);
        const choice = rankSubmitControls(fresh.controls)[0];
        if (choice === undefined) {
          note("  no enabled search button after typing the dates");
          return false;
        }
        note(`  clicking control [${choice}] "${fresh.controls[choice].text.slice(0, 40)}"`);
        return inPage(tabId, clickSubmitInPage, [choice]);
      },
    },
    { name: "Enter in the end-date field", run: () => inPage(tabId, pressEnterInPage, [fields.to.index]) },
  ];

  let after = before;
  let applied = false;
  for (const [position, attempt] of attempts.entries()) {
    if (position > 0) await sleep(PACE_MS);
    const did = await attempt.run();
    note(`  attempt ${position + 1}, ${attempt.name}: ${did ? "done" : "nothing to do"}`);
    if (!did) continue;
    after = await waitForChange(tabId, baseline, after, 8000);
    const first = dateCoverage(activityOf(after), range);
    const parsed = parsePagerRange(after.pagerText);
    note(
      `  after it: ${activityOf(after)?.rows.length ?? 0} rows dated ${first.first ?? "?"} to ${first.last ?? "?"}` +
        `, ${first.outside} outside the window, pager "${after.pagerText}"`
    );
    if (windowApplied(first) && (!parsed || parsed.from === 1)) {
      applied = true;
      break;
    }
  }
  if (!applied) {
    const shown = await inPage(tabId, readFormMessagesInPage, []);
    note(`  THE SEARCH NEVER APPLIED. Page messages: ${shown.length ? shown.join(" | ") : "none"}`);
    return {
      result: null,
      source: "none",
      rowCount: 0,
      problem:
        `the search for ${range.from} to ${range.to} never applied (the page still showed other dates)` +
        (shown.length ? `; the page says: ${shown[0]}` : ""),
    };
  }

  const stillThere = pickDateFields((await inPage(tabId, readFormInPage, [])).inputs);
  note(
    `  rows before search: ${activityOf(before)?.rows.length ?? 0}, after: ${activityOf(after)?.rows.length ?? 0}` +
      `, date fields still on page: ${stillThere ? "yes" : "NO"}`
  );
  const page = await pageThrough(tabId, after);
  const table = page.merged;
  const coverage = dateCoverage(table ?? null, range);
  note(
    `  rows dated ${coverage.first ?? "?"} to ${coverage.last ?? "?"}` +
      `, ${coverage.outside} outside ${range.from}..${range.to}, ${coverage.undated} undated of ${coverage.total}` +
      (coverage.outside ? "  <-- THE DATE FILTER DID NOT TAKE" : "")
  );
  const seen = {
    outside: coverage.outside,
    span: `${coverage.first ?? "?"} to ${coverage.last ?? "?"}`,
  };
  if (!table || !usableRows(table).length) {
    // Three months with no crossings is ordinary, not an error.
    return {
      result: null,
      source: "none",
      rowCount: 0,
      pagesRead: page.pagesRead,
      pagingStopped: page.pagingStopped,
      report: describeShape(page),
      ...seen,
    };
  }
  return { ...(await sendTolls(page)), ...seen };
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** The site answered with what reads as a block or an outage. Stop, do not retry. */
class SiteRefused extends Error {}

/**
 * Called wherever the table has gone missing and not come back.
 *
 * A missing table is ambiguous: a slow page, or the site telling us to go away.
 * Reading the page's own words settles which, and when it is the second the
 * right move is to make no further request — not to retry, wait and click again.
 */
async function stopIfBlocked(tabId: number): Promise<void> {
  const text = await inPage(tabId, () => document.body?.innerText ?? "", []);
  const snippet = looksBlocked(text);
  if (snippet) {
    note(`  THE PAGE SAYS: "${snippet}"`);
    throw new SiteRefused(snippet);
  }
}

/**
 * Re-read until the table changes to something with rows, or long enough to be
 * sure it will not.
 *
 * An empty table is not a change. E-ZPass swaps the table for nothing while the
 * next page loads, and treating that as "the next page" made paging stop at
 * page one with "the next page was empty" — and the real page two then arrived
 * during the following date window and was counted as its rows.
 */
async function waitForChange(
  tabId: number,
  before: string,
  fallback: PageDescriptor,
  maxMs = 15000
): Promise<PageDescriptor> {
  const started = Date.now();
  let sawLoading = false;
  while (Date.now() - started < maxMs) {
    await sleep(400);
    const descriptor = await inPage(tabId, readPageInPage, []);
    const signature = tableSignature(activityOf(descriptor));
    fallback = descriptor;
    if (signature === "empty") sawLoading = true;
    if (pageChanged(signature, before)) {
      if (sawLoading) note(`  table was empty while loading; rows back after ${Date.now() - started}ms`);
      return descriptor;
    }
  }
  note(
    `  waited ${Math.round((Date.now() - started) / 1000)}s and the table never changed` +
      (sawLoading ? " (it was empty for part of that)" : "")
  );
  if (tableSignature(activityOf(fallback)) === "empty") await stopIfBlocked(tabId);
  return fallback;
}

/** Wait for a table with rows to be on the page; used before touching the form. */
async function waitForRows(tabId: number, current: PageDescriptor, maxMs = 12000): Promise<PageDescriptor> {
  if (activityOf(current)?.rows.length) return current;
  const started = Date.now();
  while (Date.now() - started < maxMs) {
    await sleep(400);
    const descriptor = await inPage(tabId, readPageInPage, []);
    if (activityOf(descriptor)?.rows.length) {
      note(`  waited ${Date.now() - started}ms for the table to be there before starting`);
      return descriptor;
    }
  }
  note(`  no table with rows after ${Math.round(maxMs / 1000)}s`);
  await stopIfBlocked(tabId);
  note("  nothing on the page reads like a refusal; starting anyway");
  return current;
}

// ---------------------------------------------------------------------------
// Message listener
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message: MessageType, _sender, sendResponse) => {
  if (message.type === "COMMANDS_TICK") {
    // Every five seconds from the offscreen document; not worth a log line.
    void runCommands();
    return;
  }
  LOG("received message:", message.type);

  if (message.type === "DOWNLOAD_CSV") {
    LOG(`DOWNLOAD_CSV — ${message.trips.length} trip(s)`);
    const csv = tripsToCSV(message.trips);
    downloadCSV(csv);
    return;
  }

  if (message.type === "SEND_TOLLS") {
    sendStatement(message.tabId)
      .then(
      (result) => sendResponse({ type: "SEND_TOLLS_RESULT", result } satisfies MessageType),
      (error: unknown) =>
        sendResponse({
          type: "SEND_TOLLS_ERROR",
          error: error instanceof Error ? error.message : String(error),
        } satisfies MessageType)
    );
    return true; // async response
  }

  if (message.type === "FILE_INVOICE") {
    fileInvoice(message.tabId).then(
      (result) => sendResponse({ type: "FILE_INVOICE_RESULT", result } satisfies MessageType),
      (error: unknown) =>
        sendResponse({
          type: "FILE_INVOICE_ERROR",
          error: error instanceof Error ? error.message : String(error),
        } satisfies MessageType)
    );
    return true;
  }

  if (message.type === "PULL_TURO") {
    pullTuro(message.tabId).then(
      (result) => sendResponse({ type: "PULL_TURO_RESULT", result } satisfies MessageType),
      (error: unknown) =>
        sendResponse({
          type: "PULL_TURO_ERROR",
          error: error instanceof Error ? error.message : String(error),
        } satisfies MessageType)
    );
    return true; // async response
  }

  if (message.type === "WATCH_TURO") {
    watchTuro(message.tabId).then(
      () => sendResponse({ type: "WATCH_TURO_RESULT" } satisfies MessageType),
      (error: unknown) =>
        sendResponse({
          type: "PROBE_TURO_ERROR",
          error: error instanceof Error ? error.message : String(error),
        } satisfies MessageType)
    );
    return true;
  }

  if (message.type === "REPORT_WATCH") {
    reportWatch(message.tabId).then(
      (report) => sendResponse({ type: "PROBE_TURO_RESULT", report } satisfies MessageType),
      (error: unknown) =>
        sendResponse({
          type: "PROBE_TURO_ERROR",
          error: error instanceof Error ? error.message : String(error),
        } satisfies MessageType)
    );
    return true;
  }

  if (message.type === "PROBE_TURO") {
    probeTuro(message.tabId).then(
      (report) => sendResponse({ type: "PROBE_TURO_RESULT", report } satisfies MessageType),
      (error: unknown) =>
        sendResponse({
          type: "PROBE_TURO_ERROR",
          error: error instanceof Error ? error.message : String(error),
        } satisfies MessageType)
    );
    return true; // async response
  }

  if (message.type === "FETCH_DETAIL") {
    fetchDetailInTab(message.tripId).then((result) => {
      sendResponse({
        type: "DETAIL_RESULT",
        tripId: message.tripId,
        scheduleDates: result.scheduleDates,
        scheduleTimes: result.scheduleTimes,
      } satisfies MessageType);
    });
    return true; // async response
  }
});

// ---------------------------------------------------------------------------
// Commands queued from the site
// ---------------------------------------------------------------------------

const COMMANDS_ALARM = "turonomics-commands";

/**
 * Keep asking the API for work: an offscreen document pings every few
 * seconds, and a thirty-second alarm sits behind it in case Chrome closes the
 * document. See src/offscreen.ts.
 */
function startListening(): void {
  void chrome.alarms.create(COMMANDS_ALARM, { periodInMinutes: 0.5 });
  void ensureOffscreen();
}

async function ensureOffscreen(): Promise<void> {
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: [chrome.offscreen.Reason.WORKERS],
      justification: "Keeps a short poll for commands queued from the Turonomics site.",
    });
  } catch {
    // Already open — Chrome allows one — which is the usual case.
  }
}

chrome.runtime.onStartup.addListener(startListening);
chrome.runtime.onInstalled.addListener(startListening);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== COMMANDS_ALARM) return;
  void ensureOffscreen();
  void runCommands();
});
startListening();

let commandsRunning = false;

/**
 * Claim and run whatever the site has queued, a few at a time.
 *
 * One run at a time in this worker; the API's row lock keeps two browsers
 * from running one command. The answer is posted even when the command
 * failed, because a filing that went quiet is the one case the API will not
 * retry — it may have filed.
 */
async function runCommands(): Promise<void> {
  if (commandsRunning) return;
  commandsRunning = true;
  try {
    const { apiBase, tollsToken } = await settings();
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Extension-Version": chrome.runtime.getManifest().version,
      ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
    };
    for (let i = 0; i < 5; i++) {
      const claimed = await fetch(`${apiBase}/api/commands/claim`, { method: "POST", headers });
      // 204 is nothing queued; anything else that is not a command (a missing
      // token, the API asleep) is tried again on the next tick.
      if (claimed.status !== 200) return;
      const command = (await claimed.json()) as SiteCommand;
      LOG(`command ${command.kind}`, command.turo_trip_id ?? "");
      const outcome = await runCommand(command);
      await fetch(`${apiBase}/api/commands/${encodeURIComponent(command.id)}/done`, {
        method: "POST",
        headers,
        body: JSON.stringify(outcome),
      });
    }
  } catch (error) {
    LOG("commands:", error instanceof Error ? error.message : String(error));
  } finally {
    commandsRunning = false;
  }
}

async function runCommand(command: SiteCommand): Promise<{ ok: boolean; result: string }> {
  let tab: { id: number; opened: boolean };
  try {
    tab = await turoTabForCommands();
  } catch (error) {
    return { ok: false, result: error instanceof Error ? error.message : String(error) };
  }
  try {
    if (command.kind === "pull") {
      return { ok: true, result: describePull(await pullTuro(tab.id)) };
    }
    if (command.kind === "file" && command.trip_id) {
      const outcome = await fileInvoice(tab.id, command.trip_id);
      return { ok: outcome.filed, result: describeFiling(outcome) };
    }
    return { ok: false, result: `this version of the extension cannot ${command.kind}` };
  } catch (error) {
    return { ok: false, result: error instanceof Error ? error.message : String(error) };
  } finally {
    if (tab.opened) chrome.tabs.remove(tab.id).catch(() => undefined);
  }
}

/**
 * A Turo tab to work in: one already open, or a background one opened for
 * this command and closed after it. Turo's API only answers same-origin
 * requests carrying the session, so the work has to happen inside a Turo page.
 */
async function turoTabForCommands(): Promise<{ id: number; opened: boolean }> {
  const open = await chrome.tabs.query({ url: "https://turo.com/*", status: "complete" });
  const existing = open.find((tab) => tab.id !== undefined && !tab.discarded);
  if (existing?.id !== undefined) return { id: existing.id, opened: false };

  const created = await chrome.tabs.create({ url: "https://turo.com/us/en/trips", active: false });
  if (created.id === undefined) throw new Error("could not open a Turo tab");
  const id = created.id;
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Turo did not load within 30 seconds"));
    }, 30_000);
    const listener = (updated: number, info: chrome.tabs.TabChangeInfo) => {
      if (updated !== id || info.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  return { id, opened: true };
}
