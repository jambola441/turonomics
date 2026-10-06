/**
 * Reading Turo's own JSON, rather than Turo's HTML.
 *
 * The trip scraper in `content.ts` matches on `p.css-dbccrj-StyledText`. That
 * is an emotion-generated class name: it changes when Turo changes a style,
 * not when they change the data, and it carries no year — which is why the
 * detail page has to be fetched a second time just to learn what year a trip
 * was in.
 *
 * The page gets its data as JSON, over the session the browser already holds.
 * So does the invoice hub, which carries the whole invoice: every charge at
 * its gross amount, and the status. The notification email carries one label
 * per charge, the mileage already netted down by Turo's ten percent, and no
 * status beyond what the subject line says.
 *
 * What stands in the way is not access, it is knowing the endpoints. This
 * module answers that the same way the mail pipeline did: by reporting the
 * *shape* of what the page fetched and masking the values. A shape is enough
 * to write a parser against, and it is not somebody's trip history.
 */

/** A value kind, named rather than shown. */
const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONEY = /^-?\$?\d[\d,]*\.\d{2}$/;
const DIGITS = /^\d+$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// SCREAMING_SNAKE and kebab/lower-snake tokens are an API's own vocabulary —
// "CHARGED", "REIMBURSEMENT", "toll_fee". They are the single most useful
// thing in a payload and they are not personal data, so they survive whole.
// A value with a space in it is a sentence, not an enum.
//
// No digits, and that rule is load-bearing rather than tidy: "LWH4685" is
// upper-case, separator-free and looks exactly like an enum, and the first
// version of this reported Jolene's plate verbatim. A status never needs a
// digit; a plate, a VIN and a confirmation code always have one. Losing a
// digit-bearing enum costs one round trip, and that is the cheaper mistake.
const ENUM_TOKEN = /^[A-Za-z]+(?:[_-][A-Za-z]+)*$/;
// Raised from 32 after the first real invoice payload came back with
// `reimbursementStatus: str(34)`. A status is the thing worth reading and
// Turo's are sentence-length in screaming snake, so a cap tuned to "CHARGED"
// masked exactly the field the probe existed to find.
const MAX_ENUM_LENGTH = 64;

// A descriptor this probe wrote itself: "<file image/png 92579 bytes>",
// "<text 20>", "<blob …>". They are structure by construction and must survive
// the masker, which otherwise reduces them to their own length — the upload
// call reported `file: str(28)`, and 28 is the length of
// "<file image/png 92579 bytes>", the one field written to describe it.
const OWN_DESCRIPTOR = /^<[a-z]+[^<>]*>$/;

export function stringShape(value: string): string {
  if (value === "") return "str(0)";
  if (OWN_DESCRIPTOR.test(value)) return value;
  if (ISO_DATETIME.test(value)) return "iso-datetime";
  if (ISO_DATE.test(value)) return "iso-date";
  if (MONEY.test(value)) return "money";
  if (UUID.test(value)) return "uuid";
  if (DIGITS.test(value)) return `digits(${value.length})`;
  if (/^https?:\/\//.test(value)) return urlShape(value);
  // Upper-case vocabulary first: "CHARGED" is structure. A capitalised word on
  // its own ("Marguerite") is not, and must not ride out on the same rule.
  if (value.length <= MAX_ENUM_LENGTH && ENUM_TOKEN.test(value)) {
    if (value === value.toUpperCase()) return value;
    if (value === value.toLowerCase()) return value;
  }
  return `str(${value.length})`;
}

/**
 * A URL's route, with its identifiers named instead of shown.
 *
 * The base matters. Turo fetches `/api/...`, which `new URL` refuses on its
 * own — and the first version reported those as the bare word "url", so the
 * two first-party endpoints on the page were the only two whose route was
 * thrown away.
 */
export function urlShape(raw: string, base = "https://turo.com/"): string {
  let url: URL;
  try {
    url = new URL(raw, base);
  } catch {
    return "url";
  }
  const path = url.pathname
    .split("/")
    .filter(Boolean)
    .map((segment) =>
      // A version segment is structure, and masking it cost a filing: a probe
      // reported `/api/<str(2)>/reimbursement/<id>/request`, that was read as
      // a locale, and the POST went to a path that does not exist. Narrow on
      // purpose — "v2" and not every short alphanumeric, which is what an id
      // looks like.
      /^[a-z][a-z-]*$/.test(segment) || /^v\d{1,3}$/.test(segment)
        ? segment
        : `<${stringShape(segment)}>`
    )
    .join("/");
  const keys = [...new Set([...url.searchParams.keys()])].sort();
  const query = keys.length ? `?${keys.join("&")}` : "";
  return `${url.origin}/${path}${query}`;
}

const MAX_DEPTH = 6;
const MAX_KEYS = 80;
const MAX_VARIANTS = 3;
// Raised for objects, because a payload's keys are the map of it. Arrays stay
// summarised: sixty trips of the same shape say it once.


/**
 * The shape of a parsed JSON value: its structure, with every leaf replaced by
 * the name of its kind.
 */
export function jsonShape(value: unknown, depth = 0): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "num";
  if (typeof value === "string") return stringShape(value);
  if (depth >= MAX_DEPTH) return Array.isArray(value) ? "[…]" : "{…}";
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    // Distinct element shapes, not every element's: a hundred trips in one
    // array say the same thing a hundred times, and a heterogeneous array is
    // worth knowing about.
    const variants = [...new Set(value.map((item) => jsonShape(item, depth + 1)))];
    const shown = variants.slice(0, MAX_VARIANTS).join(" | ");
    const more = variants.length > MAX_VARIANTS ? ` | +${variants.length - MAX_VARIANTS}` : "";
    return `[${value.length} × ${shown}${more}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    const kept = entries.slice(0, MAX_KEYS);
    const body = kept
      .map(([key, item]) => `${key}: ${jsonShape(item, depth + 1)}`)
      .join(", ");
    // The *names* of what was dropped, not a count. Turo's reservation detail
    // has around eighty keys, and "+38 more" hid whichever of them holds the
    // actual return time — the one field the whole late-return grace period
    // exists to guess at. A key is structure, so printing it costs nothing;
    // it is the values beside it that are shaped away.
    const dropped = entries.slice(MAX_KEYS).map(([key]) => key);
    const more = dropped.length ? `, +${dropped.length} more: ${dropped.join(" ")}` : "";
    return `{${body}${more}}`;
  }
  return typeof value;
}

/** One request the page made, as the probe records it. */
export interface SeenCall {
  method: string;
  url: string;
  status: number;
  /** The response body, already parsed, or null when it was not JSON. */
  body: unknown;
  /**
   * What was sent. Null on a GET, and on anything recorded before the hook
   * learned to keep it — which was every probe run up to 1.6.0, and is why
   * none of them could say how an invoice is filed.
   */
  request?: unknown;
}

// Only Turo's own backend. Everything else on the page is somebody's
// analytics, and the filter cannot be written as a list of their domains:
// New Relic and Google both put the *page* URL in a query parameter, so a
// beacon sent from /reservation/<id>/reimbursement/invoice contains the words
// "reservation" and "reimbursement" and reads as the most interesting call on
// the page. Six of the eight endpoints in the first real report were that.
//
// Hence two rules rather than one. First party, and matched on the route —
// never on the query string, which is where the borrowed words live.
const FIRST_PARTY = /^(?:[a-z0-9-]+\.)*turo\.com$/i;
const ASSET = /\.(?:png|jpe?g|gif|svg|ico|woff2?|ttf|css|js|map)$/i;
// First party by host and still not Turo's data: Cloudflare's RUM beacon and
// Turo's own analytics collector, both of which answer 204 or HTML.
const FIRST_PARTY_NOISE = /^\/(?:cdn-cgi\/|api\/tracking$)/i;
// Hosts whose only business is measurement. Everything else that is written to
// is worth seeing, including storage hosts nobody can name in advance.
const ANALYTICS =
  /(?:nr-data\.net|segment\.(?:io|com)|sentry\.io|datadoghq|googletagmanager|google-analytics|google\.com|doubleclick|osano|fullstory|launchdarkly|optimizely|facebook\.com|bing\.com|clarity\.ms)$|^(?:[a-z0-9-]+\.)*(?:nr-data\.net|osano\.com)$/i;

export function interestingCall(
  url: string,
  base = "https://turo.com/",
  method = "GET"
): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url, base);
  } catch {
    return false;
  }
  // A write is reported wherever it goes, unless it is going somewhere whose
  // only business is analytics. First party only was too narrow and cost a
  // real finding: the filing POST came back carrying `mediaEvidenceUuids`,
  // meaning the evidence image had already been uploaded — and the upload
  // itself was missing from the report, because an image goes to a presigned
  // URL on a storage host rather than to turo.com.
  //
  // A denylist rather than an allowlist, because the storage host cannot be
  // guessed in advance and the analytics vendors can: they are the ones
  // already seen beaconing from these pages.
  if (method.toUpperCase() !== "GET") return !ANALYTICS.test(parsed.hostname);
  if (!FIRST_PARTY.test(parsed.hostname)) return false;
  if (FIRST_PARTY_NOISE.test(parsed.pathname)) return false;
  return !ASSET.test(parsed.pathname);
}

/**
 * The report: which calls carried data, and what their payloads look like.
 *
 * Deduplicated by method and route, because a list page fetches the same
 * endpoint once per card and the question is which *endpoints* exist.
 */
export function summariseCalls(calls: SeenCall[]): string {
  const interesting = calls.filter((call) =>
    interestingCall(call.url, "https://turo.com/", call.method)
  );
  if (interesting.length === 0) {
    return `no data calls seen (${calls.length} request(s) in total)`;
  }
  const byRoute = new Map<string, SeenCall>();
  for (const call of interesting) {
    const key = `${call.method} ${urlShape(call.url)}`;
    // Keep the first that actually carried a body: a 204 or a failed retry on
    // the same route says nothing about the payload.
    const existing = byRoute.get(key);
    if (!existing || (existing.body === null && call.body !== null)) {
      byRoute.set(key, call);
    }
  }
  const lines = [`${byRoute.size} data endpoint(s) of ${calls.length} request(s)`];
  for (const [key, call] of [...byRoute.entries()].sort()) {
    lines.push("");
    lines.push(`${key} -> ${call.status}`);
    // The request first. For a POST it is the thing being looked for, and
    // burying it under the response is how it gets skimmed past.
    if (call.request !== null && call.request !== undefined) {
      lines.push(`  sent: ${jsonShape(call.request)}`);
    }
    lines.push(call.body === null ? "  (not json)" : `  ${jsonShape(call.body)}`);
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Whatever JSON the document carries
// ---------------------------------------------------------------------------
//
// Written on a conclusion that turned out to be wrong, and kept because the
// next page may yet need it. The first probe of a trip page found no call
// carrying a trip, and "the pages are server-rendered" was read off that. They
// are not: the data comes over XHR, and the probe was hiding it — a relative
// URL was reported as the bare word "url", and six analytics beacons were
// outranking the two calls that mattered. With both fixed, the trip page shows
// `/api/reservation/detail` plainly and the document holds nothing but
// schema.org markup.
//
// The lesson is the one this file keeps relearning: an absence reported by a
// tool is a claim about the tool first.

import type { FileInvoiceResult, TuroPullResult } from "./types.js";

/** A blob of JSON found in the document. */
export interface Embedded {
  /** Where it was: `script#__NEXT_DATA__`, `window.__APOLLO_STATE__`. */
  label: string;
  /** The raw text, parsed by the caller so that nothing here holds a value. */
  text: string;
}

export interface FoundPath {
  path: string;
  shape: string;
}

/** Keys worth finding in a hydration blob the size of a whole page. */
export const CHARGE_KEYS =
  /(?:toll|charge|invoice|reimburs|fee|amount|total|price|mileage|distance|fuel|ticket|citation)/i;
export const TRIP_KEYS =
  /(?:reservation|trip|pickup|dropoff|dropOff|return|start|end|scheduled|actual|vehicle|plate|odometer)/i;

const MAX_FOUND = 25;

/**
 * Paths into a structure whose key names say they hold what we are after.
 *
 * A Next.js blob is the whole page, so reporting its shape reports nothing
 * usable. Reporting the paths where "toll" or "scheduledEnd" live is the
 * answer to "how do I read this".
 *
 * Array indices collapse to `[]`: an index is not structure, and sixty trips
 * would otherwise be sixty paths saying the same thing.
 */
export function findByKey(
  value: unknown,
  pattern: RegExp,
  limit = MAX_FOUND
): FoundPath[] {
  const found: FoundPath[] = [];
  const seen = new Set<string>();

  const walk = (node: unknown, path: string, depth: number): void => {
    if (found.length >= limit || depth > 8) return;
    if (Array.isArray(node)) {
      // One element stands for all of them: they are the same shape, or the
      // shape report says otherwise.
      if (node.length) walk(node[0], `${path}[]`, depth + 1);
      return;
    }
    if (node === null || typeof node !== "object") return;
    for (const [key, child] of Object.entries(node as Record<string, unknown>)) {
      if (found.length >= limit) return;
      const here = path ? `${path}.${key}` : key;
      if (pattern.test(key)) {
        // A path, not a subtree: the same key under a hundred siblings is one
        // finding once the indices are collapsed.
        if (!seen.has(here)) {
          seen.add(here);
          found.push({ path: here, shape: jsonShape(child, 3) });
        }
        continue;
      }
      walk(child, here, depth + 1);
    }
  };

  walk(value, "", 0);
  return found;
}

/**
 * The report for what the document carries, rather than what it fetched.
 *
 * Unparseable blobs are named and skipped: an inline script that is not JSON
 * is the common case, and silence about it reads as "there was nothing there".
 */
export function describeEmbedded(blobs: Embedded[]): string {
  if (blobs.length === 0) return "no embedded json found in the document";
  const lines: string[] = [];
  for (const blob of blobs) {
    lines.push("");
    let parsed: unknown;
    try {
      parsed = JSON.parse(blob.text);
    } catch {
      lines.push(`${blob.label} — ${blob.text.length} chars, not json`);
      continue;
    }
    lines.push(`${blob.label} — ${blob.text.length} chars`);
    const charges = findByKey(parsed, CHARGE_KEYS);
    const trips = findByKey(parsed, TRIP_KEYS);
    if (!charges.length && !trips.length) {
      lines.push(`  nothing charge- or trip-shaped; top level: ${jsonShape(parsed, 4)}`);
      continue;
    }
    for (const [title, hits] of [
      ["charges", charges],
      ["trips", trips],
    ] as const) {
      if (!hits.length) continue;
      lines.push(`  ${title}:`);
      for (const hit of hits) lines.push(`    ${hit.path}: ${hit.shape}`);
    }
  }
  return lines.join("\n").trim();
}


// ---------------------------------------------------------------------------
// Saying what a pull did
// ---------------------------------------------------------------------------

/** What the pull did, in one line, with the interesting parts named. */
export function describePull(result: TuroPullResult): string {
  const parts = [`${result.stored} of ${result.asked} rental(s) read`];
  if (result.retimed.length) parts.push(`${result.retimed.length} booking(s) moved`);
  if (result.tolls_rematched) parts.push(`${result.tolls_rematched} crossing(s) re-attributed`);
  if (result.wrong_plate.length) parts.push(`${result.wrong_plate.length} on the wrong car`);
  if (result.unknown.length) parts.push(`${result.unknown.length} not in the app`);
  // Named separately from the ones the API refused, because the two have
  // different causes: Turo would not hand it over, versus the API could not
  // read it.
  if (result.failed) parts.push(`${result.failed} Turo would not return`);
  if (result.unparsed) parts.push(`${result.unparsed} unreadable`);
  return parts.join(" · ");
}


/** What a filing did, in one line. */
export function describeFiling(result: FileInvoiceResult): string {
  const money =
    result.amountCents === undefined ? "" : ` for $${(result.amountCents / 100).toFixed(2)}`;
  if (!result.filed) {
    // The amount is named even on a failure, because "it did not work" about
    // an unknown sum is not something anyone can act on.
    return result.reason ? `Not filed${money} — ${result.reason}` : `Not filed${money}`;
  }
  const who = result.guest ? ` to ${result.guest}` : "";
  const urgency =
    result.daysLeft !== undefined && result.daysLeft <= 7
      ? ` (${result.daysLeft} day(s) left on it)`
      : "";
  return `Filed${money}${who} on reservation ${result.reservation}${urgency}`;
}
