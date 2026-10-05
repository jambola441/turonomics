/**
 * Content script — injected into www.e-zpassny.com pages.
 *
 * Reads the account-activity page into plain data and hands it to the service
 * worker, which turns it into a CSV and posts it to the API. All the judgement
 * lives in `tolls.ts`, which is a module and therefore testable; a content
 * script cannot import one, so this file deliberately contains no decisions
 * worth testing — it finds tables and candidate download links and stops.
 *
 * NOTE: like `content.ts`, this must not use ES module syntax. Chrome runs
 * content scripts as plain scripts sharing the page's global scope.
 *
 * The selectors are structural — "every <table>", "every <a href>" — rather
 * than classes or test ids. Nobody here can open the logged-in page: it is
 * behind a login and an Imperva challenge, so there is no markup to write
 * selectors against and no honest way to guess them. A table with a date, an
 * amount and a tag column in its header is recognisable without knowing what
 * the site calls it, and survives a redesign that would break a class name.
 */

interface ScrapedTable {
  headers: string[];
  rows: string[][];
}

interface ScrapedPage {
  url: string;
  tables: ScrapedTable[];
  downloadLinks: string[];
  downloadedCsv?: string;
}

type EzPassMessage =
  | { type: "SCRAPE_TOLLS" }
  | { type: "TOLLS_PAGE"; page: ScrapedPage }
  | { type: "TOLLS_PAGE_ERROR"; error: string };

(() => {
  const w = window as Window & { __turonomicsEzpassLoaded?: true };
  if (w.__turonomicsEzpassLoaded) return;
  w.__turonomicsEzpassLoaded = true;

  const LOG = (...args: unknown[]) => console.warn("[turonomics:ezpass]", ...args);

  // A link worth trying. Kept narrow and GET-only: this fetches whatever it
  // matches, and a looser pattern on an authenticated site could fetch
  // something that changes state rather than reporting it.
  const EXPORT_HINT = /\.csv|\.xlsx?|csv|excel|export|download/i;

  function cellText(cell: Element): string {
    return (cell.textContent ?? "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
  }

  /**
   * One table's header and body.
   *
   * The header is whichever of these the table actually has: a `thead` row,
   * a first row of `th`, or — for the tables that use `td` throughout — the
   * first row, on the assumption it is the one naming the columns. A table
   * whose first row is data loses one crossing and is not mistaken for a
   * statement, because its "headers" will not resolve to column names.
   */
  function readTable(table: HTMLTableElement): ScrapedTable {
    const rows = Array.from(table.rows);
    if (!rows.length) return { headers: [], rows: [] };

    let headerIndex = rows.findIndex((row) => row.querySelector("th"));
    if (headerIndex < 0) headerIndex = 0;

    const headers = Array.from(rows[headerIndex].cells).map(cellText);
    const body = rows
      .slice(headerIndex + 1)
      .map((row) => Array.from(row.cells).map(cellText));
    return { headers, rows: body };
  }

  function findDownloadLinks(): string[] {
    const links = Array.from(document.querySelectorAll<HTMLAnchorElement>("a[href]"))
      .filter((a) => {
        const href = a.getAttribute("href") ?? "";
        if (!href || href.startsWith("#") || href.startsWith("javascript:")) return false;
        return EXPORT_HINT.test(href) || EXPORT_HINT.test(cellText(a));
      })
      .map((a) => a.href);
    // An explicit .csv beats a page that merely says "download".
    return [...new Set(links)].sort((a, b) =>
      Number(/\.csv/i.test(b)) - Number(/\.csv/i.test(a))
    );
  }

  /**
   * Fetch a candidate export, if it turns out to be one.
   *
   * Same-origin and with the page's cookies, which is the whole reason this
   * runs in the browser rather than on the server. Anything that comes back
   * looking like HTML is the site answering with a page — a login redirect, an
   * error, a WAF challenge — and is discarded rather than posted as a
   * statement.
   */
  async function tryDownload(url: string): Promise<string | null> {
    try {
      const response = await fetch(url, { credentials: "include" });
      if (!response.ok) return null;
      const type = response.headers.get("content-type") ?? "";
      if (/html/i.test(type)) return null;
      const text = await response.text();
      if (!text.trim() || /^\s*</.test(text)) return null;
      // A CSV has commas on its first line. A PDF statement does not, and
      // neither does a spreadsheet, which arrives as binary gibberish here.
      const [first = ""] = text.split(/\r?\n/);
      if (!first.includes(",")) return null;
      return text;
    } catch (error) {
      LOG("download attempt failed:", error);
      return null;
    }
  }

  async function scrape(): Promise<ScrapedPage> {
    const tables = Array.from(document.querySelectorAll("table"))
      .map((table) => readTable(table as HTMLTableElement))
      .filter((table) => table.headers.length > 1);
    const downloadLinks = findDownloadLinks();

    const page: ScrapedPage = { url: location.href, tables, downloadLinks };
    for (const url of downloadLinks.slice(0, 3)) {
      const csv = await tryDownload(url);
      if (csv) {
        page.downloadedCsv = csv;
        break;
      }
    }
    LOG(`${tables.length} table(s), ${downloadLinks.length} candidate link(s),`,
        page.downloadedCsv ? "got a CSV" : "no CSV");
    return page;
  }

  chrome.runtime.onMessage.addListener(
    (message: EzPassMessage, _sender, sendResponse) => {
      if (message.type !== "SCRAPE_TOLLS") return;
      scrape().then(
        (page) => sendResponse({ type: "TOLLS_PAGE", page } satisfies EzPassMessage),
        (error: unknown) =>
          sendResponse({
            type: "TOLLS_PAGE_ERROR",
            error: error instanceof Error ? error.message : String(error),
          } satisfies EzPassMessage)
      );
      return true; // async response
    }
  );
})();
