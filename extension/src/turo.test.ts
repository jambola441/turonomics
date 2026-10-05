/**
 * Tests for reading Turo's JSON.
 *
 * The one that matters is the masking: this module exists so that the shape of
 * a payload can be read without the payload being read, and a masker that
 * leaks is worse than no masker, because it leaks while being trusted.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  interestingCall,
  jsonShape,
  stringShape,
  summariseCalls,
  urlShape,
} from "./turo.js";

test("an api's own vocabulary survives", () => {
  // The whole point of reading the JSON: "CHARGED" is the status the email
  // only hints at in its subject line.
  assert.equal(stringShape("CHARGED"), "CHARGED");
  assert.equal(stringShape("REIMBURSEMENT_INVOICE"), "REIMBURSEMENT_INVOICE");
  assert.equal(stringShape("toll_fee"), "toll_fee");
  assert.equal(stringShape("invoice-hub"), "invoice-hub");
});

test("a person's name does not", () => {
  // A capitalised word is not vocabulary. This is the rule the mail probe got
  // wrong once already.
  assert.equal(stringShape("Marguerite"), "str(10)");
  assert.equal(stringShape("Jimmy"), "str(5)");
  assert.equal(stringShape("Marguerite Whitfield"), "str(20)");
  assert.equal(stringShape("4 Prospect Place, Brooklyn NY"), "str(29)");
});

test("values are named by kind, not shown", () => {
  assert.equal(stringShape("2026-07-05T18:00:00Z"), "iso-datetime");
  assert.equal(stringShape("2026-07-05"), "iso-date");
  assert.equal(stringShape("$40.71"), "money");
  assert.equal(stringShape("40.71"), "money");
  assert.equal(stringShape("59077848"), "digits(8)");
  assert.equal(stringShape("LWH4685"), "str(7)", "a plate is not vocabulary");
});

test("a url keeps its route and loses its ids", () => {
  assert.equal(
    urlShape("https://turo.com/api/reservation/59077848/invoice?invoiceId=INV77"),
    "https://turo.com/api/reservation/<digits(8)>/invoice?invoiceId"
  );
});

test("a payload's structure comes through whole", () => {
  const shape = jsonShape({
    reservationId: 59077848,
    status: "CHARGED",
    guest: { firstName: "Marguerite", id: "8e1f0b2c" },
    charges: [
      { type: "TOLL", amount: "40.71", quantity: 7 },
      { type: "ADDITIONAL_DISTANCE", amount: "45.88", quantity: 124 },
    ],
  });
  assert.equal(
    shape,
    "{reservationId: int, status: CHARGED, guest: {firstName: str(10), id: str(8)}, " +
      "charges: [2 × {type: TOLL, amount: money, quantity: int} | " +
      "{type: ADDITIONAL_DISTANCE, amount: money, quantity: int}]}"
  );
});

test("no value from a payload reaches the report", () => {
  // Belt and braces over the unit tests above: the strings below are the ones
  // that would actually hurt, and none of them may appear in the output.
  const body = {
    guest: { name: "Marguerite Whitfield", phone: "+1 718 555 0137" },
    address: "4 Prospect Place, Brooklyn NY 11238",
    plate: "LWH4685",
    note: "left the keys under the mat",
  };
  const report = summariseCalls([
    { method: "GET", url: "https://turo.com/api/reservation/59077848", status: 200, body },
  ]);
  for (const secret of [
    "Marguerite",
    "Whitfield",
    "555",
    "Prospect",
    "11238",
    "LWH4685",
    "keys",
    "59077848",
  ]) {
    assert.ok(!report.includes(secret), `leaked ${secret}`);
  }
  assert.ok(report.includes("guest: {name: str(20), phone: str(15)}"));
});

test("a long array is summarised, not transcribed", () => {
  const trips = Array.from({ length: 60 }, (_, i) => ({ id: 59000000 + i, state: "COMPLETED" }));
  assert.equal(jsonShape(trips), "[60 × {id: int, state: COMPLETED}]");
});

test("deep nesting stops rather than running away", () => {
  let nested: unknown = "CHARGED";
  for (let i = 0; i < 12; i++) nested = { inner: nested };
  const shape = jsonShape(nested);
  assert.ok(shape.includes("{…}"), shape);
});

test("the page's own noise is filtered out", () => {
  assert.equal(interestingCall("https://turo.com/api/reservations/123"), true);
  assert.equal(interestingCall("https://turo.com/graphql"), true);
  assert.equal(
    interestingCall("https://turo.com/reservation/59077848/invoice-hub"),
    true
  );
  assert.equal(interestingCall("https://api.segment.io/v1/t"), false);
  assert.equal(interestingCall("https://turo.com/static/trip-card.css"), false);
  assert.equal(interestingCall("https://turo.com/images/vehicle-4runner.jpg"), false);
});

test("one endpoint fetched per card reports once", () => {
  const calls = Array.from({ length: 12 }, (_, i) => ({
    method: "GET",
    // Fixed width on purpose: a nine-digit id and an eight-digit id are two
    // routes, which is correct and would make this test about the wrong thing.
    url: `https://turo.com/api/reservation/590778${String(i).padStart(2, "0")}`,
    status: 200,
    body: { status: "COMPLETED" },
  }));
  const report = summariseCalls(calls);
  assert.ok(report.includes("1 data endpoint(s) of 12 request(s)"), report);
});

test("a route that returned nothing does not hide the one that returned a body", () => {
  const report = summariseCalls([
    { method: "GET", url: "https://turo.com/api/invoice/1", status: 204, body: null },
    { method: "GET", url: "https://turo.com/api/invoice/2", status: 200, body: { status: "FILED" } },
  ]);
  assert.ok(report.includes("{status: FILED}"), report);
  assert.ok(!report.includes("(not json)"), report);
});

test("a page that fetched nothing says so rather than looking broken", () => {
  assert.match(summariseCalls([]), /no data calls seen/);
});
