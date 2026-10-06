export interface TuroTrip {
  tripId: string;
  startTime: string; // ISO 8601
  endTime: string;   // ISO 8601
  licensePlate: string;
}

/** What the API's /api/tolls/import says it did. */
export interface ImportResult {
  rows: number;
  imported: number;
  already_known: number;
  matched: number;
  unmatched: number;
  unknown_tags: string[];
}

export interface SendTollsResult {
  /** How many pages of the statement were read, and why reading stopped. */
  pagesRead?: number;
  pagingStopped?: string;
  /** Null when the page yielded no statement; the report says what it had. */
  result: ImportResult | null;
  source: "download" | "table" | "none";
  rowCount: number;
  /** Set when the API refused the file, verbatim — it names the columns. */
  problem?: string;
  /** The masked page description, for when there was nothing to send. */
  report?: string;
  /** The page shows charges as positive, which the API reads as payments. */
  amountsLookPositive?: boolean;
}

/** What the API says it wants fetched, and from where. */
export interface TuroWanted {
  reservations: string[];
  detail_path: string;
  token_required: boolean;
  /** Invoices whose breakdown the mail did not give, as [reservation, invoice]. */
  invoices?: [string, string][];
  invoice_path?: string;
  hub_path?: string;
}

/** What the API made of the invoice pages it was sent. */
export interface TuroInvoicesResult {
  seen: number;
  unparsed: number;
  matched: number;
  created: number;
  /** One line per invoice whose toll share is now known. */
  itemised: string[];
  tolls_asked: number;
  tolls_recovered: number;
  statuses: string[];
  /** How many were fetched, and how many Turo would not hand over. */
  asked: number;
  failed: number;
  /** How many invoice hubs were read, and how many invoices they listed. */
  hubs?: number;
  listed?: number;
}

/** What the API says it did with them, plus what the pull itself managed. */
export interface TuroPullResult {
  seen: number;
  stored: number;
  unparsed: number;
  unknown: string[];
  retimed: string[];
  wrong_plate: string[];
  tolls_rematched: number;
  grace_periods: string[];
  /** Reservations whose invoice hub Turo offers, so worth reading. */
  invoice_hubs?: string[];
  /** How many the API asked for, and how many Turo would not hand over. */
  asked: number;
  failed: number;
  invoices?: TuroInvoicesResult;
}

/** One rental's invoice, as the API drafts it. */
export interface Draft {
  trip_id: string;
  turo_trip_id: string | null;
  guest_name: string | null;
  total_cents: number;
  amount_dollars: number;
  message: string;
  days_left: number | null;
  can_file: boolean;
  evidence_svg: string;
}

export interface FileInvoiceResult {
  filed: boolean;
  /** Why not, when it was not. */
  reason?: string;
  guest?: string;
  amountCents?: number;
  reservation?: string;
  daysLeft?: number;
}

export type MessageType =
  | { type: "SCRAPE_TRIPS" }
  | { type: "TRIPS_RESULT"; trips: TuroTrip[]; error?: never }
  | { type: "TRIPS_ERROR"; error: string; trips?: never }
  | { type: "DOWNLOAD_CSV"; trips: TuroTrip[] }
  | { type: "FETCH_DETAIL"; tripId: string }
  | { type: "DETAIL_RESULT"; tripId: string; scheduleDates: string[]; scheduleTimes: string[] }
  | { type: "DETAIL_ERROR"; tripId: string; error: string }
  | { type: "SEND_TOLLS"; tabId: number }
  | { type: "SEND_TOLLS_RESULT"; result: SendTollsResult }
  | { type: "SEND_TOLLS_ERROR"; error: string }
  | { type: "PROBE_TURO"; tabId: number }
  | { type: "PROBE_TURO_RESULT"; report: string }
  | { type: "PROBE_TURO_ERROR"; error: string }
  | { type: "PULL_TURO"; tabId: number }
  | { type: "PULL_TURO_RESULT"; result: TuroPullResult }
  | { type: "PULL_TURO_ERROR"; error: string }
  | { type: "WATCH_TURO"; tabId: number }
  | { type: "WATCH_TURO_RESULT" }
  | { type: "REPORT_WATCH"; tabId: number }
  | { type: "FILE_INVOICE"; tabId: number }
  | { type: "FILE_INVOICE_RESULT"; result: FileInvoiceResult }
  | { type: "FILE_INVOICE_ERROR"; error: string };
