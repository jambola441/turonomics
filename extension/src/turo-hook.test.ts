/**
 * Tests for the recorder, run against the built file.
 *
 * It is a content script, so it has no exports to import — it is an IIFE that
 * mutates the page's globals. That is exactly what is worth testing, so the
 * test sets up fake globals, loads the real built file, and checks what it did
 * to them.
 *
 * The guarantee under test is "records without interfering". A probe that
 * consumes a response body, or throws out of a wrapper, breaks the page it is
 * watching — and the page belongs to the host's booking flow.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

interface Recorded {
  method: string;
  url: string;
  status: number;
  body: unknown;
}

type FakeWindow = {
  fetch: typeof fetch;
  __turonomicsCalls?: Recorded[];
  __turonomicsHooked?: true;
};

/** Load the built hook over a fresh set of fakes, and hand back the window. */
async function install(
  respond: (url: string) => Response
): Promise<{ window: FakeWindow; calls: () => Recorded[] }> {
  const fakeWindow = {
    fetch: ((input: RequestInfo | URL) =>
      Promise.resolve(respond(String(input)))) as typeof fetch,
  } as FakeWindow;
  const globals = globalThis as unknown as Record<string, unknown>;
  globals.window = fakeWindow;
  globals.location = { href: "https://turo.com/trips" };
  // The hook patches XMLHttpRequest.prototype.open/send, so the fake needs
  // both to exist on a prototype. Only fetch is exercised below; this is here
  // so that loading the file does not throw.
  class FakeXHR {
    open(): void {}
    send(): void {}
    addEventListener(): void {}
  }
  globals.XMLHttpRequest = FakeXHR;
  // A cache-busting query, because a module is evaluated once per URL and each
  // test needs the IIFE to run again over its own fakes.
  await import(`../dist/turo-hook.js?${Math.random()}`);
  return { window: fakeWindow, calls: () => fakeWindow.__turonomicsCalls ?? [] };
}

test("the caller still gets to read the body", async () => {
  // The hook reads from a clone. Reading the original would leave the page's
  // own `await response.json()` throwing "body stream already read", which is
  // the failure that takes a checkout page down.
  const { window } = await install(
    () => new Response('{"status":"CHARGED"}', { status: 200 })
  );
  const response = await window.fetch("https://turo.com/api/reservation/1");
  assert.deepEqual(await response.json(), { status: "CHARGED" });
});

test("and the call is recorded", async () => {
  const { window, calls } = await install(
    () => new Response('{"status":"CHARGED"}', { status: 200 })
  );
  await window.fetch("https://turo.com/api/reservation/1");
  // The body is read from the clone asynchronously, so let that settle.
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls().length, 1);
  assert.deepEqual(calls()[0], {
    method: "GET",
    url: "https://turo.com/api/reservation/1",
    status: 200,
    body: { status: "CHARGED" },
  });
});

test("a body that is not json is recorded as having none", async () => {
  const { window, calls } = await install(
    () => new Response("<!doctype html><title>Turo</title>", { status: 200 })
  );
  await window.fetch("https://turo.com/trips");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls()[0].body, null, "and not a parse error");
});

test("a failing response is still returned, not swallowed", async () => {
  const { window, calls } = await install(() => new Response("", { status: 503 }));
  const response = await window.fetch("https://turo.com/api/reservation/1");
  assert.equal(response.status, 503);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls()[0].status, 503);
});

test("it does not grow without bound", async () => {
  const { window, calls } = await install(() => new Response("{}", { status: 200 }));
  for (let i = 0; i < 260; i++) await window.fetch(`https://turo.com/api/x/${i}`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(calls().length, 250, "capped");
});

test("loading twice does not wrap twice", async () => {
  // Chrome re-injects on client-side navigation often enough that this matters:
  // a doubly-wrapped fetch records every call twice and clones twice.
  const { window } = await install(() => new Response("{}", { status: 200 }));
  // Held by identity, not read back through the window: a second run without
  // the guard installs a *fresh* array as well as a second wrapper, so a test
  // that re-reads `window.__turonomicsCalls` looks at the new one, finds its
  // single record, and passes while every call is in fact recorded twice.
  const recorder = window.__turonomicsCalls;
  const wrapped = window.fetch;
  const globals = globalThis as unknown as Record<string, unknown>;
  globals.window = window;
  await import(`../dist/turo-hook.js?second-${Math.random()}`);
  assert.equal(window.__turonomicsCalls, recorder, "kept the same recorder");
  assert.equal(window.fetch, wrapped, "did not wrap fetch again");
  await window.fetch("https://turo.com/api/reservation/1");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(recorder?.length, 1);
});
