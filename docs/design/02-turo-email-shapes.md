# Turo notification email shapes

Observed by running the probe (`GMAIL_PROBE=true`) against the real mailbox on
2026-10-03. 12 messages matched `from:turo.com newer_than:180d`.

Every value is a type token. This file is the input the email parser is built
and tested against, the same way the sign parser was built against 3,012 real
signs rather than a guess at the format.

## Type 1 — guest message notification

The dominant type by volume, and the useful one: it carries the whole trip
record as a side effect of telling you someone sent a message.

```
from    : Turo <<EMAIL>>
subject : <NAME> has sent you a message about your <NAME>

label   : trip start: <DATE> <TIME>
label   : trip end: <DATE> <TIME>
label   : you earn: <MONEY>
label   : mileage included: <NUM> miles
label   : view <NAME> profile: <URL>
label   : send <NAME> a message: <URL>
label   : notice: <boilerplate about off-platform payment>

line    : <NAME> has sent you a message about your <NAME>.
line    : <PROSE>                  <- the guest's message
line    : Reply <URL>
line    : <NAME> <NUM>             <- vehicle and year, e.g. "Ford Transit 2024"
line    : booked by <NAME>
line    : <NAME>                   <- guest, full name
line    : (<NUM>) <NUM>            <- guest phone
line    : Reservation ID #<NUM>
```

Notes for the parser:

- **`Reservation ID #<NUM>` is the trip key.** It is the only stable
  identifier, and it appears in the body rather than in a header or the subject.
- The subject names the guest and the vehicle but not the reservation, so
  subject matching alone cannot tie a message to a trip.
- `trip start` and `trip end` carry a date and a time but **no year** — it has
  to be inferred, and inferring it wrongly in late December is the obvious bug.
- No timezone marker. These are presumably the vehicle's local time, which for
  this fleet is `America/New_York`, but that is an assumption and should be
  recorded as one rather than hidden.
- The vehicle appears as a free-text `make model year` line, not a plate or a
  VIN, so matching to a fleet vehicle is fuzzy. `Vehicle.bouncie_nickname` and
  `model` are the fields to match against.
- The guest's name appears three times in different forms (subject, "booked by",
  and a bare line), which gives some redundancy to cross-check against.

## Type 2 — payout notification

```
from    : Turo <<EMAIL>>
subject : Your earnings are on the way!

label   : note: <boilerplate about weekly deposits>
line    : <PROSE>
line    : <NAME>-ching! <NAME> sent your earnings payment of <MONEY>.
line    : <PROSE>
```

Carries an amount but **no reservation id**, so a payout cannot be attached to a
trip from this email alone. Useful for the Money module as a total; not useful
for trip sync.

## Not yet observed

No booking-confirmation or trip-start/trip-end reminder email appeared in the
last 180 days of this mailbox, which is surprising. Either they are filtered or
archived elsewhere, they are not sent to this address, or the Gmail query missed
them. Worth re-running the probe with a wider query before assuming the
message-notification email is the only ingress.
