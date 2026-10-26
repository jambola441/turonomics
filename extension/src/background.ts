/**
 * Background service worker (Manifest V3).
 *
 * Responsibilities:
 * - Receive trip data from the popup (which relays it from the content script)
 * - Assemble a CSV string
 * - Trigger a file download via chrome.downloads
 * - Read E-ZPass through the site's own API and post the statement to ours
 *
 * The E-ZPass half lives here rather than in a content script because the
 * logic has to be testable, and a content script cannot import a module. The
 * decisions are in `tolls.ts`; this makes the calls.
 */

import {
  apiRowsToCsv,
  nextStartIndex,
  readTransactionAnswer,
  transactionRequest,
  BLOCK_PAUSE_MS,
  HISTORY_MONTHS,
  looksBlocked,
  PACE_MS,
  runGate,
  WINDOW_PACE_MS,
  describeTollsCommand,
  quarterRanges,
  sumImportResults,
  type WindowReport,
} from "./tolls.js";
import {
  describeMessageSend,
  messageProblem,
  MESSAGE_SEND_PATH,
  type MessageSendOutcome,
  sessionLooksLive,
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
  TuroReceiptsResult,
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
  // Asked for again, now that the invoices are in: which receipts are worth
  // reading depends on which invoices the API has a toll line for, and the
  // list fetched at the start of the pull predates them.
  const receipts = await readReceipts(tabId, await getWanted(), apiBase, tollsToken);
  return {
    ...(result as TuroPullResult),
    asked: wanted.reservations.length,
    failed,
    discovered,
    extras_read: extrasRead,
    ...(invoices ? { invoices } : {}),
    ...(receipts ? { receipts } : {}),
  };
}

const LIST_PAGE_SIZE = 50;
const EXTRAS_BATCH = 10;

/**
 * Each wanted rental's receipt, posted in batches.
 *
 * The receipt is what reached the host, and the API settles an invoice whose
 * toll line it shows paid. This only fetches and forwards: a body Turo would
 * not return goes up as null, which the API counts and does not store, so
 * "Turo did not answer" is never read as "nothing was paid".
 */
async function readReceipts(
  tabId: number,
  wanted: TuroWanted,
  apiBase: string,
  tollsToken: string | undefined
): Promise<TuroReceiptsResult | null> {
  if (!wanted.receipts?.length || !wanted.receipt_path) return null;
  const summary: TuroReceiptsResult = { read: 0, settled: [], recovered: 0, failed: 0 };
  let batch: { reservation_id: string; receipt: unknown }[] = [];
  const flush = async () => {
    if (!batch.length) return;
    const response = await fetch(`${apiBase}/api/turo/receipts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(tollsToken ? { Authorization: `Bearer ${tollsToken}` } : {}),
      },
      body: JSON.stringify({ items: batch }),
    });
    if (!response.ok) throw new Error(`the API said ${response.status} to the receipts`);
    const reply = (await response.json()) as {
      stored: number;
      tolls_recovered: number;
      unreadable: number;
      settled: string[];
    };
    summary.read += reply.stored;
    summary.recovered += reply.tolls_recovered;
    summary.failed += reply.unreadable;
    summary.settled.push(...reply.settled);
    batch = [];
  };
  for (const id of wanted.receipts) {
    if (!/^\d+$/.test(id)) continue;
    const receipt = await inPage(
      tabId, fetchJsonInPage, [wanted.receipt_path.replace("{id}", id)], 12, "MAIN"
    );
    if (receipt === null) summary.failed += 1;
    batch.push({ reservation_id: id, receipt });
    if (batch.length >= EXTRAS_BATCH) await flush();
    await new Promise((resolve) => setTimeout(resolve, PULL_GAP_MS));
  }
  await flush();
  return summary;
}

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

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** The site answered with what reads as a block or an outage. Stop, do not retry. */
class SiteRefused extends Error {}

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
    readEzpass()
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
    withTuroTab(message.tabId, (tabId) => fileInvoice(tabId)).then(
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
    withTuroTab(message.tabId, (tabId) => pullTuro(tabId)).then(
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
  if (command.kind === "tolls") return runTollsCommand();
  try {
    if (command.kind === "pull") {
      return { ok: true, result: describePull(await withTuroTab(undefined, pullTuro)) };
    }
    if (command.kind === "message") {
      // Approved by a person on the site; the API only hands one out after
      // Send. Checked again here rather than trusting the shape.
      const problem = messageProblem(command);
      if (problem) return { ok: false, result: problem };
      const reservation = command.turo_trip_id as string;
      const text = (command.message as string).trim();
      const outcome = await withTuroTab(undefined, async (tabId) => {
        const [injected] = await chrome.scripting.executeScript({
          target: { tabId },
          func: sendMessageInPage,
          args: [MESSAGE_SEND_PATH, reservation, text],
        });
        return (injected?.result as MessageSendOutcome | undefined) ?? { status: 0 };
      });
      return describeMessageSend(outcome);
    }
    if (command.kind === "file" && command.trip_id) {
      const tripId = command.trip_id;
      const outcome = await withTuroTab(undefined, (tabId) => fileInvoice(tabId, tripId));
      return { ok: outcome.filed, result: describeFiling(outcome) };
    }
    return { ok: false, result: `this version of the extension cannot ${command.kind}` };
  } catch (error) {
    return { ok: false, result: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * Post one message to a reservation's thread, from inside a Turo page, the
 * way Turo's own thread page does: a form, the session's cookies, no other
 * header. Runs in the page and can import nothing.
 */
async function sendMessageInPage(
  path: string,
  reservationId: string,
  text: string
): Promise<{ status: number; id?: string; error?: string }> {
  try {
    const form = new FormData();
    form.append("message", text);
    form.append("reservationId", reservationId);
    const response = await fetch(path, { method: "POST", body: form, credentials: "include" });
    const body = (await response.json().catch(() => null)) as { id?: unknown; message?: unknown } | null;
    return {
      status: response.status,
      id: body && typeof body.id === "string" ? body.id : undefined,
      error: response.ok ? undefined : body && typeof body.message === "string" ? body.message : undefined,
    };
  } catch (error) {
    return { status: 0, error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * Run some work inside a Turo page, wherever the user happens to be.
 *
 * The tab given (the one the popup was opened on), an open Turo tab, or a
 * hidden one opened for this and closed after — so all that is asked of the
 * user is to be logged in to Turo in this Chrome, as the Monarch extension
 * asks of a bank. Logged out is said in those words before anything runs,
 * rather than surfacing later as a 401 from some call in the middle.
 */
async function withTuroTab<T>(preferred: number | undefined, work: (tabId: number) => Promise<T>): Promise<T> {
  const tab = preferred !== undefined ? { id: preferred, opened: false } : await turoTabForCommands();
  try {
    const [probe] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: turoSessionInPage });
    if (!sessionLooksLive(probe?.result)) {
      throw new Error("You're not logged in to Turo in this Chrome. Log in at turo.com, then try again.");
    }
    return await work(tab.id);
  } finally {
    if (tab.opened) chrome.tabs.remove(tab.id).catch(() => undefined);
  }
}

/** The status Turo answers an account call with, from inside the page; 0 on a network failure. */
function turoSessionInPage(): Promise<number> {
  return fetch("/api/v2/driver/detail", { credentials: "include", headers: { Accept: "application/json" } })
    .then((response) => response.status)
    .catch(() => 0);
}

/** Read E-ZPass, queued from the site — from the phone, typically. */
async function runTollsCommand(): Promise<{ ok: boolean; result: string }> {
  const run = await readEzpass();
  await chrome.storage.local.set({ lastTollsRun: { at: Date.now(), log: run.log ?? [] } });
  return describeTollsCommand(run);
}

// ---------------------------------------------------------------------------
// E-ZPass through its API, from any page
// ---------------------------------------------------------------------------
// The Monarch way: all that is asked is that the operator be logged in to
// E-ZPass in this Chrome. The calls are made from inside an e-zpassny.com page
// (an open one, or a hidden one opened for this), so they carry the session
// and pass the site's bot protection as the site's own calls do.

const EZPASS_HOME = "https://www.e-zpassny.com/";
const EZPASS_API = "https://api.e-zpassny.com/api/account/transaction";
const EZPASS_LOGGED_OUT =
  "You're not logged in to E-ZPass in this Chrome. Log in at e-zpassny.com (tick \"Keep me Logged In\"), then try again.";

class EzpassLoggedOut extends Error {}

/** What one call made from the page came back with. */
interface EzpassCall {
  /** "out": the session route gave no token. "fail": the call itself failed. */
  outcome: "ok" | "out" | "fail";
  status: number;
  body: unknown;
  /** The first of the answer's text, for a refusal that is not JSON. */
  text: string;
}

/**
 * One transaction call, from inside an e-zpassny.com page.
 *
 * The token is asked for afresh each time: it lasts ten minutes or so and the
 * session route re-issues it, so a long read never runs on a stale one.
 */
async function ezpassCallInPage(url: string, request: Record<string, unknown>): Promise<EzpassCall> {
  try {
    const session = await fetch("/api/auth/session", { credentials: "include", headers: { Accept: "application/json" } });
    const auth = (await session.json().catch(() => null)) as { accessToken?: unknown } | null;
    if (!auth || typeof auth.accessToken !== "string" || !auth.accessToken) {
      return { outcome: "out", status: session.status, body: null, text: "" };
    }
    const response = await fetch(url, {
      method: "POST",
      headers: { authorization: `Bearer ${auth.accessToken}`, "content-type": "application/json", Accept: "application/json" },
      body: JSON.stringify(request),
    });
    const raw = await response.text();
    let body: unknown = null;
    try {
      body = JSON.parse(raw);
    } catch {
      body = null;
    }
    if (response.status === 401) return { outcome: "out", status: 401, body: null, text: "" };
    return { outcome: response.ok ? "ok" : "fail", status: response.status, body, text: raw.slice(0, 400) };
  } catch (error) {
    return { outcome: "fail", status: 0, body: null, text: error instanceof Error ? error.message : String(error) };
  }
}

/** An e-zpassny.com tab to call from: an open one, or a hidden one closed after. */
async function withEzpassTab<T>(work: (tabId: number) => Promise<T>): Promise<T> {
  const open = await chrome.tabs.query({ url: "https://*.e-zpassny.com/*", status: "complete" });
  const existing = open.find((tab) => tab.id !== undefined && !tab.discarded);
  let id = existing?.id;
  let opened = false;
  if (id === undefined) {
    const created = await chrome.tabs.create({ url: EZPASS_HOME, active: false });
    if (created.id === undefined) throw new Error("could not open an E-ZPass tab");
    id = created.id;
    opened = true;
    await waitForLoad(id, "E-ZPass");
  }
  try {
    return await work(id);
  } finally {
    if (opened) chrome.tabs.remove(id).catch(() => undefined);
  }
}

function waitForLoad(id: number, site: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error(`${site} did not load within 30 seconds`));
    }, 30_000);
    const listener = (updated: number, info: chrome.tabs.TabChangeInfo) => {
      if (updated !== id || info.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/**
 * Read the E-ZPass statement through the site's API and post it, a window at a
 * time, newest first. Same pacing and pause-on-refusal as the page-driven read.
 */
async function readEzpass(): Promise<SendTollsResult> {
  startTrace();
  const stored = await chrome.storage.local.get(["tollsBlockedUntil"]);
  const refusal = runGate(
    Date.now(),
    typeof stored.tollsBlockedUntil === "number" ? stored.tollsBlockedUntil : null
  );
  if (refusal) {
    note(`not starting: ${refusal}. No request was made to E-ZPass.`);
    return { result: null, source: "none", rowCount: 0, problem: refusal, log: trace };
  }
  note(`extension ${chrome.runtime.getManifest().version}, reading through the E-ZPass API`);
  try {
    const result = await withEzpassTab((tabId) => readEzpassWindows(tabId));
    note(
      result.problem
        ? `finished with a problem: ${result.problem}`
        : result.result
          ? `finished: ${result.result.rows} rows read, ${result.result.imported} new, ${result.result.already_known} known, ${result.result.matched} attributed`
          : "finished with nothing sent"
    );
    return { ...result, log: trace };
  } catch (error) {
    if (error instanceof EzpassLoggedOut) {
      note("logged out: the session route gave no token");
      return { result: null, source: "none", rowCount: 0, problem: EZPASS_LOGGED_OUT, log: trace };
    }
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
    const message = error instanceof Error ? error.message : String(error);
    note(`THREW: ${message}`);
    return { result: null, source: "none", rowCount: 0, problem: `the run failed: ${message}`, log: trace };
  }
}

async function readEzpassWindows(tabId: number): Promise<SendTollsResult> {
  const now = new Date();
  const today = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
  const { tollsMonths } = await settings();
  const ranges = quarterRanges(today, tollsMonths);
  note(`today is ${today}; reading ${tollsMonths} month(s) as ${ranges.length} window(s), newest first`);

  const windows: WindowReport[] = [];
  const results: ImportResult[] = [];
  let problem: string | undefined;
  let calls = 0;
  for (const [position, range] of ranges.entries()) {
    if (position > 0) await sleep(WINDOW_PACE_MS);
    const rows: Record<string, unknown>[] = [];
    let start: number | null = 1;
    let windowCalls = 0;
    while (start !== null) {
      if (calls > 0) await sleep(PACE_MS);
      calls++;
      windowCalls++;
      const [injected] = await chrome.scripting.executeScript({
        target: { tabId },
        func: ezpassCallInPage,
        args: [EZPASS_API, transactionRequest(range, start)],
      });
      const call = injected?.result as EzpassCall | undefined;
      if (!call || call.outcome === "out") throw new EzpassLoggedOut();
      const page = call.outcome === "ok" ? readTransactionAnswer(call.body) : null;
      if (!page) {
        const blocked = looksBlocked(call.text) ?? (call.status === 403 || call.status === 429 ? `status ${call.status}` : null);
        if (blocked) throw new SiteRefused(blocked);
        const said =
          call.body && typeof call.body === "object" && "message" in call.body
            ? String((call.body as { message: unknown }).message)
            : call.text.replace(/\d/g, "#").slice(0, 160);
        problem = `${range.from} to ${range.to}: E-ZPass answered ${call.status || "nothing"}${said ? ` (${said})` : ""}`;
        break;
      }
      rows.push(...page.rows);
      start = nextStartIndex(rows.length, page, windowCalls);
    }
    if (problem) break;
    const { csv, rowCount, skipped } = apiRowsToCsv(rows);
    note(`window ${range.from} to ${range.to}: ${rows.length} rows, ${rowCount} crossings, ${skipped} payments/credits/other`);
    const report: WindowReport = { ...range, rows: rowCount };
    windows.push(report);
    if (!rowCount) continue;
    try {
      const result = await postStatement(csv);
      note(`  API: ${result.imported} new, ${result.already_known} known, ${result.matched} attributed`);
      results.push(result);
    } catch (error) {
      report.problem = error instanceof Error ? error.message : String(error);
      problem = `${range.from} to ${range.to}: ${report.problem}`;
      break;
    }
  }

  const rowCount = windows.reduce((n, w) => n + w.rows, 0);
  const empty: ImportResult = { rows: 0, imported: 0, already_known: 0, matched: 0, unmatched: 0, unknown_tags: [] };
  return {
    windows,
    // A logged-in read that found no crossings is still a read: say so as zero
    // rows, not as "nothing on the page", which reads as logged out.
    result: results.length ? sumImportResults(results) : problem ? null : empty,
    source: "api",
    rowCount,
    ...(problem ? { problem } : {}),
  };
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
  await waitForLoad(created.id, "Turo");
  return { id: created.id, opened: true };
}
