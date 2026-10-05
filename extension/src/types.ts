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
  | { type: "SEND_TOLLS_ERROR"; error: string };
