/**
 * Content script — injected into turo.com pages.
 *
 * Responsibility: scrape the host dashboard trip table and return structured
 * trip data to the background service worker on request.
 *
 * Turo's host dashboard is a React SPA. The selectors below target the trip
 * table as it existed at time of writing and may need updating if Turo
 * changes their markup.
 *
 * HOW TO FIND SELECTORS: On https://turo.com/us/en/host-dashboard/trips,
 * open DevTools → inspect the trip rows to identify the correct CSS selectors
 * or data attributes to use in SELECTORS below.
 */

import type { TuroTrip, MessageType } from "./types.js";

// ---------------------------------------------------------------------------
// Configurable selectors — update these if Turo changes their markup
// ---------------------------------------------------------------------------
const SELECTORS = {
  // Each row in the trips list
  tripRow: "[data-testid='trip-card'], .trip-card, [class*='TripCard']",
  // Trip ID (reservation number)
  tripId:
    "[data-testid='trip-id'], [class*='reservationId'], [class*='tripId']",
  // Date/time fields — Turo typically shows them as text spans
  startTime:
    "[data-testid='trip-start'], [class*='startDate'], [class*='StartDate'], time[data-type='start']",
  endTime:
    "[data-testid='trip-end'], [class*='endDate'], [class*='EndDate'], time[data-type='end']",
  // License plate
  licensePlate:
    "[data-testid='license-plate'], [class*='licensePlate'], [class*='LicensePlate'], [class*='plate']",
} as const;

// ---------------------------------------------------------------------------
// 90-day cutoff
// ---------------------------------------------------------------------------
const NINETY_DAYS_MS = 90 * 24 * 60 * 60 * 1000;

function isCutoff(dateStr: string): boolean {
  const d = new Date(dateStr);
  return !isNaN(d.getTime()) && Date.now() - d.getTime() <= NINETY_DAYS_MS;
}

// ---------------------------------------------------------------------------
// Scraping helpers
// ---------------------------------------------------------------------------
function getText(root: Element, selector: string): string {
  const el = root.querySelector(selector);
  // Prefer datetime attribute on <time> elements, fall back to text content
  if (el instanceof HTMLTimeElement && el.dateTime) return el.dateTime.trim();
  return el?.textContent?.trim() ?? "";
}

function parseTripsFromDOM(): TuroTrip[] {
  const rows = document.querySelectorAll(SELECTORS.tripRow);
  const trips: TuroTrip[] = [];

  rows.forEach((row) => {
    const startTime = getText(row, SELECTORS.startTime);
    const endTime = getText(row, SELECTORS.endTime);
    const licensePlate = getText(row, SELECTORS.licensePlate)
      .toUpperCase()
      .replace(/\s+/g, "");
    const tripId =
      getText(row, SELECTORS.tripId) ||
      row.getAttribute("data-trip-id") ||
      row.getAttribute("data-reservation-id") ||
      "";

    if (!startTime || !endTime || !licensePlate) return;
    if (!isCutoff(startTime)) return;

    trips.push({ tripId, startTime, endTime, licensePlate });
  });

  return trips;
}

// ---------------------------------------------------------------------------
// Wait for the trip list to appear (SPA may render asynchronously)
// ---------------------------------------------------------------------------
function waitForTrips(timeoutMs = 10_000): Promise<TuroTrip[]> {
  return new Promise((resolve, reject) => {
    // Check immediately in case the table is already rendered
    const initial = parseTripsFromDOM();
    if (initial.length > 0) {
      resolve(initial);
      return;
    }

    const deadline = Date.now() + timeoutMs;

    const observer = new MutationObserver(() => {
      const trips = parseTripsFromDOM();
      if (trips.length > 0) {
        observer.disconnect();
        resolve(trips);
      } else if (Date.now() > deadline) {
        observer.disconnect();
        reject(new Error("No trips found within timeout. Are you on the Turo host dashboard?"));
      }
    });

    observer.observe(document.body, { childList: true, subtree: true });

    // Enforce the deadline even if mutations stop
    setTimeout(() => {
      observer.disconnect();
      reject(new Error("Timed out waiting for trip data. Are you on the Turo host dashboard trips page?"));
    }, timeoutMs);
  });
}

// ---------------------------------------------------------------------------
// Message listener
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener(
  (message: MessageType, _sender, sendResponse) => {
    if (message.type !== "SCRAPE_TRIPS") return;

    waitForTrips()
      .then((trips) => {
        sendResponse({ type: "TRIPS_RESULT", trips } satisfies MessageType);
      })
      .catch((err: Error) => {
        sendResponse({
          type: "TRIPS_ERROR",
          error: err.message,
        } satisfies MessageType);
      });

    // Return true to keep the message channel open for the async response
    return true;
  }
);
