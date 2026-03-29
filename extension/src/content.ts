/**
 * Content script — injected into turo.com pages.
 *
 * Responsibility: scrape the host dashboard trip table, fetch each reservation
 * detail page for exact pickup/return times, and return structured trip data
 * to the background service worker on request.
 *
 * CONFIRMED selectors (from real page HTML, reservation 54958910):
 *   tripCard:     [data-testid="baseTripCard"]  — the <a> element
 *   dateRange:    p.css-dbccrj-StyledText       — "Mar 19 - Mar 22" (no year, no ISO)
 *   licensePlate: p.css-1torly5-StyledText      — "LWH4685" (no state prefix)
 *
 * NOTE: This file must NOT use ES module syntax (import/export). Chrome runs
 * content scripts as plain scripts sharing the global scope. The IIFE below
 * keeps all declarations local and guards against double-injection.
 */

// Types inlined from types.ts — content scripts cannot use ES module syntax.
interface TuroTrip {
  tripId: string;
  startTime: string; // ISO 8601
  endTime: string;   // ISO 8601
  licensePlate: string;
}

type MessageType =
  | { type: "SCRAPE_TRIPS" }
  | { type: "TRIPS_RESULT"; trips: TuroTrip[]; error?: never }
  | { type: "TRIPS_ERROR"; error: string; trips?: never }
  | { type: "DOWNLOAD_CSV"; trips: TuroTrip[] }
  | { type: "FETCH_DETAIL"; tripId: string }
  | { type: "DETAIL_RESULT"; tripId: string; scheduleDates: string[]; scheduleTimes: string[] }
  | { type: "DETAIL_ERROR"; tripId: string; error: string };

(() => {
  const w = window as Window & { __turonomicsLoaded?: true };
  if (w.__turonomicsLoaded) {
    console.log("[turonomics] already loaded — skipping re-injection");
    return;
  }
  w.__turonomicsLoaded = true;
  console.log("[turonomics] content script initializing on", location.href);

  // -------------------------------------------------------------------------
  // Debug logger
  // -------------------------------------------------------------------------
  const LOG = (...args: unknown[]) =>
    console.log("[turonomics]", ...args);

  // -------------------------------------------------------------------------
  // Selectors (confirmed from real page HTML)
  // -------------------------------------------------------------------------
  const SELECTORS = {
    tripCard: "[data-testid='baseTripCard']",
    dateRange: "p.css-dbccrj-StyledText",
    licensePlate: "p.css-1torly5-StyledText",
  } as const;

  // -------------------------------------------------------------------------
  // 90-day cutoff
  // -------------------------------------------------------------------------
  const NINETY_DAYS_MS = 91 * 24 * 60 * 60 * 1000;

  function isWithin90Days(dateStr: string): boolean {
    const d = new Date(dateStr);
    return !isNaN(d.getTime()) && Date.now() - d.getTime() <= NINETY_DAYS_MS;
  }

  // -------------------------------------------------------------------------
  // Date parsing — list page shows "Mar 19 - Mar 22" with no year
  // -------------------------------------------------------------------------
  function inferYear(dateStr: string, notBefore?: Date): Date {
    const now = new Date();
    const year = now.getFullYear();
    let d = new Date(`${dateStr} ${year}`);
    if (isNaN(d.getTime())) return d;
    // Future date → must be last year (trips are always completed)
    if (d.getTime() > now.getTime() + 24 * 60 * 60 * 1000) {
      d = new Date(`${dateStr} ${year - 1}`);
    }
    // End date before start date → year wrapped (e.g. Dec → Jan)
    if (notBefore && d < notBefore) {
      d = new Date(`${dateStr} ${d.getFullYear() + 1}`);
    }
    return d;
  }

  function parseDateRange(
    rangeStr: string
  ): { startTime: string; endTime: string } | null {
    const match = rangeStr.trim().match(/^(\w+\s+\d+)\s*-\s*(\w+\s+\d+)$/);
    if (!match) {
      LOG(`parseDateRange — no match for "${rangeStr}"`);
      return null;
    }
    const [, startStr, endStr] = match;
    const start = inferYear(startStr);
    const end = inferYear(endStr, start);
    if (isNaN(start.getTime()) || isNaN(end.getTime())) {
      LOG(`parseDateRange — invalid dates: start="${startStr}" end="${endStr}"`);
      return null;
    }
    return { startTime: start.toISOString(), endTime: end.toISOString() };
  }

  /**
   * Parse a schedule-date/time pair from trip card schedule elements.
   * dateStr: "Thu, Mar 19"  timeStr: "8:00 AM"
   * Strips the day-of-week prefix and infers the year (same logic as inferYear).
   */
  function parseScheduleDateTime(
    dateStr: string,
    timeStr: string,
    notBefore?: Date
  ): Date | null {
    // "Thu, Mar 19" → "Mar 19"
    const cleanDate = dateStr.replace(/^\w+,\s*/, "");
    const now = new Date();
    const year = now.getFullYear();

    let d = new Date(`${cleanDate}, ${year} ${timeStr}`);
    if (isNaN(d.getTime())) {
      LOG(`parseScheduleDateTime — could not parse "${cleanDate}, ${year} ${timeStr}"`);
      return null;
    }
    // Future date → must be last year (trips are always completed)
    if (d.getTime() > now.getTime() + 24 * 60 * 60 * 1000) {
      d = new Date(`${cleanDate}, ${year - 1} ${timeStr}`);
    }
    // End date before start date → year wrapped (e.g. Dec → Jan)
    if (notBefore && d < notBefore) {
      d = new Date(`${cleanDate}, ${d.getFullYear() + 1} ${timeStr}`);
    }
    return d;
  }


  // -------------------------------------------------------------------------
  // Detail scraping — asks the background to open the reservation page in a
  // hidden tab and scrape the rendered schedule elements (SPA, can't fetch HTML)
  // -------------------------------------------------------------------------
  async function fetchTripDetails(
    tripId: string
  ): Promise<{ startTime: string; endTime: string } | null> {
    return new Promise((resolve) => {
      LOG(`fetchTripDetails[${tripId}] — sending FETCH_DETAIL to background`);
      chrome.runtime.sendMessage(
        { type: "FETCH_DETAIL", tripId } satisfies MessageType,
        (response: MessageType | undefined) => {
          if (
            !response ||
            response.type !== "DETAIL_RESULT" ||
            response.scheduleDates.length < 2 ||
            response.scheduleTimes.length < 2
          ) {
            LOG(`fetchTripDetails[${tripId}] — no usable response`, response);
            resolve(null);
            return;
          }
          const startDate = parseScheduleDateTime(
            response.scheduleDates[0],
            response.scheduleTimes[0]
          );
          const endDate = startDate
            ? parseScheduleDateTime(response.scheduleDates[1], response.scheduleTimes[1], startDate)
            : parseScheduleDateTime(response.scheduleDates[1], response.scheduleTimes[1]);
          if (startDate && endDate) {
            const startTime = startDate.toISOString();
            const endTime = endDate.toISOString();
            LOG(`fetchTripDetails[${tripId}] — start=${startTime} end=${endTime}`);
            resolve({ startTime, endTime });
          } else {
            LOG(`fetchTripDetails[${tripId}] — parse failed for`, response.scheduleDates, response.scheduleTimes);
            resolve(null);
          }
        }
      );
    });
  }

  /** Enrich trips with exact times from their detail pages; fall back to list-page dates on failure. */
  async function enrichWithDetails(trips: TuroTrip[]): Promise<TuroTrip[]> {
    LOG(`enrichWithDetails — fetching details for ${trips.length} trip(s)`);
    const results = await Promise.all(
      trips.map(async (trip) => {
        const details = await fetchTripDetails(trip.tripId);
        if (details) return { ...trip, startTime: details.startTime, endTime: details.endTime };
        LOG(`enrichWithDetails — no details for ${trip.tripId}, keeping list-page dates`);
        return trip;
      })
    );
    LOG(`enrichWithDetails — done`);
    return results;
  }

  // -------------------------------------------------------------------------
  // List-page scraping
  // -------------------------------------------------------------------------
  function parseTripsFromDOM(): TuroTrip[] {
    const cards = document.querySelectorAll(SELECTORS.tripCard);
    LOG(`parseTripsFromDOM — URL: ${location.href}`);
    LOG(`parseTripsFromDOM — found ${cards.length} card(s) matching "${SELECTORS.tripCard}"`);

    if (cards.length === 0) {
      const allTestIds = [
        ...new Set(
          Array.from(document.querySelectorAll("[data-testid]"))
            .map((el) => el.getAttribute("data-testid"))
            .filter(Boolean)
        ),
      ].slice(0, 40);
      LOG("parseTripsFromDOM — data-testid values in DOM:", allTestIds);
      LOG("parseTripsFromDOM — body snippet:", document.body.innerHTML.slice(0, 500));
    }

    const trips: TuroTrip[] = [];

    cards.forEach((card, i) => {
      const href = card.getAttribute("href") ?? "";
      const tripId = href.split("/").pop() ?? "";
      const rawDate = card.querySelector(SELECTORS.dateRange)?.textContent?.trim() ?? "";
      const rawPlate = card.querySelector(SELECTORS.licensePlate)?.textContent?.trim() ?? "";
      const licensePlate = rawPlate.replace(/[·\s-]/g, "").toUpperCase();

      LOG(`card[${i}] href="${href}" tripId="${tripId}" rawDate="${rawDate}" rawPlate="${rawPlate}" licensePlate="${licensePlate}"`);

      if (!rawDate || !licensePlate) {
        LOG(`card[${i}] SKIPPED — missing field(s): rawDate=${!!rawDate} licensePlate=${!!licensePlate}`);
        return;
      }

      const dates = parseDateRange(rawDate);
      if (!dates) {
        LOG(`card[${i}] SKIPPED — could not parse date range "${rawDate}"`);
        return;
      }

      if (!isWithin90Days(dates.endTime)) {
        LOG(`card[${i}] SKIPPED — endTime "${dates.endTime}" is outside the 90-day window`);
        return;
      }

      trips.push({ tripId, startTime: dates.startTime, endTime: dates.endTime, licensePlate });
    });

    LOG(`parseTripsFromDOM — returning ${trips.length} trip(s)`);
    return trips;
  }

  // -------------------------------------------------------------------------
  // Wait for trip cards to appear (SPA renders asynchronously)
  // -------------------------------------------------------------------------
  function waitForTrips(timeoutMs = 10_000): Promise<TuroTrip[]> {
    return new Promise((resolve, reject) => {
      LOG(`waitForTrips — start (timeout ${timeoutMs}ms)`);

      const initial = parseTripsFromDOM();
      if (initial.length > 0) {
        LOG("waitForTrips — found trips immediately, resolving");
        resolve(initial);
        return;
      }

      LOG("waitForTrips — no trips yet, attaching MutationObserver on document.body");
      let mutationCount = 0;
      const deadline = Date.now() + timeoutMs;

      const observer = new MutationObserver(() => {
        mutationCount++;
        if (mutationCount <= 5 || mutationCount % 20 === 0) {
          LOG(`waitForTrips — mutation #${mutationCount}, elapsed ${Date.now() - (deadline - timeoutMs)}ms`);
        }
        const trips = parseTripsFromDOM();
        if (trips.length > 0) {
          LOG(`waitForTrips — trips found on mutation #${mutationCount}, resolving`);
          observer.disconnect();
          resolve(trips);
        } else if (Date.now() > deadline) {
          LOG(`waitForTrips — deadline exceeded on mutation #${mutationCount}, rejecting`);
          observer.disconnect();
          reject(new Error("No trips found within timeout. Are you on the Turo host dashboard?"));
        }
      });

      observer.observe(document.body, { childList: true, subtree: true });

      setTimeout(() => {
        LOG(`waitForTrips — hard timeout fired after ${mutationCount} mutation(s)`);
        observer.disconnect();
        reject(new Error("Timed out waiting for trip data. Are you on the Turo host dashboard trips page?"));
      }, timeoutMs);
    });
  }

  // -------------------------------------------------------------------------
  // Message listener
  // -------------------------------------------------------------------------
  chrome.runtime.onMessage.addListener(
    (message: MessageType, _sender, sendResponse) => {
      LOG("onMessage received:", message.type);
      if (message.type !== "SCRAPE_TRIPS") return;

      waitForTrips()
        .then((trips) => {
          LOG(`scrape complete — ${trips.length} trip(s), fetching details...`);
          return enrichWithDetails(trips);
        })
        .then((trips) => {
          LOG(`onMessage — sending TRIPS_RESULT with ${trips.length} trip(s)`);
          sendResponse({ type: "TRIPS_RESULT", trips } satisfies MessageType);
        })
        .catch((err: Error) => {
          LOG("onMessage — sending TRIPS_ERROR:", err.message);
          sendResponse({
            type: "TRIPS_ERROR",
            error: err.message,
          } satisfies MessageType);
        });

      return true;
    }
  );
})();
