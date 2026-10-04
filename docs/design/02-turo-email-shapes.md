# Turo notification email shapes

Observed by running the probe against the real mailbox, 2026-10-04, with
`from:turo newer_than:365d`. Over 150 messages; 26+ distinct shapes. Every
value below is a type token — no values were read or logged.

Two earlier runs were wrong and are worth remembering, because both failures
looked like findings. `from:turo.com` did not match Turo's sending subdomain
`mail.turo.com`, so every booking email was excluded; and a cap of 12 logged
itself as "12 message(s) match", which read like a total. The conclusion drawn
then — that no booking email existed — was an artefact of both.

## The trip lifecycle is fully covered by email — for reading

Turo emails every state change, so the full lifecycle can be *observed* from
mail alone. That makes email a complete ingress rather than the backstop D2
assumed, and it works with no browser open, which the extension never could.

**It changes nothing about writing.** The extension still holds the only
authenticated Turo session, so every action taken *in reply* to one of these
emails goes through it:

| Email tells you | Acting on it needs the extension |
|---|---|
| A guest sent a message | Replying to them |
| A guest requested a change | Approving or declining it |
| A guest's licence is unconfirmed | Confirming it |
| A trip needs cancelling from your side | Cancelling it |
| Earnings look wrong for a listing | Changing price or availability |

So the two halves are complements, not alternatives: email is how the app
learns there is something to do, and the extension is how the operator does it.
D2's "only write path" stands unchanged. An earlier version of this file said
this finding meant the extension was not needed for trip sync, which was true
only for the read half and misleading as written.

The observable lifecycle:

| Event | Subject template |
|---|---|
| Booked | `<GUEST> trip with your <VEHICLE> is booked!` |
| Change requested | `<GUEST> has requested a change to their trip with your <VEHICLE>` |
| Change confirmed | `<GUEST> confirmed <…> change request with your <VEHICLE>` |
| Changed | `<GUEST> has changed their trip with your <VEHICLE> (<NUM>)` |
| Driver added | `<GUEST> has added another driver to their trip with your <VEHICLE>` |
| Upcoming | `<GUEST> has an upcoming trip with your <VEHICLE>` |
| Ends tomorrow | `<GUEST> ends tomorrow for your <VEHICLE>` |
| Message | `<GUEST> has sent you a message about your <VEHICLE>` |
| Cancelled | `<GUEST> has canceled their trip with your <VEHICLE>` |
| Rated | `<GUEST> just rated their trip` |

Also present, and useful beyond trip sync:

| Event | Subject template | Why it matters |
|---|---|---|
| Licence check | `You still need to confirm your guest's license` | An actionable task before a trip starts |
| Payout | `Your earnings are on the way!` | Money module; carries `<MONEY>` but no reservation id |
| Reimbursement charged | `<GUEST> has been charged for your reimbursement invoice` | Toll and fuel recovery |
| Reimbursement ignored | `<GUEST> has not responded to your reimbursement invoice` | Chase task |
| Relisted / inspection / marketing | various | Noise; filter out |

## The trip-bearing shape

Booking, upcoming, message and cancellation all carry the same block:

```
from    : Turo <<EMAIL>>
subject : <GUEST> trip with your <VEHICLE> is booked!

label   : trip start: <DATE> <TIME>
label   : trip end: <DATE> <TIME>
label   : you earn: <MONEY>
label   : mileage included: <NUM> miles
label   : view <GUEST> profile: <URL>
label   : send <GUEST> a message: <URL>

line    : <GUEST> trip is booked.
line    : Ka-ching! <GUEST> trip with your <VEHICLE> is booked
          from <DATE>, <YEAR>, <TIME> to <DATE>, <YEAR>, <TIME>.
line    : <GUEST> earn <MONEY>.
line    : <VEHICLE> <YEAR>
line    : booked by <GUEST>
line    : <GUEST>
line    : (<NUM>) <NUM>
line    : Reservation ID #<NUM>
```

Notes that shape the parser:

- **`Reservation ID #<NUM>` is the trip key.** It is in the body, not a header
  or the subject, and it is the only stable identifier across the lifecycle —
  so it is what ties a cancellation to the booking it cancels.
- **The year is in the body, not the label.** `trip start:` carries a date and
  time with no year, but the "Ka-ching!" sentence spells out
  `from <DATE>, <YEAR>, <TIME> to <DATE>, <YEAR>, <TIME>`. Parse the sentence
  rather than the label and the year-rollover bug never exists. This corrects
  the earlier note here, which said the year had to be inferred.
- **No timezone marker anywhere.** Presumably the vehicle's local time. For
  this fleet that is `America/New_York`; it is an assumption, and should be
  recorded as one in the parsed record rather than silently applied.
- **The vehicle is free text**, `make model year`, with no plate or VIN, so
  matching to a fleet vehicle is fuzzy. Match on `Vehicle.model` and
  `bouncie_nickname`.
- **Cancellations say `requested by <GUEST>`** where bookings say `booked by`,
  and drop the `you earn` and `mileage included` labels.

## Known weakness in the probe itself

Shapes over-split. "4Runner" starts with a digit, so the capitalised-word mask
misses it, and `…your <NAME> 4Runner` is a different signature from
`…your <NAME>` — the same email type counted twice. It did not hide anything
here because both variants were logged, but the shape count is inflated and a
model name is surviving a mask that is supposed to catch it.
