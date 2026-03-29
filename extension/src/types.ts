export interface TuroTrip {
  tripId: string;
  startTime: string; // ISO 8601
  endTime: string;   // ISO 8601
  licensePlate: string;
}

export type MessageType =
  | { type: "SCRAPE_TRIPS" }
  | { type: "TRIPS_RESULT"; trips: TuroTrip[]; error?: never }
  | { type: "TRIPS_ERROR"; error: string; trips?: never }
  | { type: "DOWNLOAD_CSV"; trips: TuroTrip[] }
  | { type: "FETCH_DETAIL"; tripId: string }
  | { type: "DETAIL_RESULT"; tripId: string; scheduleDates: string[]; scheduleTimes: string[] }
  | { type: "DETAIL_ERROR"; tripId: string; error: string };
