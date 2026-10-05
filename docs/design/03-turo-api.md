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

The toll matcher keeps `TOLL_OVERRUN_GRACE_MINUTES`, and it keeps being a
guess. Turo does not appear to publish a return grace anywhere in this payload.

**`allowedToRequestReimbursement`** is the filing window, authoritatively.
`TOLL_FILING_WINDOW_DAYS = 90` is a number from the help pages; this is Turo
saying yes or no about this reservation.

**`booking.vehicleRegistration.licensePlate`** identifies the car without
matching a nickname or guessing between two identical Corollas.

`+38 more` was not captured — the probe's key cap. Raised to 80, and it now
names what it drops, so the next run will say whether an *actual* check-out
time is in there. `odometerDetail.checkOutOdometerReading` being null on a
finished trip suggests some of these only fill in once the host checks out.

### `GET /api/<locale>/reservations/<id>/reimbursement/invoice/<invoiceId>`

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

## Also present

| endpoint | carries |
|---|---|
| `GET /api/<locale>/feeds/conversation?page&itemsPerPage` | paginated reservations: `id`, `statusCode: BOOKED \| COMPLETED`, `booking`/`request`/`interval`/`cancelledRequest` start+end, `vehicle` with `licensePlate` and `vin`, `renter`, `location`, plus `numPages` |
| `GET /api/<locale>/reservation/conversation?reservationId` | the whole message thread: `authorDriverRole: HOST \| GUEST`, `sentTime`, `sentTimeZone`, `text`, attached `media.images[]` |
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

## What this does not answer

- Whether a check-out *time* (as opposed to an odometer reading) is in the
  `+38 more` of reservation detail. Still unanswered: the two clean reports
  were taken on 1.3.0, before the probe started naming the keys it drops.
- How an invoice is filed. The invoice page here is a finished invoice; the
  filing form is a different route, and its POST has not been observed.
- Whether the conversation feed lists reservations with no messages.
