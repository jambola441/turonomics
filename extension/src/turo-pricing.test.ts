/**
 * Calendar prices against a fake Turo that behaves as the real one was seen
 * to: a write answered with a request id before any date is looked at, each
 * date's outcome on the status call, a booked day not editable.
 *
 * What matters: nothing is written for a booked, past or out-of-range date;
 * a date Turo did not take is said; and every change carries what puts each
 * date back, automatic to automatic and the host's own price to that price.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { runSetting, type TuroCall, type TuroReply } from "./turo-settings.js";

const CAR = "3218625";
const TODAY = new Date().toISOString().slice(0, 10);

function day(n: number): string {
  const d = new Date(`${TODAY}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

interface Day { price: number; source: "OWNER" | "DYNAMIC"; booked: boolean }

function fakeTuro(options: { fail?: string[]; lagReads?: number } = {}) {
  const calls: TuroCall[] = [];
  const days = new Map<string, Day>();
  for (let n = -5; n < 400; n++) days.set(day(n), { price: 60, source: "DYNAMIC", booked: false });
  days.set(day(3), { price: 80, source: "OWNER", booked: false });
  days.set(day(5), { price: 70, source: "DYNAMIC", booked: true });
  const requests = new Map<string, { date: string; dateStatus: string }[]>();
  let nextRequest = 1;
  // The calendar as it was before the last write, shown for this many reads.
  let stale: Map<string, Day> | null = null;
  let staleReads = 0;
  const money = (amount: number) => ({ amount, currencyCode: "USD" });

  async function call(c: TuroCall): Promise<TuroReply> {
    calls.push(c);
    const path = c.path.split("?")[0];
    if (c.method === "POST" && path === "/api/fleet/calendar") {
      const body = c.json as { startDate: string; daysPerPage: number };
      // Turo answers a long calendar read with a 400.
      if (body.daysPerPage > 60) return { status: 400, body: null };
      const shownDays = staleReads > 0 && stale ? stale : days;
      if (staleReads > 0) staleReads--;
      const dailyData = [];
      for (let i = 0; i < body.daysPerPage; i++) {
        const d = new Date(`${body.startDate}T00:00:00Z`);
        d.setUTCDate(d.getUTCDate() + i);
        const date = d.toISOString().slice(0, 10);
        const x = shownDays.get(date) ?? { price: 60, source: "DYNAMIC", booked: false };
        dailyData.push({
          date,
          price: { price: money(x.price), source: x.source },
          priceEditable: !x.booked,
          minPrice: money(30),
          maxPrice: money(500),
          overpricedThreshold: x.booked ? null : money(90),
          unavailabilities: x.booked ? [{ id: 1, unavailabilityReason: "RESERVATION", reservationStatus: "APPROVED" }] : [],
        });
      }
      return { status: 200, body: { fleetCalendar: [{ vehicleDetails: { id: Number(CAR) }, dailyData }] } };
    }
    if (c.method === "GET" && path === `/api/fleet/vehicle/price-chart/${CAR}`) {
      return { status: 200, body: { chartData: [{ date: day(1), dailyPrice: money(60), dynamicPrice: money(64) }] } };
    }
    if (c.method === "PUT" && (path === "/api/fleet/pricing" || path === "/api/fleet/default-pricing")) {
      const body = c.json as { updateAmount?: number; vehicleDates: { dates: string[] }[] };
      const id = String(nextRequest++);
      if (options.lagReads) {
        stale = new Map([...days].map(([k, v]) => [k, { ...v }]));
        staleReads = options.lagReads;
      }
      const statuses = body.vehicleDates[0].dates.map((date) => {
        if (options.fail?.includes(date)) return { date, dateStatus: "FAILED" };
        const x = days.get(date) as Day;
        if (path === "/api/fleet/default-pricing") days.set(date, { ...x, price: 60, source: "DYNAMIC" });
        else days.set(date, { ...x, price: body.updateAmount as number, source: "OWNER" });
        return { date, dateStatus: "SUCCESS" };
      });
      requests.set(id, statuses);
      return { status: 200, body: { requestId: id } };
    }
    if (c.method === "GET" && path === "/api/fleet/pricing/status") {
      const id = new URLSearchParams(c.path.split("?")[1]).get("requestId") as string;
      return { status: 200, body: { requestId: id, vehicleStatus: "SUCCESS", datePriceStatuses: requests.get(id) ?? [] } };
    }
    if (c.method === "GET" && path === `/api/vehicle/${CAR}/pricing-insights`) {
      return {
        status: 200,
        body: {
          sameMakeModels: { intervalData: [{ intervalDate: "2026-11-01", medianPrice: money(70), weekdayPrice: money(62), weekendPrice: money(85), weekdayNumberOfVehicles: 14, weekendNumberOfVehicles: 9, utilizationPercentage: 61, radius: { scalar: 5, unit: "MI" }, hasInsightsForInterval: true }] },
          similarVehicles: { intervalData: [] },
        },
      };
    }
    return { status: 404, body: null };
  }
  return { calls, days, call };
}

const noWait = async () => undefined;
const writes = (calls: TuroCall[]) => calls.filter((c) => c.method === "PUT");

test("prices are set on the dates asked, and the change says how to put each back", async () => {
  const turo = fakeTuro();
  const out = await runSetting(
    { operation: "prices", mode: "write", vehicle_id: CAR, values: { prices: [{ date: day(2), amount: 95 }, { date: day(3), amount: 95 }, { date: day(4), amount: 70 }] } },
    turo.call, noWait
  );
  assert.equal(out.ok, true, out.result);
  assert.equal(writes(turo.calls).length, 2, "one write per price");
  assert.deepEqual(turo.days.get(day(2)), { price: 95, source: "OWNER", booked: false });
  assert.deepEqual(out.detail?.restore, {
    operation: "prices",
    values: { prices: [{ date: day(3), amount: 80 }], automatic: [day(2), day(4)] },
  });

  const back = await runSetting({ ...out.detail!.restore!, mode: "write", vehicle_id: CAR }, turo.call, noWait);
  assert.equal(back.ok, true, back.result);
  assert.deepEqual(turo.days.get(day(3)), { price: 80, source: "OWNER", booked: false });
  assert.equal(turo.days.get(day(2))?.source, "DYNAMIC");
});

test("a booked date is refused, and nothing at all is written", async () => {
  const turo = fakeTuro();
  const out = await runSetting(
    { operation: "prices", mode: "write", vehicle_id: CAR, values: { prices: [{ date: day(4), amount: 90 }, { date: day(5), amount: 90 }] } },
    turo.call, noWait
  );
  assert.equal(out.ok, false);
  assert.match(out.result, new RegExp(`${day(5)} has a reservation`));
  assert.equal(writes(turo.calls).length, 0);
  assert.equal(turo.days.get(day(4))?.price, 60, "the good date was not written either");
});

for (const [name, values, said] of [
  ["below Turo's minimum", { prices: [{ date: day(2), amount: 20 }] }, /below Turo's minimum 30/],
  ["above Turo's maximum", { prices: [{ date: day(2), amount: 900 }] }, /above Turo's maximum 500/],
  ["in the past", { prices: [{ date: day(-2), amount: 90 }] }, /is past/],
  ["more than a year out", { prices: [{ date: day(380), amount: 90 }] }, /more than a year out/],
  ["not whole dollars", { prices: [{ date: day(2), amount: 89.5 }] }, /whole dollars/],
  ["a date given twice", { prices: [{ date: day(2), amount: 90 }, { date: day(2), amount: 91 }] }, /given twice/],
  ["an unknown value", { price: [{ date: day(2), amount: 90 }] }, /unknown value price/],
] as const) {
  test(`a price ${name} is refused before anything is written`, async () => {
    const turo = fakeTuro();
    const out = await runSetting({ operation: "prices", mode: "write", vehicle_id: CAR, values }, turo.call, noWait);
    assert.equal(out.ok, false);
    assert.match(out.result, said);
    assert.equal(writes(turo.calls).length, 0);
  });
}

test("a date Turo fails is named, and the rest are kept", async () => {
  const turo = fakeTuro({ fail: [day(7)] });
  const out = await runSetting(
    { operation: "prices", mode: "write", vehicle_id: CAR, values: { prices: [{ date: day(6), amount: 99 }, { date: day(7), amount: 99 }] } },
    turo.call, noWait
  );
  assert.equal(out.ok, false);
  assert.match(out.result, new RegExp(`1 of 2 dates changed; not: ${day(7)} \\(FAILED`));
  assert.equal(turo.days.get(day(6))?.price, 99);
  assert.ok(out.detail?.restore, "the date that took can still be put back");
});

test("a calendar slow to show the new prices is waited for, not reported failed", async () => {
  const turo = fakeTuro({ lagReads: 2 });
  const waits: number[] = [];
  const out = await runSetting(
    { operation: "prices", mode: "write", vehicle_id: CAR, values: { prices: [{ date: day(2), amount: 95 }] } },
    turo.call, async (ms) => { waits.push(ms); }
  );
  assert.equal(out.ok, true, out.result);
  assert.deepEqual((out.detail?.after as Record<string, { price: number }>)[day(2)].price, 95);
  assert.ok(waits.filter((ms) => ms === 2000).length >= 2, "it waited for the calendar");
});

test("a date Turo failed is not waited for", async () => {
  const turo = fakeTuro({ fail: [day(2)] });
  const waits: number[] = [];
  await runSetting(
    { operation: "prices", mode: "write", vehicle_id: CAR, values: { prices: [{ date: day(2), amount: 95 }] } },
    turo.call, async (ms) => { waits.push(ms); }
  );
  assert.equal(waits.filter((ms) => ms === 2000).length, 0);
});

test("a long calendar read is made in pages Turo answers", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "price_calendar", mode: "read", vehicle_id: CAR, values: { days: 120 } }, turo.call, noWait);
  assert.equal(out.ok, true, out.result);
  assert.equal((out.detail?.current as unknown[]).length, 120);
  const pages = turo.calls.filter((c) => c.path === "/api/fleet/calendar");
  assert.ok(pages.every((c) => (c.json as { daysPerPage: number }).daysPerPage <= 60));
  const wide = await runSetting(
    { operation: "prices", mode: "write", vehicle_id: CAR, values: { prices: [{ date: day(2), amount: 95 }, { date: day(100), amount: 95 }] } },
    turo.call, noWait
  );
  assert.equal(wide.ok, true, wide.result);
});

test("dates go back to Turo's automatic price", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "prices", mode: "write", vehicle_id: CAR, values: { automatic: [day(3)] } }, turo.call, noWait);
  assert.equal(out.ok, true, out.result);
  assert.equal(writes(turo.calls)[0].path, "/api/fleet/default-pricing");
  assert.equal(turo.days.get(day(3))?.source, "DYNAMIC");
  assert.deepEqual(out.detail?.restore?.values, { prices: [{ date: day(3), amount: 80 }], automatic: [] });
});

test("the calendar read says which days are booked and Turo's own price for each", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "price_calendar", mode: "read", vehicle_id: CAR, values: { days: 7 } }, turo.call, noWait);
  assert.equal(out.ok, true, out.result);
  const days = out.detail?.current as { date: string; booked: boolean; editable: boolean; automatic?: number; min: number; max: number }[];
  assert.equal(days.length, 7);
  assert.equal(days.find((d) => d.date === day(5))?.booked, true);
  assert.equal(days.find((d) => d.date === day(5))?.editable, false);
  assert.equal(days.find((d) => d.date === day(1))?.automatic, 64);
  assert.equal(days[0].min, 30);
  assert.equal(writes(turo.calls).length, 0);
});

test("the market read is monthly figures, never other hosts' listings", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "market", mode: "read", vehicle_id: CAR, values: {} }, turo.call, noWait);
  assert.equal(out.ok, true, out.result);
  const market = out.detail?.current as { same_make_model: { median: number; utilization_percent: number }[] };
  assert.equal(market.same_make_model[0].median, 70);
  assert.equal(market.same_make_model[0].utilization_percent, 61);
  assert.ok(!turo.calls.some((c) => c.path.includes("sample-vehicles")));
});

test("prices need a car", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "prices", mode: "write", values: { automatic: [day(2)] } }, turo.call, noWait);
  assert.equal(out.ok, false);
  assert.equal(turo.calls.length, 0);
});
