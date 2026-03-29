/**
 * Popup script — runs in the extension popup window.
 *
 * Flow:
 * 1. User clicks "Export Last 90 Days"
 * 2. We find the active turo.com tab and inject the content script if needed
 * 3. We send SCRAPE_TRIPS to the content script and wait for the response
 * 4. On success, send DOWNLOAD_CSV to the background worker
 */

import type { MessageType, TuroTrip } from "./types.js";

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
