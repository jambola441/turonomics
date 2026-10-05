/**
 * Popup script — runs in the extension popup window.
 *
 * Two flows, the same shape:
 *
 * Turo:    find the turo.com tab → SCRAPE_TRIPS → DOWNLOAD_CSV, and a file
 *          lands in Downloads.
 * E-ZPass: find the e-zpassny.com tab → SCRAPE_TOLLS → SEND_TOLLS, and the
 *          statement goes straight to the API. No file in between, because the
 *          point of it is not having to remember to do this by hand.
 */

import type { MessageType, SendTollsResult, TuroTrip } from "./types.js";

const exportBtn = document.getElementById("exportBtn") as HTMLButtonElement;
const statusEl = document.getElementById("status") as HTMLDivElement;
const resultEl = document.getElementById("result") as HTMLDivElement;
const tripCountEl = document.getElementById("tripCount") as HTMLSpanElement;

function showStatus(msg: string, isError = false): void {
  statusEl.textContent = msg;
  statusEl.className = `status${isError ? " error" : ""}`;
  statusEl.classList.remove("hidden");
}

function showResult(count: number): void {
  tripCountEl.textContent = String(count);
  resultEl.classList.remove("hidden");
}

function reset(): void {
  statusEl.className = "status hidden";
  resultEl.classList.add("hidden");
}

async function getActiveTuroTab(): Promise<chrome.tabs.Tab | null> {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab?.url?.includes("turo.com")) return null;
  return tab;
}

async function scrapeTrips(tabId: number): Promise<TuroTrip[]> {
  // Ensure the content script is injected (handles cases where the tab was
  // opened before the extension was installed)
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["dist/content.js"],
  }).catch(() => {
    // Script may already be injected — ignore the error
  });

  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(
      tabId,
      { type: "SCRAPE_TRIPS" } satisfies MessageType,
      (response: MessageType | undefined) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!response) {
          reject(new Error("No response from content script."));
          return;
        }
        if (response.type === "TRIPS_RESULT") {
          resolve(response.trips);
        } else if (response.type === "TRIPS_ERROR") {
          reject(new Error(response.error));
        } else {
          reject(new Error("Unexpected response from content script."));
        }
      }
    );
  });
}

exportBtn.addEventListener("click", async () => {
  reset();
  exportBtn.disabled = true;
  showStatus("Looking for Turo tab...");

  try {
    const tab = await getActiveTuroTab();
    if (!tab?.id) {
      showStatus(
        "No active Turo tab found. Navigate to turo.com/us/en/host-dashboard/trips first.",
        true
      );
      return;
    }

    showStatus("Scraping trip list...");
    const trips = await scrapeTrips(tab.id);

    if (trips.length === 0) {
      showStatus("No trips found in the last 90 days.", true);
      return;
    }

    // Note: the content script already fetched detail pages for exact times
    // before returning, so trips arrive fully enriched.
    showStatus(`Downloading CSV for ${trips.length} trips...`);
    chrome.runtime.sendMessage({ type: "DOWNLOAD_CSV", trips } satisfies MessageType);

    statusEl.classList.add("hidden");
    showResult(trips.length);
  } catch (err) {
    showStatus(err instanceof Error ? err.message : String(err), true);
  } finally {
    exportBtn.disabled = false;
  }
});


// ---------------------------------------------------------------------------
// E-ZPass → the API
// ---------------------------------------------------------------------------
const tollsBtn = document.getElementById("tollsBtn") as HTMLButtonElement;
const tollsResultEl = document.getElementById("tollsResult") as HTMLDivElement;
const tollsReportWrap = document.getElementById("tollsReportWrap") as HTMLDetailsElement;
const tollsReportEl = document.getElementById("tollsReport") as HTMLPreElement;

const apiBaseEl = document.getElementById("apiBase") as HTMLInputElement;
const tollsTokenEl = document.getElementById("tollsToken") as HTMLInputElement;
const saveSettingsBtn = document.getElementById("saveSettings") as HTMLButtonElement;
const settingsSavedEl = document.getElementById("settingsSaved") as HTMLDivElement;

function showReport(report: string | undefined): void {
  if (!report) {
    tollsReportWrap.classList.add("hidden");
    return;
  }
  tollsReportEl.textContent = report;
  tollsReportWrap.classList.remove("hidden");
  tollsReportWrap.open = true;
}

async function getActiveEzPassTab(): Promise<chrome.tabs.Tab | null> {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab?.url?.includes("e-zpassny.com")) return null;
  return tab;
}

function send<T extends MessageType>(target: number | null, message: MessageType): Promise<T> {
  return new Promise((resolve, reject) => {
    const callback = (response: MessageType | undefined) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!response) {
        reject(new Error("No response."));
        return;
      }
      resolve(response as T);
    };
    if (target === null) chrome.runtime.sendMessage(message, callback);
    else chrome.tabs.sendMessage(target, message, callback);
  });
}

/** What came back, in a sentence, including the cases that are not successes. */
function describeImport(result: SendTollsResult): { text: string; isError: boolean } {
  if (result.problem) {
    return { text: `The API refused the file: ${result.problem}`, isError: true };
  }
  if (!result.result) {
    return {
      text: "Nothing on this page looked like account activity. Open the activity page and try again.",
      isError: true,
    };
  }
  const { rows, imported, already_known, matched, unmatched, unknown_tags } = result.result;
  if (rows > 0 && imported === 0 && already_known === 0) {
    // Read rows, stored none. Almost always the sign convention: a download
    // writes charges negative and the API skips positive rows as payments.
    return {
      text:
        `Read ${rows} rows and kept none. ` +
        (result.amountsLookPositive
          ? "This page shows charges as positive, which the API reads as payments."
          : "Nothing in the file parsed as a toll charge."),
      isError: true,
    };
  }
  const parts: string[] = [];
  if (result.pagesRead && result.pagesRead > 1) {
    parts.push(`${result.pagesRead} pages`);
  }
  // "stopped at 40 pages" means rows may be missing, which has to reach the
  // operator rather than looking like a clean read.
  if (result.pagingStopped && /stopped at/.test(result.pagingStopped)) {
    parts.push(`⚠ ${result.pagingStopped}`);
  }
  parts.push(`${rows} rows read`, `${imported} new`);
  if (already_known) parts.push(`${already_known} already on file`);
  parts.push(`${matched} attributed`);
  if (unmatched) parts.push(`${unmatched} with nobody to bill`);
  if (unknown_tags.length) {
    parts.push(`${unknown_tags.length} unbound tag(s): ${unknown_tags.join(", ")}`);
  }
  return { text: parts.join(" · "), isError: false };
}

tollsBtn.addEventListener("click", async () => {
  reset();
  tollsResultEl.classList.add("hidden");
  showReport(undefined);
  tollsBtn.disabled = true;
  showStatus("Looking for an E-ZPass tab...");

  try {
    const tab = await getActiveEzPassTab();
    if (!tab?.id) {
      showStatus("No active E-ZPass tab. Log in and open Account Activity first.", true);
      return;
    }

    // The worker reads every page of the statement and posts it. Driven from
    // there rather than from a content script, because a "next" link that
    // navigates would tear a content script down mid-loop.
    showStatus("Reading the statement — this pages through it...");
    const sent = await send(null, { type: "SEND_TOLLS", tabId: tab.id });
    if (sent.type === "SEND_TOLLS_ERROR") throw new Error(sent.error);
    if (sent.type !== "SEND_TOLLS_RESULT") throw new Error("Unexpected response from the worker.");

    const described = describeImport(sent.result);
    if (described.isError) {
      showStatus(described.text, true);
      showReport(sent.result.report);
    } else {
      statusEl.classList.add("hidden");
      tollsResultEl.textContent = described.text;
      tollsResultEl.classList.remove("hidden");
    }
  } catch (err) {
    showStatus(err instanceof Error ? err.message : String(err), true);
  } finally {
    tollsBtn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
void (async () => {
  const stored = await chrome.storage.local.get(["apiBase", "tollsToken"]);
  if (stored.apiBase) apiBaseEl.value = String(stored.apiBase);
  if (stored.tollsToken) tollsTokenEl.value = String(stored.tollsToken);
})();

// ---------------------------------------------------------------------------
// "What does this page fetch?"
// ---------------------------------------------------------------------------
const probeBtn = document.getElementById("probeBtn") as HTMLButtonElement;
const probeNoteEl = document.getElementById("probeNote") as HTMLDivElement;
const probeReportWrap = document.getElementById("probeReportWrap") as HTMLDetailsElement;
const probeReportEl = document.getElementById("probeReport") as HTMLPreElement;
const probeCopyBtn = document.getElementById("probeCopy") as HTMLButtonElement;

probeBtn.addEventListener("click", async () => {
  reset();
  probeReportWrap.classList.add("hidden");
  probeBtn.disabled = true;
  probeNoteEl.classList.remove("hidden");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !tab.url?.startsWith("https://turo.com/")) {
      showStatus("Open a Turo page first — a trip, or an invoice.", true);
      return;
    }
    showStatus("Reloading and watching what it fetches...");
    const reply = await send(null, { type: "PROBE_TURO", tabId: tab.id });
    if (reply.type === "PROBE_TURO_ERROR") throw new Error(reply.error);
    if (reply.type !== "PROBE_TURO_RESULT") throw new Error("Unexpected response from the worker.");
    statusEl.classList.add("hidden");
    probeReportEl.textContent = reply.report;
    probeReportWrap.classList.remove("hidden");
    probeReportWrap.open = true;
  } catch (error) {
    showStatus(error instanceof Error ? error.message : String(error), true);
  } finally {
    probeBtn.disabled = false;
    probeNoteEl.classList.add("hidden");
  }
});

probeCopyBtn.addEventListener("click", async () => {
  await navigator.clipboard.writeText(probeReportEl.textContent ?? "");
  probeCopyBtn.textContent = "Copied";
  setTimeout(() => (probeCopyBtn.textContent = "Copy"), 1500);
});

saveSettingsBtn.addEventListener("click", async () => {
  await chrome.storage.local.set({
    apiBase: apiBaseEl.value.trim(),
    tollsToken: tollsTokenEl.value.trim(),
  });
  settingsSavedEl.classList.remove("hidden");
  setTimeout(() => settingsSavedEl.classList.add("hidden"), 1500);
});
