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

import { chooseStatement, describeShape } from "./tolls.js";
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

async function sendTolls(page: Parameters<typeof chooseStatement>[0]): Promise<SendTollsResult> {
  const choice = chooseStatement(page);
  if (!choice.csv) {
    // Nothing matched. The masked report describes the page's shape without
    // carrying tag numbers or amounts out of it, so it can be pasted into a
    // bug report.
    return { result: null, source: "none", rowCount: 0, report: describeShape(page) };
  }
  LOG(`sending ${choice.rowCount} row(s) from the ${choice.source}`);
  try {
    const result = await postStatement(choice.csv);
    return {
      result,
      source: choice.source,
      rowCount: choice.rowCount,
      amountsLookPositive: choice.amountsLookPositive,
    };
  } catch (error) {
    return {
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
    sendTolls(message.page).then(
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
