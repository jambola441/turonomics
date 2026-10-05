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

  const record = (method: string, url: string, status: number, text: string) => {
    if (calls.length >= MAX_CALLS) return;
    let body: unknown = null;
    if (text && text.length <= MAX_BODY_BYTES) {
      try {
        body = JSON.parse(text);
      } catch {
        body = null;
      }
    }
    calls.push({ method: method.toUpperCase(), url, status, body });
  };

  const originalFetch = w.fetch.bind(w);
  w.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const response = await originalFetch(input, init);
    try {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      // A clone, so the page still gets to read its own body exactly once.
      response
        .clone()
        .text()
        .then((text) => record(method, url, response.status, text))
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
    this.addEventListener("load", () => {
      try {
        const type = this.responseType;
        // responseText throws outright when responseType is "blob" or
        // "arraybuffer", and neither carries an invoice.
        const text = type === "" || type === "text" ? this.responseText : "";
        record(
          this.__turonomicsMethod ?? "GET",
          this.__turonomicsUrl ?? location.href,
          this.status,
          text
        );
      } catch {
        // As above.
      }
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (send as any).apply(this, args);
  };
})();
