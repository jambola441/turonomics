# Turonomics Web App — Design Exploration

**Status:** exploration / pre-implementation. Nothing here is built yet.
**Decisions taken since:** see [`01-decisions.md`](01-decisions.md) — it
supersedes §7 below, and settles the messaging, ASP, hosting and sequencing
questions this document leaves open.
**First user:** single operator, 4-vehicle Turo fleet, Brooklyn NY.
**Fleet:** 1× Toyota 4Runner, 2× Toyota Corolla, 1× Ford Transit van (new).
**Tracking:** Bouncie OBD-II devices. **Parking:** on-street, subject to NYC alternate side parking (ASP).

---

## 1. The problem, stated precisely

The operator's day is not "managing a fleet." It is servicing a queue of
**time-boxed, place-bound obligations** against a single scarce resource: one
person who can only be in one place at a time.

Every vehicle continuously accrues obligations from four independent sources:

| Source | Obligation | Hard deadline? |
|---|---|---|
| Turo trip calendar | Guest pickup / guest return | Yes — guest is waiting |
| Turnaround | Clean, refuel, inspect before next trip | Yes — bounded by next trip start |
| NYC ASP | Move the car off the cleaning side | Yes — ticket at the posted hour |
| Vehicle health | Low fuel, check-engine, dead battery, oil | Soft, until it isn't |

These four streams are today tracked in four different places (Turo app,
memory, the street sign, the Bouncie app) and reconciled in the operator's
head. That reconciliation is the actual work, and it is what fails to scale
past ~4 cars.

**Design thesis:** the product is not a dashboard. It is a single
**prioritized, geographically-routed action queue** — *what do I do next,
where, and by when* — assembled by merging those four streams. Every module
below exists to feed that queue. The dashboard is a by-product.

This framing is the main thing worth agreeing or disagreeing with before any
code gets written.

---

## 2. Integration reality check

Three findings that constrain the architecture more than any preference does.

### 2.1 Turo has no public API — this is the central constraint

Turo shut off external API access on **2023-04-30** and blocked the
third-party fleet tools that depended on it (CarSync, Fleetwire). There is no
sanctioned programmatic path to trips, guest messages, or listing calendars.

Consequences, in order of preference:

1. **Browser extension as the Turo adapter.** This repo already ships one
   (`extension/`) that scrapes the host trip list and reservation detail pages
   with confirmed selectors. It runs in the operator's own authenticated
   session, on their own account, over their own data. Extend it from
   "export CSV" to "sync trips + read/compose messages."
2. **Manual entry / CSV import.** Always available, always works, zero risk.
   Must remain a first-class path, not a fallback.
3. **Email side-channel.** Turo emails booking + message notifications. An
   inbox parser gets near-real-time trip events with no scraping at all.
   Read-only — outbound still needs 1 or 2.

**Recommendation:** treat Turo as an *untrusted, best-effort, replaceable
adapter* behind an interface. Everything downstream (ASP, turnaround, run
sheet) must work correctly when the Turo adapter is stale or absent. Do not
build a core that assumes live Turo data.

Scraping selectors break. The design must degrade to manual entry without
losing state, and must surface "Turo data is N hours stale" honestly rather
than showing a confidently wrong schedule.

### 2.2 Automated guest messaging needs a scope decision

"Automatic messaging of guests" is the highest-risk item in the original
request, for two independent reasons:

- **Technical:** with no API, sending means driving turo.com UI from the
  extension.
- **Policy:** automating actions in a platform account is the behavior Turo
  moved against in 2023. Full auto-send carries real account risk, and the
  account is the whole business.

Proposed resolution — **a message queue with a graduated autonomy dial**,
same data model at every setting:

| Level | Behavior | Risk |
|---|---|---|
| **Draft** (MVP default) | App composes from template + live data; operator taps *Copy*, pastes in Turo | None |
| **Assisted** | Extension opens the right thread and pre-fills the box; operator hits send | Low |
| **Auto** | Extension sends on schedule | Real — opt-in, per-template, off by default |

Start at Draft. The value is in *composing the right message at the right
moment with the right data in it* (pickup address, ASP-safe return spot, gate
code) — not in the keystroke saved. Ship Draft, measure, then decide whether
Auto is worth the account.

### 2.3 NYC ASP data is available, but street-side resolution is the hard part

Two public sources, both usable:

- **Parking Regulation Locations and Signs** (NYC Open Data, `nfid-uabd`) —
  ~1M sign records with location + sign text, per street segment *and side*.
  Updated monthly. Requires parsing sign text into structured schedules.
- **ASP suspension calendar** — NYC DOT publishes an annual PDF **and an
  `.ics` file**. The `.ics` is directly ingestible for planned suspensions
  (holidays). Emergency/snow suspensions are announced same-day via
  311 / Notify NYC and need a separate live check.

The genuine difficulty is not the data, it's **snapping a GPS fix to the
correct side of the street**. ASP rules are side-specific; a Brooklyn street
is ~10–12 m curb to curb and consumer GPS error is ~5–10 m. A parked car's
reported position cannot reliably distinguish north side from south side.

**Design implication — confirm the guess, never automate it blindly.** When a
vehicle goes ignition-off, the app resolves a *best guess* segment + side and
asks for a one-tap confirmation:

> Parked on **Dean St**, between 4th & 5th Ave — **north side**?
> Cleaning Tue & Fri, 11:30a–1:00p. Next move: **Tue 11:30a (in 2d 4h)**.
> [ Yes ] [ Other side ] [ Different block ]

One tap, ~2 seconds, and the resulting rule is cached for that spot. Being
wrong about a side costs $65; being asked costs a tap. A wrong-side alert
that the operator learns to distrust destroys the feature's entire value, so
confirmation is not friction here — it is the feature.

Heuristics improve the guess (heading on last movement, which side the car
approached from, history of previously-confirmed spots at that location), but
confirmation stays.

---

## 3. Module map

Eight modules. Arrows are data dependencies.

```
                    ┌──────────────────────────────┐
                    │   7. TODAY / RUN SHEET       │  ← the product
                    │   prioritized, geo-routed    │
                    └──────────────────────────────┘
                       ▲      ▲       ▲        ▲
        ┌──────────────┘      │       │        └──────────────┐
        │                     │       │                       │
┌───────────────┐   ┌─────────────┐  ┌──────────────┐  ┌─────────────┐
│ 3. PARKING    │   │ 4. TRIPS &  │  │ 6. CHECK-IN/ │  │ 5. GUEST    │
│    & ASP      │   │  TURNAROUND │  │    CHECKOUT  │  │  MESSAGING  │
└───────────────┘   └─────────────┘  └──────────────┘  └─────────────┘
        ▲                  ▲                 ▲                ▲
        │                  └────────┬────────┴────────────────┘
┌───────────────┐          ┌────────────────┐
│ 2. LOCATION & │          │  Turo Adapter  │  (extension / email / manual)
│    TELEMETRY  │          └────────────────┘
│   (Bouncie)   │
└───────────────┘
        ▲
┌───────────────────────────────────────────────────────────┐
│ 1. FLEET REGISTRY   (vehicles, plates, devices, listings)  │
└───────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────┐
│ 8. MONEY  (tolls ✓ already built, gas, tickets, per-car P&L)│
└───────────────────────────────────────────────────────────┘
```

### 1. Fleet Registry
Vehicles: nickname, make/model/year, plate, VIN, Bouncie device IMEI, Turo
listing ID, fuel type + tank size, insurance, registration/inspection expiry.
The join table the whole system hangs off. Plate is the join key to the
existing EZPass matcher; VIN is the join key to telemetry.

*Why it matters at 4 cars:* two Corollas. Everything must be identifiable at a
glance by nickname and plate, never by model.

### 2. Location & Telemetry (Bouncie)
Bouncie exposes OAuth2 REST (`/v1/vehicles`, `/v1/trips`) **and webhooks**
(push) with retry-on-failure backoff. Webhooks are strongly preferred — trip
start/end, ignition on/off, GPS, geozone enter/exit, fuel level, odometer,
battery, MIL/DTC.

Derived state, which is what the rest of the app consumes:
- **Parked position** = last GPS fix at ignition-off. Not the live fix.
- **In motion / on trip** = ignition on + moving.
- **Fuel level, odometer** — auto-fills checkout/check-in, removes manual entry.
- **Health** — check-engine, low battery.

*Design note:* poll as fallback, webhook as primary; persist raw events
append-only so derived state can be rebuilt when the derivation logic changes.

### 3. Parking & ASP  ← **the novel module**
Per §2.3. Responsibilities:
- Resolve parked GPS → street segment + side (best guess + confirmation).
- Look up ASP schedule for that segment side.
- Apply the suspension calendar (`.ics` + same-day emergency check).
- Compute **"must move by"** per vehicle; emit tiered notifications
  (night before, morning of, 60 min, 15 min).
- **Suppress alerts for vehicles on an active guest trip** — the guest has it,
  it isn't parked, it isn't your problem. This cross-module suppression is
  exactly why the modules must share a run sheet rather than notify
  independently.
- Record the new spot after a move; learn confirmed spots over time.

*Payoff:* one avoided ticket ≈ $65. At 4 cars on Brooklyn streets this is the
single highest-dollar feature in the app, and the one no existing Turo tool does.

### 4. Trips & Turnaround
Trip records from the Turo adapter (or manual). The valuable derived object is
the **turnaround window**: the gap between trip *N* end and trip *N+1* start
for the same vehicle. On trip end, spawn a checklist — wash, vacuum, trash,
refuel, tire pressure, plate/registration check, staging photo — sized to the
window. A 90-minute turnaround and a 3-day gap are different jobs and should
not render identically.

*Conflict detection is the real feature:* next trip starts at 09:00, ASP on
that block is 08:00, turnaround needs 45 min, tank reads 1/8. Surface that as
one problem the night before, not three alerts at 07:55.

### 5. Guest Messaging
Templates bound to trip lifecycle events: booking confirmed, T-24h, day-of
pickup (with **live parking location** and cross-street — the thing that
actually generates "where is the car?" texts), mid-trip, return reminder
(with an **ASP-safe return instruction**), post-trip thanks + review nudge.
Merge fields pull live: `{{parking_cross_street}}`, `{{fuel_level}}`,
`{{asp_next_clean}}`. Autonomy dial per §2.2.

*The compounding win:* telling the guest where to return the car so it lands
on the non-cleaning side removes a move from tomorrow's run sheet. Messaging
is an input to the parking module, not just an output.

### 6. Check-out / Check-in
Structured, timestamped, photo-backed inspection: 4 corners + roof + interior
+ odometer + fuel gauge, damage annotation, fuel level, mileage. Bouncie
pre-fills odometer and fuel so the operator photographs rather than types.

*Purpose is evidentiary.* This record is what wins a Turo damage claim. It
should export as a single timestamped PDF/album per trip. Geotag + server
timestamp each photo.

### 7. Today / Run Sheet
The aggregator and the reason the app exists. Merges every open obligation
across all vehicles into one list ordered by *deadline × location*, so a
morning becomes a route rather than a set of alerts: "Move 4Runner (Dean St,
by 11:30) → gas the van (2 blocks) → check in Corolla-A at 12:15."

Grouping by proximity is what turns three 20-minute errands into one
35-minute loop. That, not any individual alert, is the efficiency gain the
operator asked for.

### 8. Money
The existing EZPass toll reconciliation (`api/`) is the first citizen here.
Extend to gas, cleaning, tickets, insurance, payments → **per-vehicle P&L**.

*This is the module that answers the stated goal* — "expand the business."
Which car earns its keep, what a 5th car would net, whether the van was a
good call. Not MVP, but it is the reason the MVP is worth building.

---

## 4. Domain model (first cut)

```
Vehicle ──< TelemetryEvent          (append-only, from Bouncie)
   │
   ├──< ParkingSession              (spot, segment_side, confirmed, since, must_move_by)
   │         └── StreetSegmentSide ──< AspRule (days, start, end)
   │
   ├──< Trip                        (turo_trip_id, guest, start, end, state)
   │         ├──< TurnaroundTask    (type, due_by, done_at)
   │         ├──< Inspection        (kind: checkout|checkin) ──< InspectionPhoto
   │         └──< GuestMessage      (template, state: draft|queued|sent, body)
   │
   ├──< Expense                     (tolls ✓, gas, cleaning, ticket, insurance)
   └──< MaintenanceItem             (oil, tires, registration, inspection)

Task ← the unifying view: every open obligation, whatever its origin
       (asp_move | turnaround | trip_start | trip_end | message | maintenance)
       normalized to { vehicle, kind, due_by, location, priority, state }
```

`Task` is the key abstraction. Each module *produces* Tasks; the run sheet
only consumes Tasks. That keeps modules decoupled and makes the hero screen
trivial to build and reorder.

---

## 5. Proposed stack

Extends what already exists rather than starting over.

| Layer | Choice | Why |
|---|---|---|
| API | **FastAPI** (existing `api/`) | Already here, tested, typed, deployed |
| DB | **Postgres + PostGIS** | Non-negotiable — segment/side geo queries |
| Jobs | APScheduler → Celery later | ASP deadline computation, notification fan-out |
| Web | **React + Vite, mobile-first PWA** | See below |
| Maps | MapLibre + NYC basemap | No per-load billing |
| Push | Web Push (VAPID); Twilio SMS for ASP | ASP alerts must survive a silenced phone |
| Turo | Existing Chrome extension, extended, **plus Gmail ingestion** | Extension alone can't run on a phone (D2) |
| Hosting | **Render** — web service, Postgres, cron | Decided (D8) |
| Auth | **Google sign-in** | Scales to staff, shares the Gmail identity (D8) |

**Mobile-first, explicitly.** The request said "web app," but the actual usage
is standing on a Brooklyn sidewalk at 8am in the cold with one hand free. Every
hero screen is designed at 390 px and thumb-reachable; desktop is the
secondary layout for month-end reconciliation and P&L. Installable PWA, not an
app-store build — no review cycle, and the extension already anchors the
product to a browser.

*Flagging one dependency:* PostGIS is the one piece of infra the current
deployment doesn't have. Everything else is additive.

---

## 6. Proposed MVP cut

Ruthlessly scoped to one operator, four cars, highest pain first.

**In:**
1. Fleet registry — 4 vehicles (M1)
2. Bouncie webhook ingest → parked position + fuel + odometer (M2)
3. **ASP clock + confirm-the-spot + tiered notifications** (M3) ← the wedge
4. Trip sync via extension, with manual entry as equal-status fallback (M4)
5. Turnaround checklist w/ conflict detection (M4)
6. Today run sheet (M7)
7. Message templates, **Draft level only** (M5)

**Out (deliberately):**
- Auto-send messaging — needs a risk decision first (§2.2)
- Full P&L — keep the existing toll matcher standalone until there's expense volume
- Multi-user / staff roles — one operator today; don't model permissions yet
- Route optimization — with 4 cars in one neighborhood, sort-by-proximity beats a solver
- Native apps, dynamic pricing, maintenance scheduling

**Build order (settled — D9):** registry → Bouncie → ASP with capture-once →
run sheet → trip sync and turnaround → Draft messaging. The citywide sign
parser (D3) lands behind all of it and silently stops asking for captures.
Module 3 alone justifies the project; validate it in the real world before
building on top of it.

---

## 7. Open questions — resolved

All five were settled on 2026-09-15; see [`01-decisions.md`](01-decisions.md).
In short:

| Question | Answer |
|---|---|
| Messaging autonomy | Draft only for v1 (D5) |
| ASP notification channel | Push only, iPhone — with a delivery heartbeat and SMS behind a flag (D6) |
| Turo trip source | Extension **and** server-side Gmail ingestion (D2) |
| Van-specific handling | Still open — needs a look at the Transit's actual blocks |
| Growth horizon | 15–25 cars with help; helper 6–12 months out (D7) |

One correction to §2.1 above, learned since: the extension cannot be the sole
ingress under any configuration, because Chrome extensions do not run on Chrome
for Android and an MV3 service worker only lives while a desktop browser is
open. That is what forced the email path in D2.

One correction to §3, module 2: Bouncie reports fuel level **only where the
vehicle sends it over OBD**, and odometer has three tiers of fidelity. The
registry needs a per-vehicle capability flag, and check-out must degrade to
manual entry per car.
