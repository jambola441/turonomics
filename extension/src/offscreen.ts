/**
 * A hidden document whose only job is a timer.
 *
 * The site queues commands — pull from Turo, file a rental's tolls — and the
 * service worker is what runs them, but Chrome stops an idle worker after
 * thirty seconds and the shortest alarm is thirty seconds too. A click on the
 * site would then wait up to half a minute before anything happened.
 *
 * An offscreen document is not stopped for being idle, so a timer here pings
 * the worker every few seconds. Each ping wakes it (or keeps it awake) and it
 * asks the API whether anything is queued. The thirty-second alarm stays
 * behind this, and recreates the document if Chrome ever closes it.
 */

const PING_MS = 5_000;

setInterval(() => {
  // Nobody answers a tick, so the promise rejects with "port closed". That is
  // the expected outcome, not an error to report every five seconds.
  chrome.runtime.sendMessage({ type: "COMMANDS_TICK" }).catch(() => undefined);
}, PING_MS);
