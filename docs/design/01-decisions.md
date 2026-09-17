# Decisions

Settled in conversation on 2026-09-15. These supersede the open questions in
[`00-overview.md`](00-overview.md) §7. Where a decision went against the
recommendation, the recommendation and the reason are kept so the trade-off
isn't lost.

---

## D1 — Product shape: action queue now, admin dashboard later

Both views are wanted and they overlap. Build the **action queue** first; the
admin dashboard comes later and reuses the same `Task` records.

This gets more important, not less, once there's a helper (D7): "what happened
yesterday and who did it" stops being a nice-to-have the moment more than one
person touches the cars.

## D2 — Turo ingress: extension **and** server-side email

The browser extension is the Turo adapter, with a specific and limited job:

- **Opportunistic sync.** Scrapes trips whenever Chrome is open, POSTs to the API.
- **The only write path.** It alone holds the authenticated Turo session.
- **Not the system of record**, and not the only ingress.

**Hard constraint that forced this:** Chrome extensions do not exist on Chrome
for Android, and an MV3 service worker only runs while a desktop browser is
open (killed after ~30 s idle; `chrome.alarms` can wake it, but only on a
running machine). The operator's primary device is a phone on a sidewalk. The
extension can therefore never be the sole ingress.

So the server also ingests **Turo's booking and message notification emails**
via the Gmail API — 24/7, no browser, events within about a minute. Manual
entry stays a first-class path.

Consequence carried into D4: anything the extension *sends* inherits the
laptop's uptime.

## D3 — ASP rules: parse the citywide NYC Open Data sign dataset

Source: `nfid-uabd` (Parking Regulation Locations and Signs, ~1M records,
monthly refresh), plus the NYC DOT suspension calendar `.ics` for planned
suspensions and a same-day check for emergency ones.

*Recommended instead:* a curated library of the ~20–30 blocks actually used —
an afternoon of work, exact, covering nearly all real parking. **Chosen:**
citywide parsing, for coverage beyond the home blocks.

Accepted cost: weeks before the first alert, free-text sign parsing, and
side-of-street geometry as the genuinely hard part.

De-risking, agreed alongside:

- The ~20–30 real blocks become the **ground-truth test set** the parser must
  reproduce. No parser ships that regresses them.
- **Manual override** for any block the parser gets wrong, permanently.

## D4 — Low-confidence blocks: capture once, cache forever

When the parser can't confidently resolve a block, the app asks for the sign to
be read **the first time the car parks there**, then caches it permanently.

Two payoffs: the curated library builds itself out of real parking, and every
capture is a labelled example the parser is measured against. This is also what
makes D3's sequencing work — see D9.

Explicitly rejected: staying silent on unresolved blocks. Silence is
indistinguishable from "nothing due today", which is how the operator gets
ticketed while believing they're covered.

## D5 — Messaging: Draft only for v1

App composes with live merge fields; operator copies and pastes into Turo.

Two independent reasons, either sufficient:

- **Account risk.** Automating actions in a Turo account is the behaviour Turo
  moved against in 2023, and the account is the business.
- **Reliability ceiling.** Auto-send runs through the extension, so a message
  scheduled for 1:00p only fires if Chrome happens to be open at 1:00p.

Assisted and Auto stay in the data model behind a per-template switch, off.

## D6 — Alerts: web push only, with a delivery heartbeat

Operator carries an **iPhone**, where web push requires iOS 16.4+, the PWA
installed to the home screen, and is throttled for apps not recently opened.

*Recommended instead:* push plus SMS escalation at T-60 and T-15 (about a cent
a message against a $65 ticket). **Chosen:** push only.

Mitigations built in rather than bolted on later:

- A visible **"last alert delivered"** heartbeat, so silence is detectable
  rather than ambiguous.
- SMS behind a **config flag**, not a rewrite — one env var and a Twilio key.

## D7 — Growth: 15–25 cars with help, helper 6–12 months out

Build a **single-operator UI**. Structurally:

- `Task` carries a **nullable `owner`** from day one.
- Nothing hardcodes "me" — no implicit current-user assumptions anywhere.

Assignment, per-person run sheet filtering and shift handoff then become a
feature rather than a migration. Photo authorship matters at that point too:
someone else's check-out photos become the operator's evidence.

## D8 — Platform: Render, Google sign-in

| Concern | Decision |
|---|---|
| Hosting | **Render** — web service, managed Postgres (PostGIS extension), cron jobs |
| Auth | **Google sign-in** — scales to staff accounts, no passwords, same identity as Gmail ingestion (D2) |
| Photos | S3-compatible object storage — **credential needed**, see below |

## D9 — Build order: app first, parser lands behind it

1. Fleet registry (4 vehicles)
2. Bouncie webhook ingest → parked position, fuel, odometer
3. ASP module with **capture-once** (D4) — usable in roughly a week
4. Today run sheet
5. Trip sync (extension + email, D2) and turnaround checklist
6. Message templates, Draft only (D5)
7. **Sign parser** (D3) lands behind all of it and silently stops asking for captures

The parser is weeks of work with nothing visible until it lands. Shipping the
capture-once path first means the app is in daily use while the parser is being
built, and every captured block is a test case it has to pass.

## D10 — EZPass toll matcher: folded into the new service

The existing `api/` matcher gets the vehicle registry and real trip records
instead of two uploaded CSVs, so tolls attach to trips automatically and become
the first real numbers in the Money module.

The **CSV upload endpoint stays working** — an ad-hoc EZPass export needs
somewhere to go.

---

## D11 — The Transit is not a special case

It runs **passenger plates**, so none of the commercial-vehicle rules apply: no
commercial overnight restrictions, no loading-zone or commercial-metered
regulations. Standard ASP, same rules engine, a fifth row in the same table.

One residual, which is a *spot* concern rather than a *rules* concern: a Transit
does not physically fit everywhere a Corolla does, and NYC has no length-based
cleaning rule. So the confirmed-spot cache carries a **`fits_van`** flag, and
any suggestion that recommends somewhere to park — the turnaround "park on a
clean-side block" step, and the ASP-safe return instruction merged into guest
messages — filters by vehicle. Otherwise the app will confidently send the van
to a spot it can't use.

## D12 — Verified against the live Bouncie API (2026-09-17)

Their OpenAPI spec is at `https://docs.bouncie.dev/openapi.json`. Facts that
contradict what a reasonable guess would have produced:

| | |
|---|---|
| Auth header | `Authorization: <access_token>` — **raw, not `Bearer`** |
| Token exchange | `POST https://auth.bouncie.com/oauth/token`, JSON body |
| Authorize | `https://auth.bouncie.com/dialog/authorize`, PKCE supported |
| Auth code expiry | **None.** Only invalidated by re-authorizing |
| Access token | 1 hour, with a refresh token |
| API base | `https://api.bouncie.dev`, `GET /v1/vehicles`, `GET /v1/trips` |
| Ignition events | **Do not exist.** Parked state derives from `tripEnd` |
| Battery | `{"status": "normal"}` — a status string, not a voltage |
| `stats.localTimeZone` | a UTC offset (`"-0400"`), not an IANA zone |

Webhook events available: `deviceConnect`, `deviceDisconnect`, `battery`,
`mil`, `vinChange`, `tripStart`, `tripData`, `tripMetrics`, `tripEnd`,
`applicationGeozone`, `userGeozone`.

The offset-not-zone detail matters: an offset cannot describe a DST
transition, so street-cleaning deadlines are computed in `FLEET_TIMEZONE` and
never from that field.

### Vehicle capability discovery — the answer

Both vehicles currently on the account report **true OBD fuel level and
odometer**, so check-out auto-fills rather than asking for typing:

| Vehicle | Fuel level | Odometer | Battery | MIL |
|---|---|---|---|---|
| Jolene — 2025 Toyota Corolla | yes | yes | normal | clear |
| Jimmy — 2023 Toyota 4-Runner | yes | yes | normal | clear |

**Only two of the four vehicles are on the Bouncie account.** The second
Corolla and the Transit are absent — no device, not activated, or a separate
account. Until that is resolved, half the fleet has no telemetry and therefore
no automatic parking clock. This is the single biggest open item.

### The side-of-street claim, measured

The confirm-the-spot design (D4) rests on GPS being unable to resolve which
side of a street a car is on. Measured against Jimmy's real reported fix at
590 Bergen St, with curb lines 11 m apart:

```
distance to north curb   3.9 m
distance to south curb   7.2 m
gap                      3.3 m   ← smaller than the device's own error
```

A "nearest side wins" heuristic would be a coin flip. This is asserted in
`api/tests/test_schema.py::test_side_of_street_is_genuinely_ambiguous`, so if
the assumption ever stops holding, a test says so rather than a memo.

## Still mine to decide (flagging, not asking)

- **Reading guest message threads.** Draft-only messaging (D5) only needs
  outbound. But scraping inbound threads makes the Inbox real and surfaces
  "where is the car?" while it still matters. Proposed: read them. Low cost,
  no extra risk beyond what the extension already does. Say if you'd rather not.
- **Per-vehicle telemetry capability flags.** Bouncie reports fuel level *only
  if the vehicle sends it over OBD*, and odometer has three tiers (true OBD
  reading, distance-derived, GPS-derived). So the registry needs a capability
  flag per vehicle, discovered at setup, and check-out must degrade to manual
  entry for any car that doesn't report. Toyotas generally do; the Transit
  needs checking. Resolvable in minutes once the API key is in.

## Credentials needed to start

Supplied as environment variables. Say if any of these don't exist yet and I'll
walk through getting it.

| Credential | Needed for | Blocking |
|---|---|---|
| `BOUNCIE_CLIENT_ID` / `_SECRET` / redirect URL | Telemetry ingest, capability discovery | **Step 2** |
| Render workspace access | Provisioning web service, Postgres, cron | **Step 1** |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Sign-in (D8) | Step 1 |
| Gmail API access (same Google project) | Turo email ingestion (D2) | Step 5 |
| S3-compatible bucket + keys (R2 or similar) | Check-out / check-in photos | Step 5 |

NYC Open Data needs no credential (an app token only raises rate limits).

**Names are fixed in [`.env.example`](../../.env.example)** at the repo root,
annotated with who supplies each one. Paste values into Render's environment
settings; `.env` is gitignored.

Two notes on that file worth reading before you fill it in:

- `ALLOWED_SIGNIN_EMAILS` is not optional. Google sign-in without an allowlist
  means any Google account can reach your fleet.
- `FLEET_TIMEZONE` is explicit rather than inherited from the process timezone,
  because street cleaning is local wall-clock time and DST shifts it.
