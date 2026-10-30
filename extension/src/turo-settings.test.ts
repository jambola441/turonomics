/**
 * Tests for Turo's settings, against a fake Turo that behaves as the real one
 * was seen to: guest instructions replace-all, trip preferences ignoring a
 * display string with a 200, an extra's list growing on an add.
 *
 * What matters most: nothing outside the list runs, a write Turo ignores is
 * not reported as made, and every change carries what puts it back.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { enumName, OPERATIONS, runSetting, type TuroCall, type TuroReply } from "./turo-settings.js";

const CAR = "3218625";

function fakeTuro() {
  const calls: TuroCall[] = [];
  const state = {
    guest: { welcomeMessage: "Hi", pickupReturnInstructions: "Lockbox", carGuide: null as string | null },
    prefs: { bufferTime: { currentName: "THREE_HOURS", currentValue: "3 hours" }, longerWeekendTripPreferred: false },
    images: [11, 12, 13],
    extras: [{ extraId: 7, description: "Cooler", enabled: true, extraPricingType: "PER_TRIP", extraType: { value: "COOLER" }, priceWithCurrency: { amount: 10, currencyCode: "USD" }, quantity: 1 }],
    nextExtra: 8,
    sms: true,
  };
  const ok = (body: unknown = null): TuroReply => ({ status: 200, body });
  async function call(c: TuroCall): Promise<TuroReply> {
    calls.push(c);
    const path = c.path.split("?")[0];
    if (c.method === "GET" && path === `/api/vehicle/${CAR}/guest-instructions`) return ok({ ...state.guest });
    if (c.method === "PUT" && path === `/api/vehicle/${CAR}/guest-instructions`) {
      // Replace-all: a key left out is nulled.
      const body = c.json as Record<string, string | null>;
      state.guest = { welcomeMessage: body.welcomeMessage ?? null, pickupReturnInstructions: body.pickupReturnInstructions ?? null, carGuide: body.carGuide ?? null } as typeof state.guest;
      return ok();
    }
    if (c.method === "GET" && path === `/api/vehicle/${CAR}/trip-preferences`) return ok(JSON.parse(JSON.stringify(state.prefs)));
    if (c.method === "PATCH" && path === `/api/vehicle/${CAR}/trip-preferences`) {
      const body = c.json as Record<string, unknown>;
      // A display string is answered 200 and changes nothing, as Turo does.
      if (typeof body.bufferTime === "string" && /^[A-Z_]+$/.test(body.bufferTime)) {
        state.prefs.bufferTime = { currentName: body.bufferTime, currentValue: "?" };
      }
      return ok();
    }
    if (c.method === "GET" && path === "/api/vehicle/listing") return ok({ name: "Jolene", description: "A car", images: state.images.map((id) => ({ id })), badges: [] });
    if (c.method === "POST" && path === "/api/vehicle/set_image_ordering") {
      state.images = ((c.form as Record<string, string[]>).images).map(Number);
      return ok();
    }
    if (c.method === "GET" && path === "/api/your_car/extras") return ok({ extras: state.extras.map((e) => ({ ...e })) });
    if (c.method === "GET" && path === "/api/extras") return ok({ extraTypes: [{ value: "COOLER" }] });
    if (c.method === "POST" && path === "/api/your_car/extras") {
      const f = c.form as Record<string, string>;
      state.extras.push({ extraId: state.nextExtra++, description: f.description, enabled: f.enabled === "true", extraPricingType: f.extraPricingType, extraType: { value: f.extraType }, priceWithCurrency: { amount: Number(f.price), currencyCode: "USD" }, quantity: Number(f.quantity) });
      return ok({ extras: state.extras });
    }
    if (c.method === "DELETE" && path.startsWith("/api/your_car/extras/")) {
      const id = Number(path.split("/").pop());
      state.extras = state.extras.filter((e) => e.extraId !== id);
      return { status: 204, body: null };
    }
    if (c.method === "GET" && path === "/api/me/contact_preferences") return ok([{ name: "x", preferences: [{ type: "sms", enabled: state.sms }] }]);
    if (c.method === "POST" && path === "/api/me/contact_preferences") {
      state.sms = (c.form as Record<string, string>).sms === "true";
      return ok();
    }
    return { status: 404, body: null };
  }
  return { calls, state, call };
}

const writes = (calls: TuroCall[]) => calls.filter((c) => c.method !== "GET");

test("an operation not on the list runs nothing", async () => {
  const turo = fakeTuro();
  for (const operation of ["reservation_cancel", "price", "../api/reservation/approve", ""]) {
    const out = await runSetting({ operation, mode: "write", vehicle_id: CAR, values: { x: 1 } }, turo.call);
    assert.equal(out.ok, false, operation);
  }
  assert.equal(turo.calls.length, 0, "not even a read");
});

test("the list has no prices, blocked days, reservations, money or messages", () => {
  for (const op of OPERATIONS) assert.doesNotMatch(op, /price|pricing|unavailab|reserv|reimburs|refund|message|review|cancel|delete_listing/);
});

test("a car's setting needs the car", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "guest_instructions", mode: "read", values: {} }, turo.call);
  assert.equal(out.ok, false);
  assert.equal(turo.calls.length, 0);
});

test("guest instructions keep what was not asked for, and say how to put it back", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "guest_instructions", mode: "write", vehicle_id: CAR, values: { welcomeMessage: "Welcome!" } }, turo.call);
  assert.equal(out.ok, true, out.result);
  assert.equal(turo.state.guest.pickupReturnInstructions, "Lockbox", "a replace-all write did not null the rest");
  assert.deepEqual(out.detail?.restore, { operation: "guest_instructions", values: { welcomeMessage: "Hi" } });
  const back = await runSetting({ ...out.detail!.restore!, mode: "write", vehicle_id: CAR }, turo.call);
  assert.equal(back.ok, true);
  assert.deepEqual(turo.state.guest, { welcomeMessage: "Hi", pickupReturnInstructions: "Lockbox", carGuide: null });
});

test("a value the setting does not take is refused before anything is written", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "guest_instructions", mode: "write", vehicle_id: CAR, values: { welcomeMesage: "typo" } }, turo.call);
  assert.equal(out.ok, false);
  assert.match(out.result, /welcomeMesage/);
  assert.equal(writes(turo.calls).length, 0);
});

test("a trip preference given as display text is written as its name", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "trip_preferences", mode: "write", vehicle_id: CAR, values: { bufferTime: "4 hours" } }, turo.call);
  assert.equal(out.ok, true, out.result);
  assert.deepEqual(writes(turo.calls)[0].json, { bufferTime: "FOUR_HOURS" });
  assert.deepEqual(out.detail?.restore?.values, { bufferTime: "THREE_HOURS" });
});

test("a write Turo answers OK but ignores is not reported as made", async () => {
  const turo = fakeTuro();
  // The fake, like Turo with a display string, answers 200 to this and
  // changes nothing.
  const out = await runSetting({ operation: "trip_preferences", mode: "write", vehicle_id: CAR, values: { longerWeekendTripPreferred: true } }, turo.call);
  assert.equal(out.ok, false);
  assert.match(out.result, /did not change longerWeekendTripPreferred/);
  assert.ok(out.detail?.restore, "the restore is kept in case part of it took");
});

test("a photo order that leaves a photo out is refused", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "photo_order", mode: "write", vehicle_id: CAR, values: { imageIds: [13, 11] } }, turo.call);
  assert.equal(out.ok, false);
  assert.equal(writes(turo.calls).length, 0);
  const good = await runSetting({ operation: "photo_order", mode: "write", vehicle_id: CAR, values: { imageIds: [13, 11, 12] } }, turo.call);
  assert.equal(good.ok, true, good.result);
  assert.deepEqual(turo.state.images, [13, 11, 12]);
  assert.deepEqual(good.detail?.restore?.values, { imageIds: ["11", "12", "13"] });
});

test("an added extra is undone by removing it, and a removed one by adding it back", async () => {
  const turo = fakeTuro();
  const added = await runSetting({
    operation: "extra_add", mode: "write", vehicle_id: CAR,
    values: { extraType: "COOLER", description: "Second cooler", price: 12, quantity: 1, extraPricingType: "PER_TRIP", enabled: true },
  }, turo.call);
  assert.equal(added.ok, true, added.result);
  assert.deepEqual(added.detail?.restore, { operation: "extra_remove", values: { extraId: "8" } });

  const removed = await runSetting({ operation: "extra_remove", mode: "write", vehicle_id: CAR, values: { extraId: 7 } }, turo.call);
  assert.equal(removed.ok, true, removed.result);
  assert.equal(removed.detail?.restore?.operation, "extra_add");
  assert.equal(removed.detail?.restore?.values.description, "Cooler");
  assert.deepEqual(turo.state.extras.map((e) => e.extraId), [8]);
});

test("a question's id comes from Turo's answer, since its list lags the add", async () => {
  const calls: TuroCall[] = [];
  const call = async (c: TuroCall): Promise<TuroReply> => {
    calls.push(c);
    // The list never shows the new one in time; the POST answers with it.
    if (c.method === "GET") return { status: 200, body: { vehicleFaqs: [{ id: 1, question: "Pets?", answer: "No" }] } };
    return { status: 200, body: { id: 42, question: "Smoking?", answer: "No" } };
  };
  const out = await runSetting({ operation: "faq_add", mode: "write", vehicle_id: CAR, values: { question: "Smoking?", answer: "No" } }, call);
  assert.equal(out.ok, true, out.result);
  assert.deepEqual(out.detail?.restore, { operation: "faq_remove", values: { faqId: "42" } });
  assert.deepEqual(calls.find((c) => c.method === "POST")?.form, { question: "Smoking?", answer: "No" });
});

test("removing something that is not there is refused", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "extra_remove", mode: "write", vehicle_id: CAR, values: { extraId: 99 } }, turo.call);
  assert.equal(out.ok, false);
  assert.equal(writes(turo.calls).length, 0);
});

test("an account setting needs no car, and a read writes nothing", async () => {
  const turo = fakeTuro();
  const read = await runSetting({ operation: "sms", mode: "read", values: {} }, turo.call);
  assert.deepEqual(read.detail?.current, { sms: true });
  assert.equal(writes(turo.calls).length, 0);
  const off = await runSetting({ operation: "sms", mode: "write", values: { sms: false } }, turo.call);
  assert.equal(off.ok, true, off.result);
  assert.equal(turo.state.sms, false);
  assert.deepEqual(off.detail?.restore, { operation: "sms", values: { sms: true } });
});

test("clearing a description is a change made, though Turo stores it as null", async () => {
  let description: string | null = "A car";
  const call = async (c: TuroCall): Promise<TuroReply> => {
    if (c.method === "GET") return { status: 200, body: { name: "Jolene", description } };
    const sent = (c.form as Record<string, string>).description;
    description = sent === "" ? null : sent;
    return { status: 200, body: null };
  };
  const out = await runSetting({ operation: "listing_text", mode: "write", vehicle_id: CAR, values: { description: "" } }, call);
  assert.equal(out.ok, true, out.result);
});

test("a trip preference Turo ignores says the name it was sent as", async () => {
  const turo = fakeTuro();
  const out = await runSetting({ operation: "trip_preferences", mode: "write", vehicle_id: CAR, values: { bufferTime: "4 hours!" } }, turo.call);
  assert.equal(out.ok, true, "FOUR_HOURS is taken");
  const odd = await runSetting({ operation: "trip_preferences", mode: "write", vehicle_id: CAR, values: { bufferTime: "two weeks" } }, async (c) => {
    if (c.method === "GET") return { status: 200, body: { bufferTime: { currentName: "THREE_HOURS" } } };
    return { status: 200, body: null };
  });
  assert.equal(odd.ok, false);
  assert.match(odd.result, /bufferTime \(sent as TWO_WEEKS\)/);
});

test("turning an extra off alone uses the toggle call", async () => {
  const turo = fakeTuro();
  await runSetting({ operation: "extra_update", mode: "write", vehicle_id: CAR, values: { extraId: 7, enabled: false } }, turo.call);
  const write = writes(turo.calls)[0];
  assert.equal(write.method, "PATCH");
  assert.equal(write.path, "/api/your_car/extras/7?enabled=false");
  const turo2 = fakeTuro();
  await runSetting({ operation: "extra_update", mode: "write", vehicle_id: CAR, values: { extraId: 7, description: "Big cooler" } }, turo2.call);
  assert.equal(writes(turo2.calls)[0].method, "PUT");
});

test("a notification Turo ignores is not reported as changed", async () => {
  const read = { themes: [{ theme: "TRIPS", channels: [{ channel: "EMAIL", enabled: true }] }] };
  const ignoring = async (c: TuroCall): Promise<TuroReply> =>
    c.method === "GET" ? { status: 200, body: read } : { status: 204, body: null };
  const out = await runSetting({
    operation: "notifications", mode: "write",
    values: { preferences: [{ communication_theme: "TRIPS", delivery_channel: "EMAIL", value: false }] },
  }, ignoring);
  assert.equal(out.ok, false);
  assert.deepEqual(out.detail?.restore?.values, { preferences: [{ communication_theme: "TRIPS", delivery_channel: "EMAIL", value: true }] });
});

test("display text becomes the enum name; a name is kept", () => {
  assert.equal(enumName("3 hours"), "THREE_HOURS");
  assert.equal(enumName("1 day"), "ONE_DAY");
  assert.equal(enumName("30 minutes"), "THIRTY_MINUTES");
  assert.equal(enumName("Any"), "ANY");
  assert.equal(enumName("FOUR_HOURS"), "FOUR_HOURS");
  assert.equal(enumName(true), true);
});
