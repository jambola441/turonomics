/**
 * Tests for reading Turo's JSON.
 *
 * The one that matters is the masking: this module exists so that the shape of
 * a payload can be read without the payload being read, and a masker that
 * leaks is worse than no masker, because it leaks while being trusted.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { TuroPullResult } from "./types.js";
import {
  CHARGE_KEYS,
  describeFiling,
  describePull,
  describeEmbedded,
  findByKey,
  interestingCall,
  jsonShape,
  stringShape,
  summariseCalls,
  TRIP_KEYS,
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
  assert.equal(interestingCall("/api/properties"), true, "relative, same page");
  assert.equal(interestingCall("https://api.segment.io/v1/t"), false);
  assert.equal(interestingCall("https://turo.com/static/trip-card.css"), false);
  assert.equal(interestingCall("https://turo.com/images/vehicle-4runner.jpg"), false);
});

test("a beacon does not become interesting by quoting the page url", () => {
  // Six of the eight endpoints in the first real report were this: New Relic
  // and Google put the document location in a query parameter, so a beacon
  // sent from an invoice page carries the words "reservation" and
  // "reimbursement" and outranks the two calls that mattered.
  const ref = encodeURIComponent(
    "https://turo.com/us/en/reservation/58358939/reimbursement/invoice?invoiceId=113672232"
  );
  assert.equal(interestingCall(`https://bam.nr-data.net/jserrors/1/abc?ref=${ref}`), false);
  assert.equal(interestingCall(`https://www.google.com/ccm/collect?dl=${ref}`), false);
  assert.equal(
    interestingCall(`https://cmp.osano.com/16A0DAfdsdf/abc-def/en.json?u=${ref}`),
    false
  );
});

test("a relative url keeps its route", () => {
  // Reported as the bare word "url" at first, which hid the only two
  // first-party endpoints on the page.
  assert.equal(urlShape("/api/properties"), "https://turo.com/api/properties");
  assert.equal(
    urlShape("/api/reservation/59077848/invoice", "https://turo.com/us/en/trips"),
    "https://turo.com/api/reservation/<digits(8)>/invoice"
  );
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


// ---------------------------------------------------------------------------
// Reading the document, because that is where the data turned out to be
// ---------------------------------------------------------------------------

/** A hydration blob shaped like the real thing: the page, with the data in it. */
const NEXT_DATA = JSON.stringify({
  props: {
    pageProps: {
      locale: "en-US",
      reservation: {
        id: 58358939,
        scheduledStart: "2026-07-03T14:00:00Z",
        scheduledEnd: "2026-07-05T18:00:00Z",
        actualEnd: "2026-07-05T20:41:00Z",
        vehicle: { plate: "LWH4685", odometerStart: 48219 },
        guest: { firstName: "Marguerite" },
      },
      reimbursementInvoice: {
        invoiceId: 113672232,
        status: "CHARGED",
        charges: [
          { type: "TOLL", label: "7 tolls", amount: "40.71", quantity: 7 },
          { type: "ADDITIONAL_DISTANCE", label: "22 mi", amount: "7.70", quantity: 22 },
        ],
        totalCharge: "48.41",
      },
    },
  },
  buildId: "a8f3c1",
});

test("the paths into a hydration blob are reported, not its whole shape", () => {
  const report = describeEmbedded([{ label: "script#__NEXT_DATA__", text: NEXT_DATA }]);
  // A matching key reports its subtree and stops descending, so the invoice
  // arrives whole rather than as one path per field. That is the useful
  // granularity: it says both where the invoice is and what is in it.
  assert.match(report, /props\.pageProps\.reimbursementInvoice: \{/);
  assert.match(report, /charges: \[2 × \{type: TOLL/);
  assert.match(report, /totalCharge: money/);
  assert.match(report, /props\.pageProps\.reservation: \{/);
  assert.match(report, /actualEnd: iso-datetime/, "the late return is in there");
});

test("and still no value from it", () => {
  const report = describeEmbedded([{ label: "script#__NEXT_DATA__", text: NEXT_DATA }]);
  for (const secret of ["Marguerite", "LWH4685", "48219", "40.71", "58358939", "113672232"]) {
    assert.ok(!report.includes(secret), `leaked ${secret}`);
  }
  // The vocabulary that makes the blob readable does survive.
  assert.ok(report.includes("TOLL"), report);
  assert.ok(report.includes("CHARGED"), report);
});

test("an array of sixty trips is one path, not sixty", () => {
  const blob = JSON.stringify({
    trips: Array.from({ length: 60 }, (_, i) => ({
      reservationId: 59000000 + i,
      tollCharges: [{ amount: "4.50" }],
    })),
  });
  const found = findByKey(JSON.parse(blob), CHARGE_KEYS);
  assert.equal(found.length, 1);
  assert.equal(found[0].path, "trips[].tollCharges");
});

test("a blob that is not json is named rather than passed over", () => {
  const report = describeEmbedded([
    { label: "script (inline)", text: "window.__turoFlags = {a:1}" },
  ]);
  assert.match(report, /script \(inline\) — \d+ chars, not json/);
});

test("a document with nothing in it says so", () => {
  assert.match(describeEmbedded([]), /no embedded json found/);
  assert.match(
    describeEmbedded([{ label: "script#config", text: '{"theme":"dark"}' }]),
    /nothing charge- or trip-shaped/
  );
});

test("the keys we are hunting match what Turo actually calls things", () => {
  // From the mail probe: "22 mi additional distance", "7 tolls", "Tickets",
  // "Refueling", "Total charge". And the trip times the grace period guesses.
  for (const key of ["tollCharges", "totalCharge", "invoiceId", "reimbursementInvoice", "additionalDistance", "refuelingFee", "ticketCharges"]) {
    assert.ok(CHARGE_KEYS.test(key), key);
  }
  for (const key of ["reservationId", "scheduledEnd", "actualEnd", "dropoffTime", "vehicle", "odometerEnd"]) {
    assert.ok(TRIP_KEYS.test(key), key);
  }
});

test("a truncated object names what it dropped", () => {
  // "+38 more" on Turo's reservation detail hid whichever key holds the actual
  // return time — the one field the late-return grace exists to guess at. A
  // key is structure; only the value beside it is shaped away.
  const wide: Record<string, unknown> = {};
  for (let i = 0; i < 85; i++) wide[`field${i}`] = "CHARGED";
  const shape = jsonShape(wide);
  assert.match(shape, /\+5 more: field80 field81 field82 field83 field84\}$/);
});

test("a status longer than a word is still vocabulary", () => {
  // Turo's reimbursementStatus came back as str(34) against a cap tuned to
  // "CHARGED", masking the single field the probe existed to read.
  assert.equal(
    stringShape("REIMBURSEMENT_CHARGED_TO_GUEST_OK"),
    "REIMBURSEMENT_CHARGED_TO_GUEST_OK"
  );
  // Still not a licence plate, and still not a sentence.
  assert.equal(stringShape("LWH4685"), "str(7)");
  assert.equal(stringShape("Charged to the guest on Tuesday"), "str(31)");
});

test("first-party analytics is still analytics", () => {
  // Both showed up in a real report: a Cloudflare RUM beacon and Turo's own
  // collector, on turo.com, answering 204 and HTML respectively.
  assert.equal(interestingCall("https://turo.com/cdn-cgi/rum"), false);
  assert.equal(interestingCall("https://turo.com/api/tracking"), false);
  // Not an over-broad rule: the real endpoints under /api/ stay.
  assert.equal(interestingCall("https://turo.com/api/reservation/detail?x=1"), true);
  assert.equal(interestingCall("https://turo.com/api/tracking/preferences"), true);
});

// ---------------------------------------------------------------------------
// Saying what a pull did
// ---------------------------------------------------------------------------

const EMPTY: TuroPullResult = {
  seen: 0, stored: 0, unparsed: 0, unknown: [], retimed: [], wrong_plate: [],
  tolls_rematched: 0, grace_periods: [], asked: 0, failed: 0,
};

test("a quiet pull says only what it read", () => {
  assert.equal(
    describePull({ ...EMPTY, asked: 37, seen: 37, stored: 37 }),
    "37 of 37 rental(s) read"
  );
});

test("a moved booking and its consequence are both named", () => {
  // These two are the point of the whole pull, so they may not be buried.
  const out = describePull({
    ...EMPTY,
    asked: 37,
    stored: 37,
    retimed: ["58358939: 2026-07-03 14:00–18:00 -> 2026-07-03 14:00–21:00"],
    tolls_rematched: 2,
  });
  assert.match(out, /1 booking\(s\) moved/);
  assert.match(out, /2 crossing\(s\) re-attributed/);
});

test("the two kinds of failure are not conflated", () => {
  // Turo refusing to hand a reservation over and the API being unable to read
  // one have different causes and different fixes.
  const out = describePull({ ...EMPTY, asked: 10, stored: 7, failed: 2, unparsed: 1 });
  assert.match(out, /2 Turo would not return/);
  assert.match(out, /1 unreadable/);
});

test("a rental on the wrong car is surfaced, because it bills the wrong guest", () => {
  const out = describePull({ ...EMPTY, asked: 1, stored: 1, wrong_plate: ["58358939: Jerry"] });
  assert.match(out, /1 on the wrong car/);
  assert.ok(!out.includes("Jerry"), "the count belongs in the line, the detail does not");
});

test("a write is never filtered out, whatever its route", () => {
  // The filing POST and the image upload are the two calls the probe exists to
  // catch, and their paths cannot be guessed in advance — so a non-GET to
  // turo.com is reported even where the same path would be skipped as noise.
  assert.equal(interestingCall("https://turo.com/cdn-cgi/rum", "https://turo.com/", "POST"), true);
  assert.equal(interestingCall("https://turo.com/api/tracking", "https://turo.com/", "GET"), false);
  // Still first party only: a beacon to somebody else stays out.
  assert.equal(interestingCall("https://api.segment.io/v1/t", "https://turo.com/", "POST"), false);
});

test("what was sent is reported before what came back", () => {
  const report = summariseCalls([
    {
      method: "POST",
      url: "https://turo.com/api/reimbursement",
      status: 200,
      request: { reservationId: 58626257, lineItems: [{ type: "TOLL_REIMBURSEMENT" }] },
      body: { invoiceId: 113672232, reimbursementStatus: "FILED" },
    },
  ]);
  assert.match(report, /sent: \{reservationId: int, lineItems: \[1 × \{type: TOLL_REIMBURSEMENT\}\]\}/);
  assert.ok(report.indexOf("sent:") < report.indexOf("invoiceId"), report);
});

test("an upload to a storage host is reported, a beacon is not", () => {
  // The filing POST came back with mediaEvidenceUuids already populated and no
  // upload call in the report: an evidence image goes to a presigned URL on a
  // storage host, and first-party-only filtering threw it away.
  const put = (url: string) => interestingCall(url, "https://turo.com/", "PUT");
  const post = (url: string) => interestingCall(url, "https://turo.com/", "POST");
  assert.equal(put("https://turo-media.s3.amazonaws.com/abc123?X-Amz-Signature=x"), true);
  assert.equal(post("https://uploads.turo.com/media"), true);
  assert.equal(post("https://some-cdn.example.com/upload"), true, "cannot be named in advance");
  // Measurement stays out, however it is written to.
  assert.equal(post("https://bam.nr-data.net/jserrors/1/abc"), false);
  assert.equal(post("https://www.google.com/ccm/collect?en=x"), false);
  assert.equal(post("https://cmp.osano.com/x/y/z"), false);
  assert.equal(post("https://api.segment.io/v1/t"), false);
  // Reads are still first party only, so a page's fonts and CDNs stay quiet.
  assert.equal(interestingCall("https://fonts.googleapis.com/css", "https://turo.com/", "GET"), false);
});

test("the probe's own descriptors survive the masker", () => {
  // The upload reported `file: str(28)`, and 28 is the length of
  // "<file image/png 92579 bytes>" — the masker ate the one field written to
  // describe the upload, leaving a number that looks like a short string.
  assert.equal(stringShape("<file image/png 92579 bytes>"), "<file image/png 92579 bytes>");
  assert.equal(stringShape("<text 130>"), "<text 130>");
  assert.equal(stringShape("<blob application/pdf 4096 bytes>"), "<blob application/pdf 4096 bytes>");
  // Not a licence to pass through anything in angle brackets: a value is still
  // a value, and markup is not a descriptor.
  assert.equal(stringShape("<Marguerite Whitfield>"), "str(22)");
  assert.equal(stringShape("<p>hello</p>"), "str(12)");
});

test("an uploaded file is legible in the report", () => {
  const report = summariseCalls([
    {
      method: "POST",
      url: "https://turo.com/api/reservation/image",
      status: 200,
      request: { "<multipart>": { file: "<file image/png 92579 bytes>", reservationId: 58626257 } },
      body: { uuid: "7a1f0b2c-1111-2222-3333-444455556666", step: "TRIP_PHOTO" },
    },
  ]);
  assert.match(report, /file: <file image\/png 92579 bytes>/);
  assert.match(report, /reservationId: int/);
});

// ---------------------------------------------------------------------------
// Saying what a filing did
// ---------------------------------------------------------------------------

test("a filing says who was asked and for how much", () => {
  const out = describeFiling({
    filed: true, guest: "Alice", amountCents: 1151, reservation: "58626257", daysLeft: 4,
  });
  assert.match(out, /Filed for \$11\.51 to Alice on reservation 58626257/);
  assert.match(out, /4 day\(s\) left/, "the deadline, while it is still close");
});

test("a comfortable deadline is not mentioned", () => {
  const out = describeFiling({
    filed: true, guest: "Alice", amountCents: 1151, reservation: "58626257", daysLeft: 60,
  });
  assert.ok(!out.includes("day(s) left"), out);
});

test("a failure names the amount as well as the reason", () => {
  // "It did not work" about an unknown sum is not something anyone can act on.
  const out = describeFiling({
    filed: false, reason: "upload: Turo said 413", amountCents: 1151,
  });
  assert.match(out, /Not filed for \$11\.51 — upload: Turo said 413/);
});

test("nothing to file is not reported as a failure to file", () => {
  assert.equal(describeFiling({ filed: false, reason: "nothing to file" }), "Not filed — nothing to file");
});

test("an api version survives in a route", () => {
  // Masking it cost a filing: `/api/<str(2)>/reimbursement/<id>/request` was
  // read as a locale, the POST went to a path that does not exist, and the
  // evidence image had already been uploaded by then.
  assert.equal(
    urlShape("https://turo.com/api/v2/reimbursement/58426608/request"),
    "https://turo.com/api/v2/reimbursement/<digits(8)>/request"
  );
  // Narrow on purpose. An id is not a version, however short.
  assert.equal(
    urlShape("https://turo.com/api/reservation/a1b2c3/detail"),
    "https://turo.com/api/reservation/<str(6)>/detail"
  );
  assert.equal(
    urlShape("https://turo.com/api/v12345678/x"),
    "https://turo.com/api/<str(9)>/x"
  );
});

test("a path id is placed against the page's own url", () => {
  // Which id a route takes is the difference between a filing and a 404, and
  // a masked report cannot tell an 8-digit reservation id from an 8-digit
  // reimbursement id. Whether it is the number the operator is looking at is
  // the distinction that matters, and is not a value.
  const page = "https://turo.com/us/en/reservation/58426608/reimbursement/request/tolls";
  assert.match(
    urlShape("https://turo.com/api/v2/reimbursement/58426608/request", "https://turo.com/", page),
    /\/api\/v2\/reimbursement\/<digits\(8\)=the one in the page url>\/request$/
  );
  // A different id is reported as a plain shape, which is the finding.
  assert.match(
    urlShape("https://turo.com/api/v2/reimbursement/77112233/request", "https://turo.com/", page),
    /\/reimbursement\/<digits\(8\)>\/request$/
  );
});

test("placing an id never prints it", () => {
  const page = "https://turo.com/us/en/reservation/58426608/x";
  const shaped = urlShape("https://turo.com/api/v2/reimbursement/58426608/request", "https://turo.com/", page);
  assert.ok(!shaped.includes("58426608"), shaped);
});

test("a substring of a longer id is not a match", () => {
  // 5842 appearing inside 58426608 must not read as "the one in the page url".
  const page = "https://turo.com/us/en/reservation/58426608/x";
  assert.match(
    urlShape("https://turo.com/api/v2/reimbursement/5842/request", "https://turo.com/", page),
    /<digits\(4\)>/
  );
});
