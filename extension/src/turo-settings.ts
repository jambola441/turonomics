/**
 * Turo's listing and account settings, read and changed by name.
 *
 * The API queues a `turo` command naming an operation; this is the whole list
 * of operations, and how each is called. Nothing else in the extension makes
 * a Turo write for the API, so a command can only ask for one of these: the
 * API, or anyone who reaches it, cannot point the browser at an arbitrary
 * Turo address.
 *
 * Every change is read, written, read back. The answer carries the setting
 * before and after, and the operation and values that put it back, which the
 * API keeps so the change can be undone.
 *
 * Shapes are the ones exercised against the live account on 2026-10-30, each
 * restored and checked field by field (turo-host-api-findings.md). Written
 * against an injected caller so they can be tested without a browser.
 */

/** One request to Turo, made from inside a Turo page with its cookies. */
export interface TuroCall {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  json?: unknown;
  /** x-www-form-urlencoded; an array value is sent as repeated keys. */
  form?: Record<string, string | string[]>;
}

export interface TuroReply {
  status: number;
  body: unknown;
}

export type Caller = (call: TuroCall) => Promise<TuroReply>;

export interface SettingPayload {
  operation?: unknown;
  mode?: unknown;
  vehicle_id?: unknown;
  values?: unknown;
}

export interface SettingOutcome {
  ok: boolean;
  result: string;
  detail?: {
    current?: unknown;
    options?: unknown;
    before?: unknown;
    after?: unknown;
    restore?: { operation: string; values: Record<string, unknown> };
  };
}

type Values = Record<string, unknown>;

class Refused extends Error {}

/** What a write needs: where, how to call Turo, what was asked. */
interface Ctx {
  vehicle: string;
  values: Values;
  call: Caller;
}

async function get(call: Caller, path: string): Promise<Record<string, unknown>> {
  const reply = await call({ method: "GET", path });
  if (reply.status < 200 || reply.status >= 300) throw new Error(`Turo answered ${reply.status} reading ${path}`);
  return (reply.body ?? {}) as Record<string, unknown>;
}

async function send(call: Caller, request: TuroCall): Promise<unknown> {
  const reply = await call(request);
  if (reply.status < 200 || reply.status >= 300) {
    const said = reply.body && typeof reply.body === "object" ? JSON.stringify(reply.body).slice(0, 200) : "";
    throw new Error(`Turo answered ${reply.status} to ${request.method} ${request.path}${said ? `: ${said}` : ""}`);
  }
  return reply.body;
}

/** Only these keys, and each one known: a typo is refused, not sent. */
function only(values: Values, keys: readonly string[]): Values {
  const unknown = Object.keys(values).filter((k) => !keys.includes(k));
  if (unknown.length) throw new Refused(`unknown value ${unknown.join(", ")}; this setting takes ${keys.join(", ")}`);
  return values;
}

function pick(from: Record<string, unknown>, keys: readonly string[]): Values {
  const out: Values = {};
  for (const key of keys) if (key in from) out[key] = from[key];
  return out;
}

function str(value: unknown, name: string): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  throw new Refused(`${name} is needed`);
}

function id(values: Values, name: string): string {
  const value = values[name];
  if (typeof value === "number" || (typeof value === "string" && /^\w+$/.test(value))) return String(value);
  throw new Refused(`${name} is needed`);
}

function list(body: Record<string, unknown>, key: string): Record<string, unknown>[] {
  const value = body[key];
  return Array.isArray(value) ? (value as Record<string, unknown>[]) : [];
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------------------
// Settings set as a whole: read, write the merge, read back
// ---------------------------------------------------------------------------

interface WholeSetting {
  scope: "car" | "account";
  /** The keys a change may carry. */
  keys: readonly string[];
  read(ctx: Ctx): Promise<{ current: Record<string, unknown>; options?: unknown }>;
  /** Writes `values`, already narrowed to `keys`, over `current`. */
  write(ctx: Ctx, current: Record<string, unknown>, values: Values): Promise<void>;
  /** The values that would write `current` back, for the keys `values` changed. */
  restore(current: Record<string, unknown>, values: Values): Values;
  /** The asked values as the read shows them, to check the write took; omitted where they cannot be compared. */
  readBack?(after: Record<string, unknown>, values: Values): Values;
}

/** Same value, ignoring the order of an object's keys. */
export function same(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    const bs = b as unknown[];
    return a.length === bs.length && a.every((x, i) => same(x, bs[i]));
  }
  const ka = Object.keys(a as object);
  const kb = Object.keys(b as object);
  return ka.length === kb.length && ka.every((k) => same((a as Values)[k], (b as Values)[k]));
}

/** The keys a write was asked to change that the read back does not show changed. */
export function notTaken(asked: Values, readBack: Values): string[] {
  return Object.keys(asked).filter((key) => !same(asked[key], readBack[key]));
}

const GUEST_KEYS = ["welcomeMessage", "pickupReturnInstructions", "carGuide"] as const;
const DISTANCE_KEYS = ["dailyDistanceLimit", "monthlyDistanceEnabled", "monthlyDistanceLimit"] as const;
const TRIP_PREFERENCE_KEYS = [
  "bufferTime", "customBufferTime", "poiBufferTime",
  "minimumHomeLeadTime", "minimumCustomLeadTime", "minimumPoiLeadTime",
  "minimumRequestDuration", "minimumRequestDurationForCustom", "minimumRequestDurationForPoi",
  "maximumRequestDuration", "maximumRequestDurationForCustom", "maximumRequestDurationForPoi",
  // The three *LocationEnabled keys are left out: Turo takes them but never
  // reads them back, so a change to one could not be checked or undone.
  "longerWeekendTripPreferred",
] as const;

/**
 * A trip preference as written: the enum name a field is set to, or the flag.
 *
 * The enum name is the only thing that works. The options carry display text
 * ("3 hours") and Turo answers 200 to a write of it while changing nothing,
 * so what is written is checked by reading it back.
 */
export function preferenceValue(field: unknown): unknown {
  if (field && typeof field === "object" && "currentName" in field) {
    return (field as { currentName: unknown }).currentName;
  }
  return field;
}

const ONES = ["ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN",
  "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN", "SEVENTEEN", "EIGHTEEN", "NINETEEN"];
const TENS = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY"];

function numberWord(n: number): string {
  if (n < 20) return ONES[n];
  return TENS[Math.floor(n / 10)] + (n % 10 ? `_${ONES[n % 10]}` : "");
}

/**
 * "3 hours" -> "THREE_HOURS", "Any" -> "ANY": the enum names seen on the
 * account follow this. A name already in that form is kept. A guess this
 * gets wrong is caught by the read-back, not written silently.
 */
export function enumName(value: unknown): unknown {
  if (typeof value !== "string" || /^[A-Z][A-Z0-9_]*$/.test(value)) return value;
  return value
    .trim()
    .replace(/\d+/g, (digits) => (Number(digits) < 100 ? numberWord(Number(digits)) : digits))
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_|_$/g, "");
}

/** The listing's own fields, wherever Turo nests them in /api/vehicle/listing. */
export function listingFields(body: Record<string, unknown>): {
  description: string | null;
  vehicleName: string | null;
  badgeIds: number[];
  imageIds: string[];
} {
  const vehicle = (body.vehicle && typeof body.vehicle === "object" ? body.vehicle : body) as Record<string, unknown>;
  const text = (v: unknown) => (typeof v === "string" ? v : null);
  return {
    description: text(body.description ?? vehicle.description),
    // "name" on the read; the description write calls the same field vehicleName.
    vehicleName: text(body.name ?? body.vehicleName ?? vehicle.vehicleName),
    badgeIds: list(body, "badges").map((b) => Number(b.id)).filter((n) => Number.isFinite(n)),
    imageIds: list(body, "images").map((i) => String(i.id)),
  };
}

/** Each option field as its enum name; the weekend flag as the bool it is. */
export function tripPreferences(values: Values): Values {
  const out: Values = {};
  for (const [key, value] of Object.entries(values)) out[key] = key === "longerWeekendTripPreferred" ? value : enumName(value);
  return out;
}

const WHOLE: Record<string, WholeSetting> = {
  guest_instructions: {
    scope: "car",
    keys: GUEST_KEYS,
    async read({ call, vehicle }) {
      return { current: pick(await get(call, `/api/vehicle/${enc(vehicle)}/guest-instructions`), GUEST_KEYS) };
    },
    // Replace-all: a key left out is nulled, so the current ones go with it.
    async write({ call, vehicle }, current, values) {
      await send(call, { method: "PUT", path: `/api/vehicle/${enc(vehicle)}/guest-instructions`, json: { ...current, ...values } });
    },
    restore: (current, values) => pick(current, Object.keys(values)),
    readBack: (after, values) => pick(after, Object.keys(values)),
  },
  listing_text: {
    scope: "car",
    keys: ["description", "vehicleName"],
    async read({ call, vehicle }) {
      const fields = listingFields(await get(call, `/api/vehicle/listing?vehicleId=${enc(vehicle)}`));
      return { current: { description: fields.description, vehicleName: fields.vehicleName } };
    },
    // Colour is left out, which leaves it as it is.
    async write({ call, vehicle }, current, values) {
      const merged = { ...current, ...values };
      await send(call, {
        method: "POST",
        path: "/api/vehicle/description",
        form: { description: String(merged.description ?? ""), vehicleName: String(merged.vehicleName ?? ""), vehicleId: vehicle },
      });
    },
    restore: (current, values) => pick(current, Object.keys(values)),
    readBack: (after, values) => pick(after, Object.keys(values)),
  },
  badges: {
    scope: "car",
    keys: ["badgeIds"],
    async read({ call, vehicle }) {
      const fields = listingFields(await get(call, `/api/vehicle/listing?vehicleId=${enc(vehicle)}`));
      const listing = await get(call, `/api/listings/${enc(vehicle)}`);
      return { current: { badgeIds: fields.badgeIds }, options: listing.badgeOptions };
    },
    async write({ call, vehicle }, _current, values) {
      const ids = values.badgeIds;
      if (!Array.isArray(ids) || !ids.every((n) => Number.isInteger(n))) throw new Refused("badgeIds is a list of numbers");
      await send(call, { method: "PUT", path: `/api/v2/vehicle/${enc(vehicle)}/badges`, json: { badgeIds: ids } });
    },
    restore: (current) => ({ badgeIds: current.badgeIds }),
    // A set: Turo may list them in its own order.
    readBack: (after, values) => {
      const asked = [...((values.badgeIds as number[]) ?? [])].sort((a, b) => a - b);
      const have = [...((after.badgeIds as number[]) ?? [])].sort((a, b) => a - b);
      return { badgeIds: same(asked, have) ? values.badgeIds : have };
    },
  },
  photo_order: {
    scope: "car",
    keys: ["imageIds"],
    async read({ call, vehicle }) {
      return { current: { imageIds: listingFields(await get(call, `/api/vehicle/listing?vehicleId=${enc(vehicle)}`)).imageIds } };
    },
    // Every photo, each once: an order that drops one is refused here rather
    // than finding out what Turo does with it.
    async write({ call, vehicle }, current, values) {
      const wanted = Array.isArray(values.imageIds) ? values.imageIds.map(String) : [];
      const have = (current.imageIds as string[]) ?? [];
      if (wanted.length !== have.length || [...wanted].sort().join() !== [...have].sort().join()) {
        throw new Refused("imageIds must list every photo exactly once; read photo_order for them");
      }
      await send(call, { method: "POST", path: "/api/vehicle/set_image_ordering", form: { images: wanted, vehicleId: vehicle } });
    },
    restore: (current) => ({ imageIds: current.imageIds }),
    // Ids are compared as text: asked as numbers, read back as strings.
    readBack: (after, values) => {
      const asked = ((values.imageIds as unknown[]) ?? []).map(String);
      return { imageIds: same(asked, after.imageIds) ? values.imageIds : after.imageIds };
    },
  },
  trip_preferences: {
    scope: "car",
    keys: TRIP_PREFERENCE_KEYS,
    async read({ call, vehicle }) {
      return { current: await get(call, `/api/vehicle/${enc(vehicle)}/trip-preferences`) };
    },
    // Partial: only what is sent changes.
    async write({ call, vehicle }, _current, values) {
      await send(call, { method: "PATCH", path: `/api/vehicle/${enc(vehicle)}/trip-preferences`, json: tripPreferences(values) });
    },
    restore(current, values) {
      const out: Values = {};
      for (const key of Object.keys(values)) out[key] = preferenceValue(current[key]);
      return out;
    },
    // Compared as written: the display text asked for is checked as its name.
    readBack(after, values) {
      const out: Values = {};
      const written = tripPreferences(values);
      for (const key of Object.keys(values)) {
        out[key] = same(written[key], preferenceValue(after[key])) ? values[key] : preferenceValue(after[key]);
      }
      return out;
    },
  },
  distance_limit: {
    scope: "car",
    keys: DISTANCE_KEYS,
    async read({ call, vehicle }) {
      const current = await get(call, `/api/vehicle/${enc(vehicle)}/distance-limit`);
      const options = await get(call, `/api/vehicle/${enc(vehicle)}/distance-limit/options`);
      return { current, options };
    },
    async write({ call, vehicle }, current, values) {
      await send(call, {
        method: "PUT",
        path: `/api/vehicle/${enc(vehicle)}/distance-limit`,
        json: { ...pick(current, DISTANCE_KEYS), ...values, vehicleId: Number(vehicle) || vehicle },
      });
    },
    restore: (current, values) => pick(current, Object.keys(values)),
    readBack: (after, values) => pick(after, Object.keys(values)),
  },
  business_hours: {
    scope: "account",
    keys: ["alwaysAvailable", "week"],
    async read({ call }) {
      return { current: await get(call, `/api/drivers/${enc(await driverId(call))}/business_hours`) };
    },
    // Replace-all, with the read body as the base, as Turo's own page sends it.
    async write({ call }, current, values) {
      const driver = await driverId(call);
      await send(call, { method: "PUT", path: `/api/drivers/${enc(driver)}/business_hours`, json: { ...current, ...values } });
    },
    restore: (current, values) => pick(current, Object.keys(values)),
  },
  notifications: {
    scope: "account",
    keys: ["preferences"],
    async read({ call }) {
      return { current: await get(call, "/api/me/notification-preferences?device_type=WEB") };
    },
    async write({ call }, _current, values) {
      const prefs = values.preferences;
      const valid =
        Array.isArray(prefs) &&
        prefs.every((p) => p && typeof p.communication_theme === "string" && typeof p.delivery_channel === "string" && typeof p.value === "boolean");
      if (!valid) throw new Refused("preferences is a list of {communication_theme, delivery_channel, value}");
      await send(call, { method: "POST", path: "/api/me/notification-preferences", json: prefs });
    },
    restore(current, values) {
      const themes = list(current, "themes");
      const was = (theme: string, channel: string): boolean | undefined => {
        const t = themes.find((x) => x.theme === theme);
        const c = t ? list(t, "channels").find((x) => x.channel === channel) : undefined;
        return c && typeof c.enabled === "boolean" ? c.enabled : undefined;
      };
      const back = (values.preferences as { communication_theme: string; delivery_channel: string }[])
        .map((p) => ({ ...p, value: was(p.communication_theme, p.delivery_channel) }))
        .filter((p) => p.value !== undefined);
      return { preferences: back };
    },
  },
  sms: {
    scope: "account",
    keys: ["sms"],
    async read({ call }) {
      const reply = await call({ method: "GET", path: "/api/me/contact_preferences" });
      if (reply.status !== 200 || !Array.isArray(reply.body)) throw new Error(`Turo answered ${reply.status} reading contact preferences`);
      const sms = (reply.body as Record<string, unknown>[])
        .flatMap((group) => list(group, "preferences"))
        .find((p) => p.type === "sms");
      return { current: { sms: sms ? sms.enabled : null } };
    },
    async write({ call }, _current, values) {
      if (typeof values.sms !== "boolean") throw new Refused("sms is true or false");
      await send(call, { method: "POST", path: "/api/me/contact_preferences", form: { sms: String(values.sms) } });
    },
    restore: (current) => ({ sms: current.sms }),
    readBack: (after) => ({ sms: after.sms }),
  },
};

/** The host's driver id: /api/me's driver.id. */
async function driverId(call: Caller): Promise<string> {
  const me = await get(call, "/api/me");
  const found = (me.driver as Record<string, unknown> | undefined)?.id;
  if (typeof found !== "number" && typeof found !== "string") throw new Error("could not find this account's driver id");
  return String(found);
}

// ---------------------------------------------------------------------------
// Lists of items: extras, questions, feature lines
// ---------------------------------------------------------------------------

interface ItemList {
  /** The setting a read of these is under. */
  setting: string;
  idKey: string;
  read(ctx: Ctx): Promise<{ items: Record<string, unknown>[]; options?: unknown }>;
  /** The item's fields as an add would take them. */
  fields(item: Record<string, unknown>): Values;
  add(ctx: Ctx, values: Values): Promise<void>;
  update(ctx: Ctx, itemId: string, values: Values): Promise<void>;
  remove(ctx: Ctx, itemId: string): Promise<void>;
}

const EXTRA_KEYS = ["extraType", "description", "price", "quantity", "extraPricingType", "enabled"] as const;

/** An extra as Turo lists it, as the fields its add and edit take. */
export function extraFields(item: Record<string, unknown>): Values {
  const type = item.extraType as Record<string, unknown> | string | undefined;
  const price = item.priceWithCurrency as { amount?: unknown; currencyCode?: unknown } | undefined;
  return {
    extraType: typeof type === "object" && type ? type.value ?? type.name : type,
    description: item.description,
    price: price?.amount,
    quantity: item.quantity,
    extraPricingType: item.extraPricingType,
    enabled: item.enabled,
  };
}

function extraQuery(fields: Values): string {
  const params = new URLSearchParams();
  for (const key of EXTRA_KEYS) params.set(key, str(fields[key], key));
  params.set("currencyCode", "USD");
  return params.toString();
}

const LISTS: Record<string, ItemList> = {
  extra: {
    setting: "extras",
    idKey: "extraId",
    async read({ call, vehicle }) {
      const items = list(await get(call, `/api/your_car/extras?vehicleId=${enc(vehicle)}`), "extras");
      const types = await get(call, `/api/extras?maxExtras=5&search=&vehicleId=${enc(vehicle)}`);
      return { items, options: types.extraTypes };
    },
    fields: extraFields,
    async add({ call, vehicle }, values) {
      const form: Record<string, string> = { currencyCode: "USD", vehicleIds: vehicle };
      for (const key of EXTRA_KEYS) form[key] = str(values[key], key);
      await send(call, { method: "POST", path: "/api/your_car/extras", form });
    },
    // A query string and no body, as Turo's own page sends it.
    async update({ call }, itemId, values) {
      await send(call, { method: "PUT", path: `/api/your_car/extras/${enc(itemId)}?${extraQuery(values)}` });
    },
    async remove({ call }, itemId) {
      await send(call, { method: "DELETE", path: `/api/your_car/extras/${enc(itemId)}` });
    },
  },
  faq: {
    setting: "faqs",
    idKey: "faqId",
    async read({ call, vehicle }) {
      return { items: list(await get(call, `/api/listings/${enc(vehicle)}/faqs`), "vehicleFaqs") };
    },
    fields: (item) => ({ question: item.question, answer: item.answer }),
    async add({ call, vehicle }, values) {
      await send(call, {
        method: "POST",
        path: `/api/listings/${enc(vehicle)}/faqs`,
        form: { question: str(values.question, "question"), answer: str(values.answer, "answer") },
      });
    },
    async update({ call, vehicle }, itemId, values) {
      await send(call, {
        method: "PUT",
        path: `/api/listings/${enc(vehicle)}/faqs/${enc(itemId)}`,
        form: { question: str(values.question, "question"), answer: str(values.answer, "answer") },
      });
    },
    async remove({ call, vehicle }, itemId) {
      await send(call, { method: "DELETE", path: `/api/listings/${enc(vehicle)}/faqs/${enc(itemId)}` });
    },
  },
  feature: {
    setting: "features",
    idKey: "featureId",
    async read({ call, vehicle }) {
      return { items: list(await get(call, `/api/listings/${enc(vehicle)}/additional_features`), "features") };
    },
    fields: (item) => ({ feature: item.feature }),
    async add({ call, vehicle }, values) {
      await send(call, {
        method: "POST",
        path: `/api/listings/${enc(vehicle)}/additional_features`,
        form: { feature: str(values.feature, "feature") },
      });
    },
    async update({ call, vehicle }, itemId, values) {
      await send(call, {
        method: "PUT",
        path: `/api/listings/${enc(vehicle)}/additional_features/${enc(itemId)}`,
        form: { feature: str(values.feature, "feature") },
      });
    },
    async remove({ call, vehicle }, itemId) {
      await send(call, { method: "DELETE", path: `/api/listings/${enc(vehicle)}/additional_features/${enc(itemId)}` });
    },
  },
};

/** The item's own id, whatever Turo calls the key. */
function itemId(item: Record<string, unknown>): string {
  return String(item.extraId ?? item.id);
}

const ITEM_KEYS: Record<string, readonly string[]> = {
  extra: EXTRA_KEYS,
  faq: ["question", "answer"],
  feature: ["feature"],
};

async function runItem(kind: string, action: string, ctx: Ctx): Promise<SettingOutcome> {
  const spec = LISTS[kind];
  const keys = ITEM_KEYS[kind];
  const before = (await spec.read(ctx)).items;
  if (action === "add") {
    const values = only(ctx.values, keys);
    await spec.add(ctx, values);
    const after = (await spec.read(ctx)).items;
    const known = new Set(before.map(itemId));
    const added = after.find((item) => !known.has(itemId(item)));
    return {
      ok: true,
      result: `${kind} added`,
      // The list can lag the add by a few seconds; without its id the add
      // is reported but cannot be undone from here.
      detail: {
        before,
        after,
        ...(added ? { restore: { operation: `${kind}_remove`, values: { [spec.idKey]: itemId(added) } } } : {}),
      },
    };
  }
  const wanted = id(ctx.values, spec.idKey);
  const item = before.find((x) => itemId(x) === wanted);
  if (!item) throw new Refused(`no ${kind} ${wanted} on this car; read ${spec.setting} for them`);
  const was = spec.fields(item);
  if (action === "update") {
    const changes = { ...ctx.values };
    delete changes[spec.idKey];
    const merged = { ...was, ...only(changes, keys) };
    await spec.update(ctx, wanted, merged);
    const after = (await spec.read(ctx)).items;
    return {
      ok: true,
      result: `${kind} ${wanted} changed`,
      detail: { before: item, after: after.find((x) => itemId(x) === wanted), restore: { operation: `${kind}_update`, values: { [spec.idKey]: wanted, ...was } } },
    };
  }
  await spec.remove(ctx, wanted);
  return {
    ok: true,
    result: `${kind} ${wanted} removed`,
    // Put back as a new item, with a new id.
    detail: { before: item, after: null, restore: { operation: `${kind}_add`, values: was } },
  };
}

// ---------------------------------------------------------------------------
// The one entry point
// ---------------------------------------------------------------------------

/** Every operation the API may name, for the API's list to be checked against. */
export const OPERATIONS: readonly string[] = [
  ...Object.keys(WHOLE),
  ...Object.values(LISTS).map((l) => l.setting),
  ...Object.keys(LISTS).flatMap((k) => [`${k}_add`, `${k}_update`, `${k}_remove`]),
];

export async function runSetting(payload: SettingPayload, call: Caller): Promise<SettingOutcome> {
  const operation = typeof payload.operation === "string" ? payload.operation : "";
  const mode = payload.mode === "write" ? "write" : "read";
  const values = (payload.values && typeof payload.values === "object" ? payload.values : {}) as Values;
  const vehicle = typeof payload.vehicle_id === "string" || typeof payload.vehicle_id === "number" ? String(payload.vehicle_id) : "";
  const ctx: Ctx = { vehicle, values, call };
  try {
    const whole = WHOLE[operation];
    const listed = Object.entries(LISTS).find(([, l]) => l.setting === operation);
    const item = /^(extra|faq|feature)_(add|update|remove)$/.exec(operation);
    if (!whole && !listed && !item) return { ok: false, result: `this extension does not know the Turo setting ${operation || "(none)"}` };
    const scope = whole ? whole.scope : "car";
    if (scope === "car" && !/^\d+$/.test(vehicle)) return { ok: false, result: "no Turo vehicle id for this car" };

    if (listed) {
      if (mode === "write") return { ok: false, result: `${operation} is changed through its _add, _update and _remove` };
      const read = await listed[1].read(ctx);
      return { ok: true, result: `read ${operation}: ${read.items.length}`, detail: { current: read.items, options: read.options } };
    }
    if (item) {
      if (mode !== "write") return { ok: false, result: `${operation} is a change; read ${LISTS[item[1]].setting}` };
      return await runItem(item[1], item[2], ctx);
    }
    const spec = whole as WholeSetting;
    const { current, options } = await spec.read(ctx);
    if (mode === "read") return { ok: true, result: `read ${operation}`, detail: { current, options } };
    const asked = only(values, spec.keys);
    if (!Object.keys(asked).length) return { ok: false, result: "nothing to change" };
    await spec.write(ctx, current, asked);
    const after = (await spec.read(ctx)).current;
    const restore = { operation, values: spec.restore(current, asked) };
    // Turo answers 200 to some writes it ignores. A change is only reported
    // made once the read shows it; the restore is kept either way, in case
    // part of it took.
    const missed = spec.readBack ? notTaken(asked, spec.readBack(after, asked)) : [];
    return {
      ok: missed.length === 0,
      result: missed.length ? `Turo accepted the write but did not change ${missed.join(", ")}` : `${operation} changed`,
      detail: { before: current, after, restore },
    };
  } catch (error) {
    if (error instanceof Refused) return { ok: false, result: error.message };
    return { ok: false, result: error instanceof Error ? error.message : String(error) };
  }
}
