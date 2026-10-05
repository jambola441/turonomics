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
  describeShape,
  mergeActivityPages,
  pickPageSize,
  rankNextControls,
  shouldStopPaging,
  tableSignature,
  type ControlDescriptor,
  type PageSizeOption,
  type ScrapedPage,
  type ScrapedTable,
} from "./tolls.js";
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
}

async function settings(): Promise<Settings> {
  const stored = await chrome.storage.local.get(["apiBase", "tollsToken"]);
  return {
    apiBase: String(stored.apiBase || DEFAULT_API).replace(/\/$/, ""),
    tollsToken: String(stored.tollsToken || ""),
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

  return { url: location.href, tables, downloadLinks, nextControls, pageSizes };
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

/** Run a function in the page, retrying while a navigation is in flight. */
async function inPage<Args extends unknown[], R>(
  tabId: number,
  func: (...args: Args) => R,
  args: Args,
  tries = 12
): Promise<R> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < tries; attempt++) {
    try {
      const [result] = await chrome.scripting.executeScript({
        target: { tabId },
        func,
        args,
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
  let descriptor = await inPage(tabId, readPageInPage, []);

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

  // Fewer pages beats cleverer paging.
  const size = pickPageSize(descriptor.pageSizes);
  if (size !== null) {
    const before = tableSignature(activityOf(descriptor));
    if (await inPage(tabId, setPageSizeInPage, [size])) {
      descriptor = await waitForChange(tabId, before, descriptor);
      LOG(`asked for ${size} rows per page`);
    }
  }

  const pages: ScrapedTable[] = [];
  const signatures: string[] = [];
  let stopped: string | null = null;

  for (;;) {
    pages.push(...descriptor.tables);
    signatures.push(tableSignature(activityOf(descriptor)));

    stopped = shouldStopPaging(signatures);
    if (stopped) break;

    const ranked = rankNextControls(descriptor.nextControls);
    if (!ranked.length) {
      stopped = "no next page";
      break;
    }

    const before = signatures[signatures.length - 1];
    let advanced = false;
    for (const index of ranked) {
      if (!(await inPage(tabId, clickControlInPage, [index]))) continue;
      const after = await waitForChange(tabId, before, descriptor);
      if (tableSignature(activityOf(after)) !== before) {
        descriptor = after;
        advanced = true;
        break;
      }
      // That control did nothing. Try the next candidate rather than
      // concluding the statement ends here.
    }
    if (!advanced) {
      stopped = "no next page";
      break;
    }
  }

  const merged = mergeActivityPages(pages);
  LOG(`read ${signatures.length} page(s): ${stopped}`);
  return {
    url: descriptor.url,
    tables: descriptor.tables,
    downloadLinks: descriptor.downloadLinks,
    merged: merged ?? undefined,
    pagesRead: signatures.length,
    pagingStopped: stopped ?? undefined,
  };
}

/** Re-read until the table changes, or long enough to be sure it will not. */
async function waitForChange(
  tabId: number,
  before: string,
  fallback: PageDescriptor
): Promise<PageDescriptor> {
  for (let attempt = 0; attempt < 20; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 400));
    const descriptor = await inPage(tabId, readPageInPage, []);
    if (tableSignature(activityOf(descriptor)) !== before) return descriptor;
    fallback = descriptor;
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// Message listener
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message: MessageType, _sender, sendResponse) => {
  LOG("received message:", message.type);

  if (message.type === "DOWNLOAD_CSV") {
    LOG(`DOWNLOAD_CSV — ${message.trips.length} trip(s)`);
    const csv = tripsToCSV(message.trips);
    downloadCSV(csv);
    return;
  }

  if (message.type === "SEND_TOLLS") {
    readStatement(message.tabId)
      .then(sendTolls)
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
