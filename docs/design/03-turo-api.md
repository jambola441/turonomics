# Turo's own API, as the pages use it

Observed 2026-10-05 with the extension's page probe, from a trip page and from
`reservation/<id>/reimbursement/invoice?invoiceId=<id>`. Shapes only — the
probe masks every value, so what follows is field names and value *kinds*.

The point of writing this down: the trip scraper matches on emotion class
names (`p.css-dbccrj-StyledText`) and reads a date range with no year in it.
These endpoints carry the same data typed, with ids, and in epoch millis.

Everything here is fetched with the session the browser already holds, so the
extension can call it directly. None of it needs a key.

**The pages are not server-rendered.** An earlier read of a broken probe said
they were: the trip page appeared to fetch nothing, so the data "must" have
shipped inside the document. It does not. With the probe's two bugs fixed — a
relative URL reported as the bare word `url`, and analytics beacons outranking
real calls because the filter read their query strings — the trip page shows
`/api/reservation/detail` plainly, and the only JSON in the document is 892
characters of schema.org markup for search engines. An absence reported by a
tool is a claim about the tool first.

## The two that matter

### `GET /api/reservation/detail?reservationId=<id>&oppTermsAware=true`

```
allowedToRequestReimbursement: bool
booking: {
  start:          {epochMillis, localDate, localTime}
  end:            {epochMillis, localDate, localTime}
  gracePeriodEnd: {epochMillis, localDate, localTime}
  cost, costWithCurrency: {amount, currencyCode}
  distanceLimit: {scalar, unit: MI, unlimited}, mileageLimit
  location: {address, city, state, latitude, longitude, timeZone, type: STREET}
  vehicleRegistration: {licensePlate, state, insuranceCardUrl}
}
odometerDetail: {
  checkInOdometerReading:  {scalar, unit: MI}
  checkOutOdometerReading: {scalar, unit: MI} | null
  distanceDriven, excessDistance, latestOdometerReading
  distanceOverageFee: {distance, money: {amount, currencyCode}}
}
cleaningRecord: {cleaned: bool, cleanedAt}
checkInParkingRecord, checkOutParkingRecord
created: {epochMillis, ...}, driverRole: HOST, endEditable: bool
cancellationPolicyType: NON_REFUNDABLE
id, messageCount, cohosts[], owner, +38 more
```

Three fields here replace guesses in this codebase:

**`booking.gracePeriodEnd`** is the free-cancellation deadline, and it is no
use at all for a late return. Measured across all 57 reservations on the live
account, every one of them falls *before the trip starts* — a cluster at
exactly 24 hours before pickup, and much larger negatives for bookings made
months ahead:

```
61311766: grace  -24.0h from start,  -86.0h from end
61838661: grace -873.5h from start, -938.0h from end
```

Written down because the first reading of this field was the opposite — "this
is the real boundary" — and building on it would have moved the toll window to
a day before the rental began. `GET /api/turo/grace` reports the offsets and
states the reading, so this is a measurement rather than an impression.

The toll matcher keeps `TOLL_OVERRUN_GRACE_MINUTES` as a **ceiling**, and Turo
does not appear to publish a return grace anywhere in this payload. What does
settle it is the fleet's own trackers: Bouncie reports engine state on every
poll, so the moment a car came to rest and stayed at rest is recorded already.
`ingest/returns.py` reads it, and narrows the window to the car's actual
return — see the note there for why it narrows only.

**`allowedToRequestReimbursement` is not the filing window**, whatever it
looks like. It was read that way and briefly made a hard gate, and against the
live account it is `false` for all 37 rentals — including one that was then
filed by hand, accepted, and charged to the guest an hour and a half later.
The gate answered "nothing to file" for the whole account while $1,218 sat
uncollected.

It is stored and reported, in case it starts meaning something legible, and
nothing is decided by it. The 90-day window from the help pages remains the
gate.

**`booking.vehicleRegistration.licensePlate`** identifies the car without
matching a nickname or guessing between two identical Corollas.

`+38 more` was not captured — the probe's key cap. Raised to 80, and it now
names what it drops, so the next run will say whether an *actual* check-out
time is in there. `odometerDetail.checkOutOdometerReading` being null on a
finished trip suggests some of these only fill in once the host checks out.

### `GET /api/v2/reservations/<id>/reimbursement/invoice/<invoiceId>`

```
invoiceId, reimbursementId
reimbursementStatus: str(34)          <- a long screaming-snake status
description, title
lineItems: [{
  id, type: TOLL_REIMBURSEMENT, title, description
  total: {amount, currencyCode}
  reimbursementLineItems: []
  evidenceImagesResponse: {images: [{imageId, step: TRIP_PHOTO,
                                     photographerDriverRole: HOST, ...}]}
}]
fees: []
total:           {amount, currencyCode}
totalBeforeFees: {amount, currencyCode}
tripInfo: {
  tripStart: {epochMillis, localDate, localTime}
  tripEnd:   {epochMillis, localDate, localTime}
  timeZone, distanceUnit: MI
  guestFirstName, hostFirstName, vehicleMakeModelYear
}
```

**`lineItems[].type`** is the charge type as an enum. The mail parser gets at
this with a regular expression over a label — `\btolls?\b`, and a list of
other words to refuse a label like "Tolls and fuel". `TOLL_REIMBURSEMENT` is
the same fact, stated.

**`totalBeforeFees` beside `total`** is the ×0.9 that the email shapes note
records as a mystery: $45.88 of additional distance totalling as $41.29.
`fees` is empty on this toll invoice, and `total` equals `totalBeforeFees`.
So Turo's cut is a fee on some charge types and not others, and it is itemised
rather than inferred. An invoice's toll line is gross either way, which is why
it reconciles against a statement and the total does not.

## Filing a reimbursement

Observed 2026-10-05 by watching a real toll invoice being filed for
reservation 58626257, with the extension's watch mode.

### `GET /api/reservations/<id>/reimbursement-options`

What may be claimed, and how much:

```
alreadyRequested: {amount, currencyCode}
maximumReimbursementPerTrip: {amount, currencyCode}
maximumReimbursementRequestAmount: int
options: [{
  itemType: TOLLS | TICKETS
  name, description, shortDescription, unit: CURRENCY
  reimbursementCalculationDto: {
    type:            TOLL_REIMBURSEMENT | TICKET_REIMBURSEMENT
    invoiceItemType: TOLL_REIMBURSEMENT | TICKET_REIMBURSEMENT
    reimbursementInputUnit: CURRENCY
    maxReimbursementAmount: {amount, currencyCode}
    hostEarningsCalculationDto: {hostEarningsTakeRate, hostEarningsUnitCost}
  }
}]
tripInfo: {tripStart, tripEnd, timeZone, guestFirstName, hostFirstName, …}
```

`hostEarningsTakeRate` is the ×0.9 that the email shapes note recorded as a
mystery, stated per item type rather than inferred from two invoices. The
three caps — `alreadyRequested`, `maximumReimbursementPerTrip` and
`maximumReimbursementRequestAmount` — are worth reading before filing rather
than after being refused.

### `POST /api/v3/reimbursement/<reservationId>/request`

```
sent: {
  items: [{
    amount: num                     # dollars, not cents
    itemType: TOLLS                 # from reimbursement-options
    invoiceItemType: null           # null on the wire, despite the options
    mediaEvidenceUuids: [uuid]      # evidence, uploaded beforehand
  }]
  message: str                      # the host's note to the guest
  automatedTollTransactionsDto: {transactionUuids: []}
  automatedOnTripEVTransactionsDto: null
  evPostTripRechargingBatteryLevelsDto: null
}
-> {messageUuid: uuid, reimbursementId: int}
```

**`v3`, read directly, and not the `v2` the page's other calls use.** The
operator filed 58426608 by hand with the watch running, and the report showed
`POST /api/v3/reimbursement/<digits(8)=the one in the page url>/request -> 200`.
On the same page, `/api/v2/driver/detail` and
`/api/v2/reservations/<id>/reimbursement/invoice` were fetched. Turo versions
each route separately.

How this got wrong twice is worth keeping:

* It was first hard-coded as `us`, from a masked `/api/<str(2)>/…`, and 404'd.
* The extension then read the segment from whatever the page had fetched. A
  filing page never fetches the filing route before somebody files, so the
  only versioned calls it could find were the `v2` ones. That 404'd too, as
  `/api/v2/reimbursement/58426608/request`.
* The operator's reading of `en` was the page URL, `turo.com/us/en/…`, not the
  API path.

So the path is now a constant (`reimbursementRequestPath` in
`extension/src/turo.ts`), and the id in it is the reservation id. If Turo moves
the route to `v4`, the filing fails with the path named in the message, and a
watch report on a hand filing will show the new one.

Two things to note before building on this.

**`mediaEvidenceUuids` comes from a separate upload**, observed on the second
run:

```
POST /api/reservation/image                       # multipart, same origin
  sent: {file: <file image/png 92579 bytes>, reservationId: int}
-> {uuid, imageId, step: TRIP_PHOTO, photographerDriverRole: HOST, success}
```

The `uuid` from that response is what goes in `mediaEvidenceUuids`. So filing
is two calls, in order: upload, then request. No presigned storage host and no
separate media service — the guess that the upload must have gone off-origin
was wrong, though the widened write filter it prompted is worth keeping.

Note the image is uploaded as `step: TRIP_PHOTO`, the same bucket as the
check-in photos, rather than as anything invoice-specific. Evidence is a trip
photo that a reimbursement happens to point at.

**`automatedTollTransactionsDto.transactionUuids` was empty** on a manual
filing. Turo evidently has its own automated toll feed, and those uuids are
presumably its transactions rather than ours. Filing with an empty list and an
amount works, which is the path this app would take.

## Also present

| endpoint | carries |
|---|---|
| `GET /api/<version>/feeds/conversation?page&itemsPerPage` | paginated reservations: `id`, `statusCode: BOOKED \| COMPLETED`, `booking`/`request`/`interval`/`cancelledRequest` start+end, `vehicle` with `licensePlate` and `vin`, `renter`, `location`, plus `numPages` |
| `GET /api/<version>/reservation/conversation?reservationId` | the whole message thread: `authorDriverRole: HOST \| GUEST`, `sentTime`, `sentTimeZone`, `text`, attached `media.images[]` |
| `GET /api/reservation/photos?reservationId` | 39 trip photos with `step: TRIP_PHOTO \| OWNER_CHECK_IN` and `imageType: ODOMETER` |
| `GET /api/me` | the host: `driverRoles`, `driverLicenseStatus`, `alerts.searchExcludedVehicleIds` |
| `GET /api/reservations/<id>/my-role` | `driverRole` |
| `GET /api/sse/auth?recipientId` | a token for `sse.turo.com/drivers/<id>/events`, a live event stream |
| `GET /api/properties/<locale>` | app config |
| `GET /api/feature-flags?platform` | 122 flags as `{name, treatment: on \| off}` |

Two of these are worth more than they look.

**The conversation feed is the trip enumeration.** It is paginated and it
carries the reservation with four different start/end pairs —
`request`, `booking`, `interval` and `cancelledRequest`. Email only ever shows
one of them, and which one is not stated. A modified trip is exactly where
those pairs disagree.

**`sse.turo.com/drivers/<id>/events`** is a live stream, authorised by a token
this endpoint hands out. The alerting in this app polls Gmail every ten
minutes; this is where a guest message actually arrives from.

## What reads it

`extension/src/background.ts` holds `pullTuro`, and it holds no policy: it asks
`GET /api/turo/wanted` which reservations to fetch and which route to fetch
them from, fetches each with the page's own session, and posts the bodies back
to `POST /api/turo/details` unmodified.

Everything those bodies mean is decided in `ingest/turo_detail.py`, where
`npm test` is not needed to run it. That split is on purpose: a change in
Turo's shape is then a change to one Python module with tests against the
observed payload, rather than to a TypeScript file that has to be rebuilt and
side-loaded before anyone can see whether it worked.

Turo's booking times win over the email's, which is the whole reason for
pulling: email states a trip's times as they were when it was sent, and nothing
re-states them when a guest extends. Each change is reported rather than
applied quietly, and a change re-runs toll attribution.

### Invoices the email did not break down

The "has been charged" email links the receipt rather than the invoice and
often gives only a total. A rental carrying one of those cannot be filed for:
Austin's 59077848 had $140.40 charged that way beside $40.71 of crossings, and
nothing here could say the $140.40 was not those tolls. Next-draft skips such a
rental and the ledger calls it "check Turo's invoice".

The pull reads them. `GET /api/turo/wanted` lists `invoices` — reservation and
invoice id pairs for invoices with no readable toll line — and the
`invoice_path` to fetch each from. The extension also adds the invoice page
that is open, because an invoice seen only through the charged email has no id
the API knows: opening it on Turo and pressing Pull is how that one is read.
Bodies go to `POST /api/turo/invoices` unmodified.

`ingest/turo_invoice.py` takes the toll share from `lineItems[].type`, never
the title. It matches the invoice to the mail's row by either Turo id, then —
only for a row with no lines — by total, and refuses to guess between two such
rows of one amount. A toll line learned this way stamps as asked the crossings
imported before the invoice was first seen, and ticks them off if it was
charged. An invoice the mail never saw is recorded as filed, not charged,
because `reimbursementStatus` has not yet been read unmasked; the response
reports every status it sees until it has.

## What this does not answer

- Whether a check-out *time* (as opposed to an odometer reading) is in the
  `+38 more` of reservation detail. Still unanswered: the two clean reports
  were taken on 1.3.0, before the probe started naming the keys it drops.
- How an invoice is filed. The invoice page here is a finished invoice; the
  filing form is a different route, and its POST has not been observed.
- Whether the conversation feed lists reservations with no messages.
