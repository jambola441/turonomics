# Turonomics — Chrome Extension

Two jobs, both of them "read a page I am already logged into and get the data
out":

1. **Send tolls to Turonomics** — reads an E-ZPass account-activity page and
   posts the statement to the API, which attributes each crossing to the trip
   and guest who was driving.
2. **Export Last 90 Days** — writes a CSV of Turo host trips to Downloads.

## Why the tolls are scraped in your browser

`www.e-zpassny.com` is behind Imperva bot protection: an unauthenticated
request gets a JavaScript challenge instead of HTML. Scraping it from the
server would mean storing your E-ZPass password, getting a headless browser
past that challenge and past whatever the login's second factor is, and
re-doing all of it whenever any of the three changes.

Your browser has already solved every one of those problems. The session is
yours, the click is yours, and the WAF sees a real browser because it is one.
Nothing stores your E-ZPass credentials — this extension never sees them.

## Sending a statement

1. Log in to E-ZPass and open **Account Activity**.
2. Click the Turonomics icon → **Send tolls to Turonomics**.
3. It reports what the API did with it: how many rows it read, how many were
   new, how many it could attribute to a guest, and any transponder that is not
   bound to a car.

### Reading three months at a time

When the page has a from/to date pair and a search button the extension
recognises, it drives them itself: it asks for the last 12 months as four
consecutive three-month windows, **newest first**, and for each one fills in the
dates, runs the search, pages through the results, and posts that window to the
API before starting the next. Posting per window means a run that dies on the
third has still delivered the first two. The windows meet exactly — each starts
the day after the older one ends — so nothing falls between them. A window with
no crossings is normal and is not an error.

In this mode the site's own CSV download is **not** used: its link is built for
whatever range the page last showed, and a stale one would return the same rows
for every window and look complete.

If the page has no date fields or search button it recognises, it falls back to
reading what is on screen. In that mode it prefers a CSV the site generates
(there is usually a download link on that page) and otherwise scrapes the
activity table, **paging through it itself**. If the page has a rows-per-page
control it asks for the largest option first, because a page not loaded is a page
that cannot go wrong, then clicks through what remains and merges the result. It
stops when a page repeats, when the click changes nothing, or at 40 pages — and
the last of those is reported as a warning, because rows may be missing.

Re-sending is harmless either way: the API fingerprints each crossing and skips
the ones it has.

The paging is driven from the service worker rather than a content script, since
a "next" link that navigates would tear a content script down mid-loop.

### Being gentle with E-ZPass

E-ZPass has started answering this with what reads as a bot block or an outage,
so the send asks for less and stops sooner. It does not try to look less like a
script; a refusal is a reason to stop.

- **One window by default.** A run reads the last 3 months. Older months are
  already on file after the first read. To backfill, raise **E-ZPass months to
  read** under Settings once, then put it back.
- **Paced.** 2 seconds before each click that makes the site fetch something and
  4 seconds between windows.
- **Cooldown.** A run is refused if the last one was under 5 minutes ago, before
  any request is made.
- **Stops on a refusal.** If the table goes missing and the page's own words read
  like a block or an outage ("unusual activity", "temporarily unavailable",
  "access denied", …) it stops at once, makes no further requests, and pauses
  for an hour. The log quotes the words around the match, digits masked, because
  "you look automated" and "we are down" call for different responses.

### The run log

After every send the popup has a **Run log** under the result (closed when it
went fine, open when it did not) with a **Copy log** button. It records, step by
step: which fields it took for the date range and which button it would click,
what it typed into each, whether the table changed after the search, how many
rows each page had, why paging stopped, and what the API said per window. Tag
numbers, plates and amounts never go in it — rows appear only as masked shapes
like `###|aa aaa####|$-#.##` — so it can be pasted as is.

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

### If it cannot find the activity

The popup shows a **What was on the page** report naming each table's columns
and masking their values — `###########` where a tag number was. That is safe to
paste into an issue; it says what the markup looks like without saying what your
tolls were.

Two specific things it will tell you about:

- **The API refused the file.** The message lists the column names it found. The
  parser's aliases are in `api/src/turonomics_api/parsing/ezpass.py`; adding the
  page's names there is usually the whole fix.
- **Read N rows and kept none.** An E-ZPass download writes charges as negative
  and the API treats a positive amount as a payment. If the page shows charges
  as positive, every row looks like a payment. The extension does not flip the
  signs itself: an activity table mixes tolls with replenishments, and negating
  everything would turn a payment into a charge and bill it to a guest.

### Settings

Under **Settings** in the popup:

- **API** — defaults to `https://turonomics.onrender.com`.
- **Tolls token** — `TOLLS_TOKEN` from the API service, if one is set there.
  Leave it blank if not.

## What has not been verified against the real page

The selectors are structural on purpose — "every `<table>`, scored by whether
its header has a date, an amount and a tag or plate column" — because the
logged-in activity page is behind a login and a WAF and could not be opened
while this was written. The scoring, the CSV assembly, the row cleanup and the
masking are all covered by `npm test`. Which table on the real page wins is not,
and cannot be until somebody runs it on the real page. The same goes for the date
fields and search button: they are found by what they are called and what type
they are, not by selector, and whether that finds the real ones is only known by
running it. If it does not, it reads the page as shown rather than guessing. The first run is
therefore the test: if it picks the wrong table or the API rejects the columns,
the report in the popup says so and names what it saw.

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

`src/tolls.ts` holds everything the E-ZPass side decides, and nothing that
touches the DOM: which table is the statement, which control advances a page,
which page size to ask for, when paging has finished, how to merge the pages,
and the date windows with the fields and button that drive them. The functions injected into the page live in `src/background.ts` and do
the least possible — read the DOM, click a thing, set a select — so that every
judgement is reachable by `npm test` without a browser.

`examples/ezpass/scraped-from-page.csv` is a contract between this extension and
the Python importer: `tolls.test.ts` asserts the scraper still produces it byte
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
