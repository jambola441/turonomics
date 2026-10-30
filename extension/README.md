# Turonomics — Chrome Extension

Two jobs, both of them "read a page I am already logged into and get the data
out":

1. **Send tolls to Turonomics** — reads your E-ZPass account activity, from
   any page, and posts the statement to the API, which attributes each crossing to the trip
   and guest who was driving.
2. **Export Last 90 Days** — writes a CSV of Turo host trips to Downloads.

## Why the tolls are read in your browser

`www.e-zpassny.com` is behind Imperva bot protection: an unauthenticated
request gets a JavaScript challenge instead of HTML. Scraping it from the
server would mean storing your E-ZPass password, getting a headless browser
past that challenge and past whatever the login's second factor is, and
re-doing all of it whenever any of the three changes.

Your browser has already solved every one of those problems. The session is
yours, the click is yours, and the WAF sees a real browser because it is one.
Nothing stores your E-ZPass credentials — this extension never sees them.

## Sending a statement

1. Be logged in to E-ZPass in this Chrome. Tick **Keep me Logged In** on the
   login form: without it the site logs you out after about ten idle minutes.
2. From any page, click the Turonomics icon → **Send tolls to Turonomics**
   (or press **Read E-ZPass** on the site's invoices page, from a phone).
3. It reports what the API did with it: how many rows it read, how many were
   new, how many it could attribute to a guest, and any transponder that is not
   bound to a car. Logged out is said in those words.

### How it reads

The Account Activity page fills its table from one JSON call to
`api.e-zpassny.com`, made with a short-lived token the site's session route
hands out. The extension makes the same call, from inside an e-zpassny.com page
— an open tab if there is one, otherwise a hidden one it closes after — so it
carries your session and meets the bot protection as the site's own calls do.
No page needs to be open, no form is filled and no pager clicked.

It reads 90-day windows (the API refuses more than three months), **newest
first**, and posts each to the API before starting the next, so a run that dies
on the third has still delivered the first two. The windows meet exactly, so
nothing falls between them. A window with no crossings is normal.

Only **TOLL POSTING** rows are sent. Payments, CBD and dispute credits, parking
and fees are left out, and so are the account holder's name and address, which
the site's answer repeats on every row. What is sent is the site's own CSV
download format, so the parser that reads a hand-uploaded statement reads this
too. Re-sending is harmless: the API fingerprints each crossing and skips the
ones it has.

### Being gentle with E-ZPass

E-ZPass once answered page-driving with what read as a bot block or an outage,
so a send asks for little and stops at the first refusal. It does not try to
look less like a script; a refusal is a reason to stop.

- **One window by default.** A run reads the last 3 months. Older months are
  already on file after the first read. To backfill, raise **E-ZPass months to
  read** under Settings once, then put it back.
- **Paced.** 2 seconds between calls and 4 seconds between windows.
- **Stops on a refusal.** If the answer reads like a block or an outage
  ("unusual activity", "temporarily unavailable", "access denied", a 403 or
  429, …) it stops at once, makes no further requests, and pauses for an hour.

### The run log

After every send the popup has a **Run log** under the result (closed when it
went fine, open when it did not) with a **Copy log** button. It records each
window's dates, how many rows came back, how many were crossings, and what the
API said. Tag numbers, plates and amounts never go in it, so it can be pasted
as is.

### Finding the page's own API

If the date search does not behave, the probe buttons work on the E-ZPass
activity page too, and can show what the page itself calls when you press
Filter — which may be a JSON endpoint worth using instead of driving the form:

1. On the activity page: **Watch while I do something** (it reloads the tab).
2. Type a date range and press **Filter** on the page.
3. **Stop and report what it saw**, then **Copy**.

The report lists each request the page made to e-zpassny.com with its route,
which query parameters it used and how each value is *written* (`us-date(##/##/##)`
— the format, never the date), and the shape of any request body and response. No
cookies or headers are recorded, and every value is masked, so it is safe to
paste.

### If something goes wrong

- **The API refused the file.** The message lists the column names it found. The
  parser's aliases are in `api/src/turonomics_api/parsing/ezpass.py`.
- **Read N rows and kept none.** An E-ZPass download writes charges as negative
  and the API treats a positive amount as a payment. The extension keeps the
  site's sign rather than flipping it, so if this ever appears the site has
  changed how it reports a toll.

### Settings

Under **Settings** in the popup:

- **API** — defaults to `https://turonomics.onrender.com`.
- **Tolls token** — `TOLLS_TOKEN` from the API service, if one is set there.
  Leave it blank if not.

## What has been verified against the real site

The API's route, request body, three-month limit, field names and activity
categories were read off the logged-in site in October 2026. The conversion to
CSV, the crossing filter and the paging are covered by `npm test`, including a
check that the header is the one the API parser's contract file uses.

## Turo trip export

## Output CSV format

```
trip_id,start_time,end_time,license_plate
12345,2024-01-15T10:00:00Z,2024-01-18T14:00:00Z,ABC1234
```

| Column | Description |
|--------|-------------|
| `trip_id` | Turo reservation/trip ID |
| `start_time` | Trip start (ISO 8601) |
| `end_time` | Trip end (ISO 8601) |
| `license_plate` | Vehicle license plate (uppercase, no spaces) |

## Development

```bash
npm install
npm run build        # compile TypeScript → dist/
npm run watch        # watch mode
npm run lint         # eslint
npm test             # compile, then node --test over dist/
```

`src/tolls.ts` holds everything the E-ZPass side decides: the date windows, the
request body, when a window is fully read, which rows are crossings and how they
become CSV. The function injected into the page lives in `src/background.ts` and
does the least possible — get a token, make one call — so that every judgement
is reachable by `npm test` without a browser.

`examples/ezpass/scraped-from-page.csv` is a contract between this extension and
the Python importer: `tolls.test.ts` asserts the CSV writer still produces it byte
for byte, and `api/tests/test_parsing_ezpass.py` asserts the importer still
reads it. Changing the CSV shape on one side fails on the other.

## Loading in Chrome

**Already have it loaded?** `dist/` is gitignored, so pulling is never enough
on its own — the manifest points at `dist/background.js`, and Chrome runs
whatever was built last:

```sh
git pull origin main
cd extension && npm install && npm run build
```

Then **Reload** on the extension card at `chrome://extensions/`, and check the
version on it. Chrome only re-prompts when permissions change, so a release
that adds a capability without adding a permission looks identical until you
reload.

| version | needs | why |
|---|---|---|
| 1.21.0 | build + reload | reads and changes Turo's own listing and account settings when Claude asks through the MCP endpoint: guest instructions, description and name, badges, photo order, trip rules, distance limit, extras, questions, feature lines, business hours, notification and text preferences. Each change is read, written and read back, reported before and after, and can be undone; a write Turo answers OK but ignores is reported as not made. Only the operations listed in `src/turo-settings.ts` can run — the API names one, never an address. No prices, blocked days, reservations, money, reviews or messages. The API hands these only to 1.21.0 or later |
| 1.20.0 | build + reload | sends a message to a guest once you have approved it with **Send** on the trips page: the site queues it, and the extension posts it to the reservation's thread from a Turo tab, the way Turo's own thread page does, then reports whether Turo stored it. The API hands a message only to 1.20.0 or later |
| 1.19.0 | build + reload | **Send tolls** and the site's **Read E-ZPass** work from any page: they call E-ZPass's own API from an open or hidden e-zpassny.com tab, and only need you logged in to E-ZPass in Chrome (tick "Keep me Logged In" — the site logs out after ~10 idle minutes otherwise). Crossings only; payments, credits and parking are left out, and so are the account holder's name and address |
| 1.18.0 | build + reload | **Pull trips** and **File the next toll invoice** work from any page: they use an open Turo tab or a hidden one, and only need you logged in to Turo in Chrome (logged out is said in those words) |
| 1.17.0 | build + reload | runs **Read E-ZPass** queued from the site (the invoices page, from a phone), on an E-ZPass tab left open and logged in on the computer, and reports the result back |
| 1.16.0 | build + reload | Pull also reads Turo's receipt for rentals with an uncharged reimbursement, and the API settles an invoice whose exact toll line the receipt shows paid |
| 1.15.3 | build + reload | fixes 1.15.2 wiping the end date (it focused the field); refuses to search with a date the page rejects; checks the table is back on page 1; the 5-minute cooldown is gone |
| 1.15.2 | build + reload | windows are 90 days, not calendar quarters (E-ZPass refused a 92-day one); the date search submits the form first, which is the method that worked; no wasted 8s waits |
| 1.15.1 | build + reload | the date search is clicked after the dates are typed (it was chosen beforehand and could be a disabled or wrong control), verified by the rows returned, retried with Enter / form submit, and the run stops if it never applies |
| 1.15.0 | build + reload | gentler on E-ZPass: one date window by default, paced clicks, a cooldown between runs, and a stop-and-pause-an-hour if the page reads like a block or outage |
| 1.14.2 | build + reload | sets MUI rows-per-page to the largest option, reads the pager's "1–10 of N" to know the real last page, and calls a stop short of it incomplete (with the pager's controls in the log) |
| 1.14.1 | build + reload | paging no longer stops at page one: an empty table while the next page loads is waited out, not read as the end; the form is not touched mid-load |
| 1.14.0 | build + reload | the Turo probe buttons also work on the E-ZPass activity page, and show how its date search is requested |
| 1.13.1 | build + reload | date fields get the year width they ask for (MM/DD/YY); the run log checks the rows really fall in each window |
| 1.13.0 | build + reload | Send tolls reads the last 12 months in three-month date windows, paging each and posting it as it goes |
| 1.12.0 | build + reload | Pull reads each trip's photos and message thread for the trip view |
| 1.11.0 | build + reload | Pull finds every reservation on the account from Turo's own trip lists, not only the ones the mail mentioned |
| 1.10.1 | build + reload | after filing, reads that rental's invoice from Turo so Turo's email does not count it twice |
| 1.10.0 | build + reload | runs what the site queues — Pull from Turo, and File on a ledger row — within a few seconds while Chrome is open |
| 1.9.0 | build + reload | Pull finds every trip's invoices through Turo's invoice hub — no need to open them |
| 1.8.0 | build + reload | Pull also reads Turo invoices the email did not break down, and the invoice page you have open |
| 1.7.4 | build + reload | toll filing posts to `/api/v3/reimbursement/<reservation>/request` — the route Turo actually uses |
| 1.7.3 | build + reload | the probe says whether an id in a path is the one in the page URL |
| 1.7.2 | build + reload | reads the API path from the page, and names it when a call fails |
| 1.7.0 | build + reload | files a toll invoice end to end; new "File the next toll invoice" button |
| 1.6.2 | build + reload | the probe stops masking its own file descriptors |
| 1.6.1 | build + reload | writes to any non-analytics host are reported |
| 1.6.0 | build + reload | records what requests *send*, and a watch that survives a form submit |
| 1.5.0 | build + reload | pulls Turo's reservation detail; new "Pull trips from Turo" button |
| 1.4.1 | build + reload | first-party analytics filtered out |
| 1.4.0 | build + reload | names the keys it drops; a long status is no longer masked |
| 1.3.0 | build + reload | reads the document as well as the network |
| 1.2.0 | build + reload | manifest version and description changed; `dist/turo-hook.js` is new and is registered by filename at runtime, so a stale `dist/` fails at the moment you click the button rather than at load |
| 1.1.0 | build + reload | the E-ZPass content script was removed; the worker injects what it needs |
| earlier | neither | server-side only |

First time:

1. Run `npm install && npm run build`
2. Open Chrome → `chrome://extensions/`
3. Enable **Developer mode**
4. Click **Load unpacked** → select this `extension/` directory

## Usage

1. Navigate to `https://turo.com/us/en/host-dashboard/trips`
2. Click the Turonomics extension icon
3. Click **Export Last 90 Days**
4. The CSV downloads automatically

## Updating Selectors

If Turo changes their UI markup, update the `SELECTORS` object at the top of
`src/content.ts`. Use Chrome DevTools on the host dashboard trips page to
find the correct CSS selectors for trip rows, start time, end time, and
license plate elements.
