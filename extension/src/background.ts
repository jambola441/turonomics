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
import { describeEmbedded, summariseCalls, type Embedded, type SeenCall } from "./turo.js";
import type { Draft, FileInvoiceResult, TuroPullResult, TuroWanted } from "./types.js";
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
  const wanted = await fetch(`${apiBase}/api/turo/wanted`).then(
    (response) => response.json() as Promise<TuroWanted>
  );
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
  return { ...(result as TuroPullResult), asked: wanted.reservations.length, failed };
}

const PULL_GAP_MS = 250;

/**
 * File the invoice the API says is most worth filing.
 *
 * Which rental, how much, and what the guest reads are all decided by the API.
 * This does the two things only a logged-in browser can: rasterise the sheet
 * and talk to Turo as the host.
 */
async function fileInvoice(tabId: number): Promise<FileInvoiceResult> {
  const { apiBase, tollsToken } = await settings();
  const response = await fetch(`${apiBase}/api/invoices/next-draft`);
  if (response.status === 404) return { filed: false, reason: "nothing to file" };
  if (!response.ok) throw new Error(`the API said ${response.status}`);
  const draft = (await response.json()) as Draft;
  if (!draft.turo_trip_id) {
    return { filed: false, reason: "that rental is off-platform — file it yourself" };
  }
  if (!draft.can_file) {
    return { filed: false, reason: "Turo will not take an invoice for that rental" };
  }

  const outcome = await inPage(
    tabId,
    fileInvoiceInPage,
    [draft.evidence_svg, draft.turo_trip_id, draft.amount_dollars, draft.message],
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
 *   POST /api/<locale>/reimbursement/<id>/request   with that image's uuid
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

  /**
   * The segment between `/api/` and the route, read from what this page has
   * already fetched.
   *
   * It was hard-coded to "us", read off `/api/<str(2)>/…` in a masked probe
   * report. The POST went to a path that does not exist and came back 404 —
   * after the evidence image had been uploaded. A second reading said the
   * segment must be a version like "v2", since the masker prints a plain
   * lowercase segment verbatim and only one containing a digit masks that
   * way; the operator, looking at their own browser, said "en".
   *
   * Both are inferences about somebody else's URL, and one of them is already
   * known to have been wrong. So neither is used: this takes the segment from
   * a URL the page actually fetched, whatever it turns out to be, and fails
   * plainly if it cannot find one.
   */
  const apiPrefix = (): string | null => {
    try {
      const names = performance.getEntriesByType("resource").map((entry) => entry.name);
      // Strongest evidence first: a call on the reimbursement route itself.
      // Then any of the routes observed carrying the same prefix. Whatever
      // that segment turns out to be — a version, a locale, something else —
      // this takes it from a URL the page actually fetched rather than from
      // anybody's reading of a masked report.
      for (const pattern of [
        /\/api\/([A-Za-z0-9_-]{1,8})\/reimbursement\//,
        /\/api\/([A-Za-z0-9_-]{1,8})\/reservations\/\d+\/reimbursement/,
        /\/api\/([A-Za-z0-9_-]{1,8})\/(?:feeds|driver|reservation)\//,
      ]) {
        for (const name of names) {
          const found = pattern.exec(name);
          if (found) return found[1];
        }
      }
    } catch {
      // Resource timing is not something to fail a filing over.
    }
    return null;
  };

  return (async () => {
    const prefix = apiPrefix();
    if (!prefix) {
      // Deliberately not falling back to a guess. The last guess uploaded an
      // evidence image and then 404'd, which leaves a stray photo on the trip
      // and nothing filed.
      return fail(
        "route",
        "could not read Turo's API path from this page — open a trip page, let it " +
          "load, and try again"
      );
    }
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
      const response = await fetch(`/api/${prefix}/reimbursement/${reservationId}/request`, {
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
          `Turo said ${response.status} to /api/${prefix}/reimbursement/${reservationId}/request`
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
  await chrome.scripting.unregisterContentScripts({ ids: [HOOK_ID] }).catch(() => undefined);
  await chrome.scripting.registerContentScripts([
    {
      id: HOOK_ID,
      matches: ["https://turo.com/*"],
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
  try {
    const calls = await inPage(tabId, () => {
      const w = window as Window & { __turonomicsCalls?: unknown[] };
      return (w.__turonomicsCalls ?? []) as SeenCall[];
    }, [], 12, "MAIN");
    return [
      "=== what it fetched ===",
      summariseCalls(calls),
    ].join("\n");
  } finally {
    await chrome.scripting.unregisterContentScripts({ ids: [HOOK_ID] }).catch(() => undefined);
  }
}

async function probeTuro(tabId: number): Promise<string> {
  await chrome.scripting.unregisterContentScripts({ ids: [HOOK_ID] }).catch(() => undefined);
  await chrome.scripting.registerContentScripts([
    {
      id: HOOK_ID,
      matches: ["https://turo.com/*"],
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
      summariseCalls(calls),
      "",
      "=== what the document carries ===",
      describeEmbedded(embedded),
    ].join("\n");
  } finally {
    await chrome.scripting.unregisterContentScripts({ ids: [HOOK_ID] }).catch(() => undefined);
  }
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
