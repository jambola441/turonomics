/**
 * Records what a Turo page fetches, so the endpoints can be found once.
 *
 * Runs in the page's own world at document_start, which is the only place and
 * the only moment that works: `window.fetch` has to be the wrapped one before
 * React calls it, and an isolated content script has a different `window`.
 *
 * It records and it does not interfere. Every wrapper returns exactly what the
 * original returned, reads the body from a clone, and swallows its own errors
 * — a probe that breaks the page it is watching teaches nothing and costs the
 * host a booking. Nothing leaves the page: the worker reads these records and
 * reports their *shape* (see turo.ts).
 *
 * NOT an ES module. Registered as a content script, so it shares the page's
 * global scope and must keep its declarations to itself.
 */

interface RecordedCall {
  method: string;
  url: string;
  status: number;
  body: unknown;
  /** What was *sent*. For a filing POST this is the whole point. */
  request?: unknown;
  /** The page it was made from, so an id in the path can be placed. */
  page?: string;
}

(() => {
  const MAX_CALLS = 250;
  // A payload bigger than this is a bundle or an image, not an invoice, and
  // parsing it would block the page while it fetched.
  const MAX_BODY_BYTES = 2_000_000;

  const w = window as Window & {
    __turonomicsCalls?: RecordedCall[];
    __turonomicsHooked?: true;
  };
  if (w.__turonomicsHooked) return;
  w.__turonomicsHooked = true;
  const calls: RecordedCall[] = [];
  w.__turonomicsCalls = calls;

  const parsed = (text: string): unknown => {
    if (!text || text.length > MAX_BODY_BYTES) return null;
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  };

  /**
   * What a request carried, without carrying it.
   *
   * A filing POST sends JSON; an evidence upload sends multipart with an image
   * in it. Neither is read for its values — the worker shapes whatever this
   * returns — but an image has to be described rather than parsed, because
   * megabytes of base64 would be recorded, shaped and reported as a very long
   * string for no gain.
   */
  const describeRequest = (value: unknown): unknown => {
    if (value === undefined || value === null) return null;
    if (typeof value === "string") return parsed(value) ?? `<text ${value.length}>`;
    if (typeof FormData !== "undefined" && value instanceof FormData) {
      const fields: Record<string, unknown> = {};
      value.forEach((entry, key) => {
        if (typeof entry === "string") fields[key] = parsed(entry) ?? `<text ${entry.length}>`;
        else fields[key] = `<file ${entry.type || "unknown"} ${entry.size} bytes>`;
      });
      return { "<multipart>": fields };
    }
    if (typeof Blob !== "undefined" && value instanceof Blob) {
      return `<blob ${value.type || "unknown"} ${value.size} bytes>`;
    }
    if (value instanceof ArrayBuffer) return `<binary ${value.byteLength} bytes>`;
    if (typeof URLSearchParams !== "undefined" && value instanceof URLSearchParams) {
      const fields: Record<string, unknown> = {};
      value.forEach((entry, key) => (fields[key] = `<text ${entry.length}>`));
      return { "<form>": fields };
    }
    return `<${typeof value}>`;
  };

  const record = (
    method: string,
    url: string,
    status: number,
    text: string,
    request?: unknown
  ) => {
    if (calls.length >= MAX_CALLS) return;
    calls.push({
      method: method.toUpperCase(),
      url,
      status,
      body: parsed(text),
      request: request ?? null,
      page: location.href,
    });
  };

  const originalFetch = w.fetch.bind(w);
  w.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const response = await originalFetch(input, init);
    try {
      // Absolute, resolved here where `location` is the page's own. Turo
      // fetches "/api/...", and a relative URL recorded as-is loses its route
      // when the worker shapes it — which is how the only two first-party
      // endpoints in the first real report came back as the word "url".
      const raw =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const url = new URL(raw, location.href).href;
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      const sent = describeRequest(init?.body);
      // A clone, so the page still gets to read its own body exactly once.
      response
        .clone()
        .text()
        .then((text) => record(method, url, response.status, text, sent))
        .catch(() => undefined);
    } catch {
      // Recording is best effort; the response is not.
    }
    return response;
  };

  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  interface Tracked extends XMLHttpRequest {
    __turonomicsMethod?: string;
    __turonomicsUrl?: string;
  }
  XMLHttpRequest.prototype.open = function (
    this: Tracked,
    method: string,
    url: string | URL,
    ...rest: unknown[]
  ) {
    this.__turonomicsMethod = method;
    this.__turonomicsUrl = typeof url === "string" ? url : url.href;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (open as any).call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (this: Tracked, ...args: unknown[]) {
    const sent = describeRequest(args[0]);
    this.addEventListener("load", () => {
      try {
        const type = this.responseType;
        // responseText throws outright when responseType is "blob" or
        // "arraybuffer", and neither carries an invoice.
        const text = type === "" || type === "text" ? this.responseText : "";
        record(
          this.__turonomicsMethod ?? "GET",
          new URL(this.__turonomicsUrl ?? location.href, location.href).href,
          this.status,
          text,
          sent
        );
      } catch {
        // As above.
      }
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (send as any).apply(this, args);
  };
})();
