/**
 * Turning an E-ZPass account-activity page into the CSV the API already reads.
 *
 * Why the extension and not the server: www.e-zpassny.com sits behind Imperva
 * bot protection — an unauthenticated curl gets a JavaScript challenge rather
 * than HTML. A headless browser on Render would be arguing with that WAF, and
 * it would need the operator's E-ZPass password and a way past whatever
 * two-factor step the login does. The browser already logged in is none of
 * those things: the session is theirs, the click is theirs, and the page is a
 * real browser because it is one.
 *
 * Why this file is separate from the content script: content scripts cannot use
 * ES modules, so anything in one is untestable without a browser. Everything
 * here is a pure function over plain data, imported by the service worker and
 * by `tolls.test.ts`. The content script's whole job is to read the DOM into
 * `ScrapedTable`s and hand them over.
 *
 * Why it emits the download's CSV format rather than parsing the rows: the
 * Python side already has a tested parser for an E-ZPass statement, with
 * column-name aliases, the payment-row skip, and the sign convention. A second
 * parser here would be a second thing to get wrong. If the page's column names
 * are ones the API does not know, the import fails with a 422 that lists the
 * names it found — which is how to learn them, rather than by guessing.
 */

export interface ScrapedTable {
  /** Header cell text, verbatim. These are column names, not account data. */
  headers: string[];
  /** Body rows, cell text, verbatim. */
  rows: string[][];
}

export interface ScrapedPage {
  url: string;
  tables: ScrapedTable[];
  /** Hrefs that look like a CSV/Excel export, best first. */
  downloadLinks: string[];
  /** Set when the content script fetched a download link itself. */
  downloadedCsv?: string;
}

/** What `chooseStatement` concluded, so the popup can say something useful. */
export interface StatementChoice {
  csv: string | null;
  /** Where it came from, for the popup and the logs. */
  source: "download" | "table" | "none";
  /** Matched column names, for when the API rejects the headers. */
  headers: string[];
  rowCount: number;
  /**
   * True when every amount on the chosen table reads as zero or positive.
   *
   * This matters more than it looks. An E-ZPass download writes charges as
   * negative ("$-2.86") and the API skips anything positive as a payment, so a
   * page that displays charges positive would import as nothing at all. Not
   * corrected here: an activity table mixes tolls with replenishments, so
   * flipping every sign would turn payments into charges and bill a guest for
   * one. Reported instead, loudly, and the import's own "read 40, imported 0"
   * says the same thing from the other end.
   */
  amountsLookPositive: boolean;
}

// ---------------------------------------------------------------------------
// Column recognition
// ---------------------------------------------------------------------------
// Deliberately loose. The job is not to parse the column but to recognise that
// a table is the activity table, and the API's own alias list decides the rest.
const COLUMN_PATTERNS: Record<string, RegExp> = {
  date: /\bdate\b|posted|transaction\s*date/i,
  amount: /amount|debit|credit|charge|\bfee\b|\btoll\b|balance/i,
  tagPlate: /\btag\b|plate|transponder|vehicle/i,
  plaza: /plaza|exit|entry|\bpoint\b|agency|location|facility/i,
};

/** The columns without which a table cannot be a statement. */
const REQUIRED = ["date", "amount", "tagPlate"] as const;

export function classifyHeaders(headers: string[]): Set<string> {
  const found = new Set<string>();
  for (const header of headers) {
    for (const [name, pattern] of Object.entries(COLUMN_PATTERNS)) {
      if (pattern.test(header)) found.add(name);
    }
  }
  return found;
}

/**
 * The table most likely to be the account activity, or null.
 *
 * Structural rather than selector-based on purpose: a CSS class can be
 * rewritten by a deploy, but the activity table will always be the table with a
 * date, an amount and a tag or plate in its header and the most rows under it.
 */
export function pickActivityTable(tables: ScrapedTable[]): ScrapedTable | null {
  let best: ScrapedTable | null = null;
  let bestScore = -1;
  for (const table of tables) {
    if (!table.rows.length) continue;
    const found = classifyHeaders(table.headers);
    if (!REQUIRED.every((name) => found.has(name))) continue;
    // More recognised columns wins; a tie goes to the longer table, because a
    // summary box can carry the same headers over a single row.
    const score = found.size * 1000 + Math.min(table.rows.length, 999);
    if (score > bestScore) {
      best = table;
      bestScore = score;
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Row cleanup
// ---------------------------------------------------------------------------
export function cleanCell(value: string): string {
  // Table cells wrap, carry non-breaking spaces and sometimes a second line of
  // small print. The CSV wants one line.
  return value.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}

/**
 * Rows worth writing out.
 *
 * Drops the ones a table has that a CSV does not: a "no records found" row
 * spanning every column, a totals row, and anything whose cell count does not
 * line up with the header. A short row is not repaired — a row whose columns
 * cannot be identified is a row whose amount cannot be either.
 */
export function usableRows(table: ScrapedTable): string[][] {
  const width = table.headers.length;
  return table.rows
    .map((row) => row.map(cleanCell))
    .filter((row) => row.length === width && row.some((cell) => cell !== ""));
}

export function toCsv(headers: string[], rows: string[][]): string {
  const escape = (value: string): string =>
    /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  const lines = [headers.map(cleanCell).map(escape).join(",")];
  for (const row of rows) lines.push(row.map(escape).join(","));
  return lines.join("\n") + "\n";
}

const AMOUNT_HEADER = /amount|debit|credit|charge|\bfee\b|\btoll\b/i;

/** True when no amount cell on the table carries a minus sign. */
export function amountsLookPositive(headers: string[], rows: string[][]): boolean {
  const columns = headers
    .map((header, index) => (AMOUNT_HEADER.test(header) ? index : -1))
    .filter((index) => index >= 0);
  if (!columns.length) return false;
  let sawANumber = false;
  for (const row of rows) {
    for (const index of columns) {
      const cell = row[index] ?? "";
      if (!/\d/.test(cell)) continue;
      sawANumber = true;
      // "($2.86)" is accounting for negative, and so is a leading or trailing
      // minus. Any of them means the page does distinguish charges from
      // credits and there is nothing to warn about.
      if (/-|\(.*\d.*\)/.test(cell)) return false;
    }
  }
  return sawANumber;
}

/**
 * The statement to send, from whatever the page offered.
 *
 * A download the site generated beats a table scraped out of the DOM every
 * time: it is the format the API's parser was written against, it carries
 * columns the page may not show, and it is not paginated. The table is the
 * fallback for when no such link exists.
 */
export function chooseStatement(page: ScrapedPage): StatementChoice {
  if (page.downloadedCsv && page.downloadedCsv.trim()) {
    const [firstLine = ""] = page.downloadedCsv.split(/\r?\n/);
    const lines = page.downloadedCsv.split(/\r?\n/).filter((line) => line.trim());
    return {
      csv: page.downloadedCsv,
      source: "download",
      headers: firstLine.split(",").map((h) => h.replace(/^"|"$/g, "").trim()),
      rowCount: Math.max(lines.length - 1, 0),
      amountsLookPositive: false,
    };
  }

  const table = pickActivityTable(page.tables);
  if (!table) {
    return { csv: null, source: "none", headers: [], rowCount: 0, amountsLookPositive: false };
  }
  const rows = usableRows(table);
  if (!rows.length) {
    return {
      csv: null,
      source: "none",
      headers: table.headers.map(cleanCell),
      rowCount: 0,
      amountsLookPositive: false,
    };
  }
  const headers = table.headers.map(cleanCell);
  return {
    csv: toCsv(headers, rows),
    source: "table",
    headers,
    rowCount: rows.length,
    amountsLookPositive: amountsLookPositive(headers, rows),
  };
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------
/**
 * Mask the values but keep their shape.
 *
 * For when nothing on the page matched and the markup has to be described back
 * to somebody who cannot see it. Digits become `#` and letters `a`, so a tag
 * number, a plate and a dollar amount are each recognisable as a format
 * without being readable as data. Header text is left alone: a column name is
 * not account data, and it is the whole thing worth knowing.
 */
export function maskValue(value: string): string {
  return cleanCell(value).replace(/\d/g, "#").replace(/[A-Za-z]/g, "a");
}

export function describeShape(page: ScrapedPage): string {
  const lines = [`url: ${page.url.replace(/\?.*$/, "")}`];
  lines.push(`download links: ${page.downloadLinks.length}`);
  for (const href of page.downloadLinks.slice(0, 5)) {
    lines.push(`  ${href.replace(/\?.*$/, "")}`);
  }
  lines.push(`tables: ${page.tables.length}`);
  page.tables.forEach((table, index) => {
    const found = [...classifyHeaders(table.headers)].sort().join(",") || "none";
    lines.push(`  [${index}] ${table.rows.length} rows, recognised: ${found}`);
    lines.push(`      headers: ${table.headers.map(cleanCell).join(" | ")}`);
    const sample = table.rows[0];
    if (sample) lines.push(`      shape:   ${sample.map(maskValue).join(" | ")}`);
  });
  return lines.join("\n");
}
