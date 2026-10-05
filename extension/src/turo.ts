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
const MAX_ENUM_LENGTH = 32;

export function stringShape(value: string): string {
  if (value === "") return "str(0)";
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

/** A URL's route, with its identifiers named instead of shown. */
export function urlShape(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return "url";
  }
  const path = url.pathname
    .split("/")
    .filter(Boolean)
    .map((segment) =>
      /^[a-z][a-z-]*$/.test(segment) ? segment : `<${stringShape(segment)}>`
    )
    .join("/");
  const keys = [...new Set([...url.searchParams.keys()])].sort();
  const query = keys.length ? `?${keys.join("&")}` : "";
  return `${url.origin}/${path}${query}`;
}

const MAX_DEPTH = 6;
const MAX_KEYS = 40;
const MAX_VARIANTS = 3;

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
    const more = entries.length > MAX_KEYS ? `, +${entries.length - MAX_KEYS} more` : "";
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
}

// Turo's pages fetch fonts, images, analytics and feature flags. None of that
// carries a trip, and reporting it buries the two or three calls that do.
const UNINTERESTING = /(?:segment|sentry|datadog|googletagmanager|optimizely|launchdarkly|fullstory|\.(?:png|jpe?g|svg|woff2?|css|js)(?:$|\?))/i;
const INTERESTING = /(?:\/api\/|graphql|\.json(?:$|\?)|invoice|reservation|reimburs|trip|booking|vehicle)/i;

export function interestingCall(url: string): boolean {
  if (UNINTERESTING.test(url)) return false;
  return INTERESTING.test(url);
}

/**
 * The report: which calls carried data, and what their payloads look like.
 *
 * Deduplicated by method and route, because a list page fetches the same
 * endpoint once per card and the question is which *endpoints* exist.
 */
export function summariseCalls(calls: SeenCall[]): string {
  const interesting = calls.filter((call) => interestingCall(call.url));
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
    lines.push(call.body === null ? "  (not json)" : `  ${jsonShape(call.body)}`);
  }
  return lines.join("\n");
}
