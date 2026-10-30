/**
 * Calendar prices: read them, read the market, set them on chosen dates.
 *
 * The operator's decision: Claude sets prices directly. What keeps that safe
 * is checked here, against the live calendar, before anything is written:
 *
 * - only dates Turo marks editable and no reservation touches (a booked day
 *   already has its price; a trip's first or last day is left alone too);
 * - nothing in the past, nothing more than a year out;
 * - whole dollars within Turo's own minimum and maximum for the car.
 *
 * A request with any date that fails is refused whole, naming the dates, so
 * what is written is exactly what was asked. A write is answered with a
 * request id before any date is evaluated; each date's outcome is read from
 * the status call (SUCCESS, FAILED, or clamped to the floor or ceiling), and
 * then the calendar is read again. The answer carries every date's price and
 * source before and after, and the change that puts them back: a date that
 * was Turo's automatic price goes back to automatic, a date that had the
 * host's own price gets that price again.
 *
 * Shapes as exercised on the live account (turo-host-api-findings.md,
 * "Calendar price reads, re-exercised for the price tools").
 */

import type { Caller, SettingOutcome } from "./turo-settings.js";

type Values = Record<string, unknown>;
type Money = { amount?: unknown } | null | undefined;

export const PRICING_OPERATIONS = ["price_calendar", "market", "prices"] as const;

/** At most this many days a price_calendar read returns. */
const MAX_DAYS = 120;
/** Turo answers a calendar read of more than about 60 days with a 400. */
const PAGE_DAYS = 60;
/** Turo's status says SUCCESS before its calendar shows the new price. */
const CATCH_UP_TRIES = 6;
const CATCH_UP_MS = 2000;
/** How far ahead a price may be set. */
const MAX_AHEAD_DAYS = 366;

export interface CalendarDay {
  date: string;
  price: number | null;
  source: string | null;
  editable: boolean;
  /** A reservation touches the day: inside it, or its first or last day. */
  booked: boolean;
  /** The host blocked the day. */
  blocked: boolean;
  min: number | null;
  max: number | null;
  /** Above this Turo calls the price high for the day. */
  overpriced: number | null;
  /** Turo's automatic price for the day, from the price chart, when it has one. */
  automatic?: number | null;
}

class Refused extends Error {}

function amount(money: Money): number | null {
  const value = money && typeof money === "object" ? Number(money.amount) : NaN;
  return Number.isFinite(value) ? value : null;
}

function iso(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return iso(d);
}

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

/** One car's calendar day as the tools speak of it. */
export function calendarDay(raw: Record<string, unknown>): CalendarDay {
  const price = (raw.price ?? {}) as { price?: Money; source?: unknown };
  const holds = Array.isArray(raw.unavailabilities) ? (raw.unavailabilities as Record<string, unknown>[]) : [];
  return {
    date: String(raw.date),
    price: amount(price.price),
    source: typeof price.source === "string" ? price.source : null,
    editable: raw.priceEditable === true,
    booked: holds.some((h) => h.unavailabilityReason === "RESERVATION"),
    blocked: holds.some((h) => h.unavailabilityReason === "CUSTOM"),
    min: amount(raw.minPrice as Money),
    max: amount(raw.maxPrice as Money),
    overpriced: amount(raw.overpricedThreshold as Money),
  };
}

async function readCalendar(call: Caller, vehicle: string, start: string, days: number): Promise<CalendarDay[]> {
  const reply = await call({
    method: "POST",
    path: "/api/fleet/calendar",
    json: { daysPerPage: days, includeVehicleDetails: true, startDate: start, vehicleIds: [Number(vehicle)] },
  });
  if (reply.status !== 200) throw new Error(`Turo answered ${reply.status} reading the calendar`);
  const fleet = ((reply.body ?? {}) as { fleetCalendar?: Record<string, unknown>[] }).fleetCalendar ?? [];
  // Keyed on the vehicle's own id, not on position in the list.
  const mine = fleet.find((v) => String((v.vehicleDetails as Record<string, unknown> | null)?.id) === vehicle) ?? (fleet.length === 1 ? fleet[0] : undefined);
  if (!mine) throw new Error("the calendar did not include this car");
  return ((mine.dailyData ?? []) as Record<string, unknown>[]).map(calendarDay);
}

async function readAutomatic(call: Caller, vehicle: string): Promise<Map<string, number | null>> {
  const reply = await call({ method: "GET", path: `/api/fleet/vehicle/price-chart/${encodeURIComponent(vehicle)}` });
  const out = new Map<string, number | null>();
  if (reply.status !== 200) return out;
  const chart = ((reply.body ?? {}) as { chartData?: Record<string, unknown>[] }).chartData ?? [];
  for (const point of chart) out.set(String(point.date), amount(point.dynamicPrice as Money));
  return out;
}

/** The market around the car by month: comparable cars' prices and how busy they are. */
export function marketSummary(body: Record<string, unknown>): Values {
  const months = (set: unknown) => {
    const data = ((set ?? {}) as { intervalData?: Record<string, unknown>[] }).intervalData ?? [];
    return data
      .filter((m) => m.hasInsightsForInterval !== false)
      .map((m) => ({
        month: String(m.intervalDate).slice(0, 7),
        median: amount(m.medianPrice as Money),
        weekday: amount(m.weekdayPrice as Money),
        weekend: amount(m.weekendPrice as Money),
        cars_weekday: m.weekdayNumberOfVehicles ?? null,
        cars_weekend: m.weekendNumberOfVehicles ?? null,
        utilization_percent: m.utilizationPercentage ?? null,
        radius_miles: ((m.radius ?? {}) as { scalar?: unknown }).scalar ?? null,
      }));
  };
  return {
    same_make_model: months(body.sameMakeModels),
    similar_vehicles: months(body.similarVehicles),
  };
}

interface PriceAsk {
  /** date -> whole dollars */
  fixed: Map<string, number>;
  /** dates back to Turo's automatic price */
  automatic: string[];
}

export function parseAsk(values: Values): PriceAsk {
  const fixed = new Map<string, number>();
  const automatic: string[] = [];
  const unknown = Object.keys(values).filter((k) => k !== "prices" && k !== "automatic");
  if (unknown.length) throw new Refused(`unknown value ${unknown.join(", ")}; prices takes prices and automatic`);
  const prices = values.prices ?? [];
  if (!Array.isArray(prices)) throw new Refused("prices is a list of {date, amount}");
  for (const item of prices as Values[]) {
    const date = String(item?.date ?? "");
    const value = item?.amount;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Refused(`date ${date || "(none)"} is not YYYY-MM-DD`);
    if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
      throw new Refused(`the price for ${date} must be whole dollars`);
    }
    if (fixed.has(date)) throw new Refused(`${date} is given twice`);
    fixed.set(date, value);
  }
  const back = values.automatic ?? [];
  if (!Array.isArray(back)) throw new Refused("automatic is a list of dates");
  for (const raw of back) {
    const date = String(raw);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Refused(`date ${date} is not YYYY-MM-DD`);
    if (fixed.has(date)) throw new Refused(`${date} is both priced and set back to automatic`);
    automatic.push(date);
  }
  if (!fixed.size && !automatic.length) throw new Refused("nothing to change");
  return { fixed, automatic };
}

/** Why each date cannot be changed; empty when all can. */
export function problems(ask: PriceAsk, days: Map<string, CalendarDay>, today: string): string[] {
  const out: string[] = [];
  const dates = [...ask.fixed.keys(), ...ask.automatic];
  for (const date of dates) {
    const day = days.get(date);
    if (date < today) out.push(`${date} is past`);
    else if (daysBetween(today, date) > MAX_AHEAD_DAYS) out.push(`${date} is more than a year out`);
    else if (!day) out.push(`${date} is not on the calendar`);
    else if (day.booked) out.push(`${date} has a reservation`);
    else if (!day.editable) out.push(`${date} cannot be priced`);
    else {
      const wanted = ask.fixed.get(date);
      if (wanted !== undefined) {
        if (day.min !== null && wanted < day.min) out.push(`${date}: ${wanted} is below Turo's minimum ${day.min}`);
        if (day.max !== null && wanted > day.max) out.push(`${date}: ${wanted} is above Turo's maximum ${day.max}`);
      }
    }
  }
  return out;
}

type Wait = (ms: number) => Promise<void>;

/** Each date's outcome for one write, once none is pending. */
async function settle(
  call: Caller,
  wait: Wait,
  vehicle: string,
  requestId: string,
  isDefault: boolean
): Promise<Map<string, string>> {
  const outcome = new Map<string, string>();
  for (let tries = 0; tries < 15; tries++) {
    await wait(1000);
    const reply = await call({
      method: "GET",
      path: `/api/fleet/pricing/status?requestId=${encodeURIComponent(requestId)}&vehicleId=${encodeURIComponent(vehicle)}${isDefault ? "&updateType=DEFAULT" : ""}`,
    });
    if (reply.status !== 200) continue;
    const statuses = ((reply.body ?? {}) as { datePriceStatuses?: Record<string, unknown>[] }).datePriceStatuses ?? [];
    outcome.clear();
    for (const s of statuses) outcome.set(String(s.date), String(s.dateStatus));
    if (statuses.length && ![...outcome.values()].includes("PENDING")) return outcome;
  }
  return outcome;
}

function span(dates: string[]): { start: string; days: number } {
  const sorted = [...dates].sort();
  return { start: sorted[0], days: daysBetween(sorted[0], sorted[sorted.length - 1]) + 1 };
}

/** The calendar from `start` for `days`, in pages Turo will answer. */
async function readDays(call: Caller, vehicle: string, start: string, days: number): Promise<CalendarDay[]> {
  const out: CalendarDay[] = [];
  for (let offset = 0; offset < days; offset += PAGE_DAYS) {
    out.push(...(await readCalendar(call, vehicle, addDays(start, offset), Math.min(PAGE_DAYS, days - offset))));
  }
  return out;
}

async function readSpan(call: Caller, vehicle: string, dates: string[]): Promise<Map<string, CalendarDay>> {
  const { start, days } = span(dates);
  return new Map((await readDays(call, vehicle, start, days)).map((day) => [day.date, day]));
}

export async function runPricing(
  operation: string,
  mode: "read" | "write",
  vehicle: string,
  values: Values,
  call: Caller,
  wait: Wait,
  today: string = iso(new Date())
): Promise<SettingOutcome> {
  try {
    if (operation === "price_calendar") {
      if (mode === "write") return { ok: false, result: "price_calendar is read; prices changes it" };
      const start = typeof values.startDate === "string" && /^\d{4}-\d{2}-\d{2}$/.test(values.startDate) ? values.startDate : today;
      const days = Math.max(1, Math.min(MAX_DAYS, Number(values.days) || 60));
      const calendar = await readDays(call, vehicle, start, days);
      const automatic = await readAutomatic(call, vehicle);
      for (const day of calendar) if (automatic.has(day.date)) day.automatic = automatic.get(day.date);
      const booked = calendar.filter((d) => d.booked).length;
      return { ok: true, result: `read ${calendar.length} days, ${booked} booked`, detail: { current: calendar } };
    }
    if (operation === "market") {
      if (mode === "write") return { ok: false, result: "market is read only" };
      const reply = await call({ method: "GET", path: `/api/vehicle/${encodeURIComponent(vehicle)}/pricing-insights` });
      if (reply.status !== 200) throw new Error(`Turo answered ${reply.status} reading pricing insights`);
      // The sample listings Turo can show carry other hosts' plates and VINs;
      // only the monthly figures are read.
      return { ok: true, result: "read the market", detail: { current: marketSummary((reply.body ?? {}) as Values) } };
    }
    if (mode !== "write") return { ok: false, result: "prices is a change; read price_calendar" };

    const ask = parseAsk(values);
    const dates = [...ask.fixed.keys(), ...ask.automatic];
    const before = await readSpan(call, vehicle, dates);
    const refused = problems(ask, before, today);
    if (refused.length) return { ok: false, result: `nothing changed: ${refused.join("; ")}` };

    const outcomes = new Map<string, string>();
    const byAmount = new Map<number, string[]>();
    for (const [date, value] of ask.fixed) byAmount.set(value, [...(byAmount.get(value) ?? []), date]);
    const vehicleId = Number(vehicle);
    for (const [value, group] of byAmount) {
      const reply = await call({
        method: "PUT",
        path: "/api/fleet/pricing",
        json: { updateAmount: value, updateType: "FIXED_AMOUNT", vehicleDates: [{ vehicleId, dates: group.sort() }] },
      });
      const requestId = (reply.body as { requestId?: unknown } | null)?.requestId;
      if (reply.status !== 200 || requestId === undefined) throw new Error(`Turo answered ${reply.status} to a price change`);
      for (const [date, status] of await settle(call, wait, vehicle, String(requestId), false)) outcomes.set(date, status);
    }
    if (ask.automatic.length) {
      const reply = await call({ method: "PUT", path: "/api/fleet/default-pricing", json: { vehicleDates: [{ vehicleId, dates: [...ask.automatic].sort() }] } });
      const requestId = (reply.body as { requestId?: unknown } | null)?.requestId;
      if (reply.status !== 200 || requestId === undefined) throw new Error(`Turo answered ${reply.status} setting prices back to automatic`);
      for (const [date, status] of await settle(call, wait, vehicle, String(requestId), true)) outcomes.set(date, status);
    }

    const notShown = (after: Map<string, CalendarDay>): string[] => [
      ...[...ask.fixed].filter(([date, value]) => after.get(date)?.price !== value || after.get(date)?.source !== "OWNER").map(([date]) => date),
      ...ask.automatic.filter((date) => after.get(date)?.source !== "DYNAMIC"),
    ];
    // Read back until the calendar shows what Turo's status says it took: it
    // lags the status by seconds. A date Turo failed is not waited for.
    const took = (date: string) => ["SUCCESS", "CEILING_OVERRIDE", "FLOOR_OVERRIDE"].includes(outcomes.get(date) ?? "");
    let after = await readSpan(call, vehicle, dates);
    for (let tries = 1; tries < CATCH_UP_TRIES && notShown(after).some(took); tries++) {
      await wait(CATCH_UP_MS);
      after = await readSpan(call, vehicle, dates);
    }
    const shown = (map: Map<string, CalendarDay>) =>
      Object.fromEntries(dates.sort().map((d) => [d, { price: map.get(d)?.price ?? null, source: map.get(d)?.source ?? null }]));
    const missed = notShown(after).map((date) => {
      const now = after.get(date)?.price;
      return `${date} (${outcomes.get(date) ?? "no status"}${now != null ? `, now ${now}` : ""})`;
    });
    // What puts every asked date back, whether or not it took.
    const restore = {
      operation: "prices",
      values: {
        prices: dates.filter((d) => before.get(d)?.source === "OWNER" && before.get(d)?.price != null)
          .map((d) => ({ date: d, amount: Math.round(before.get(d)?.price as number) })),
        automatic: dates.filter((d) => before.get(d)?.source !== "OWNER"),
      },
    };
    return {
      ok: missed.length === 0,
      result: missed.length
        ? `${dates.length - missed.length} of ${dates.length} dates changed; not: ${missed.join(", ")}`
        : `${dates.length} dates changed`,
      detail: { before: shown(before), after: shown(after), restore },
    };
  } catch (error) {
    if (error instanceof Refused) return { ok: false, result: error.message };
    return { ok: false, result: error instanceof Error ? error.message : String(error) };
  }
}
