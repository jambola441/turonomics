# Turo's own API, as the pages use it

Observed 2026-10-05 with the extension's page probe, from a trip page and from
`reservation/<id>/reimbursement/invoice?invoiceId=<id>`. Shapes only — the
probe masks every value, so what follows is field names and value *kinds*.

The point of writing this down: the trip scraper matches on emotion class
names (`p.css-dbccrj-StyledText`) and reads a date range with no year in it.
These endpoints carry the same data typed, with ids, and in epoch millis.

Everything here is fetched with the session the browser already holds, so the
extension can call it directly. None of it needs a key.

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

**`booking.gracePeriodEnd`** is Turo's own grace period, per reservation. The
toll matcher invents one (`TOLL_OVERRUN_GRACE_MINUTES`, two hours) because
email carries no such thing. This is the real boundary, and it is the
difference between billing a guest for an 11pm crossing and eating it.

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

## What this does not answer

- Whether a check-out *time* (as opposed to an odometer reading) is in the
  `+38 more` of reservation detail. The next probe run will say.
- How an invoice is filed. The invoice page here is a finished invoice; the
  filing form is a different route, and its POST has not been observed.
- Whether the conversation feed lists reservations with no messages.
