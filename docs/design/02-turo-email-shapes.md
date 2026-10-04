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

| Email tells you | How to act on it |
|---|---|
| A guest sent a message | **Reply to the email** — it reaches the guest |
| A guest requested a change | Extension: approving or declining it |
| A guest's licence is unconfirmed | Extension: confirming it |
| A trip needs cancelling from your side | Extension: cancelling it |
| Earnings look wrong for a listing | Extension: price or availability |

**Guest messaging is the exception, and it is the one that matters most.**
Replying to a message notification reaches the guest, so the whole
conversation — in and out — runs over mail. That is the feature the operator
named first, and it needs no browser and no extension. Everything else in the
table is an approval or a setting, which is deliberate, occasional, and done at
a keyboard where a browser is open anyway.

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

## Links: the identifier the body does not carry

The body says `Toyota Corolla 2021` and nothing else, which is why matching a
trip to a vehicle is fuzzy — and why two Corollas on the same fleet tie, which
`match_vehicle()` resolves by refusing to guess. Turo does carry an
unambiguous identifier, but it is in a link rather than the text: the car's
photo in the email is wrapped in an anchor whose href points at the vehicle's
own page.

`plain_text()` de-tags the HTML, so every href was discarded before the parser
ever saw one. The probe now reads links from the markup instead and reports them
as `descriptor -> shape`:

```
link    : img[<NAME>] -> https://turo.com/us/en/vehicle-detail/<NUM>
link    : Reply to <NAME> -> https://turo.com/trips/<NUM>/messages
```

The descriptor says what the reader clicks — `img` for the photo — because the
shape alone does not distinguish the vehicle link from the half-dozen others
pointing at the same host.

A URL is the one value the probe does not erase whole. The route is the useful
part; the id in it is not. So the host and lowercase route words stay and
everything else goes: digit runs, hashes, plates, and the base64 payload the
click tracker wraps the real link in — those encode the recipient's own address
often enough that keeping any segment would defeat the point of the module.

### The real format, from a probe run on 2026-10-04

```
link : img[<NAME>] -> https://turo.com/us/en/car-rental/united-states/brooklyn-ny/toyota/corolla/<NUM>
link : img[<NAME>] -> https://turo.com/us/en/suv-rental/united-states/brooklyn-ny/toyota/<ID>/<NUM>
link : extend the snooze -> https://turo.com/your-car/<NUM>
```

The trailing number is the listing id. The body-type segment varies with the
car (`car-rental`, `suv-rental`), and the four segments between it and the id
are country, city, make and model. `4runner` masks to `<ID>` because it starts
with a digit, which is the same quirk that made the capitalised-word mask miss
"4Runner" in subject lines.

**All five trip-bearing email types carry it** — booked, changed, cancelled,
upcoming, and message notifications — so matching on it is a complete fix
rather than a partial one. Earnings, invoices, licence reminders and marketing
do not, and do not need to.

Matched by *shape*, not by "the last number in a turo.com link": the same
emails carry `/drivers/<id>` for the guest's profile and `/reservation/<id>`
for the trip. Both are numeric. Taking the wrong one would attach every trip in
the mailbox to a single imaginary vehicle.

The id is stored on `Vehicle.turo_listing_id`, which existed in the initial
schema and had never been used. A car whose model is unique in the fleet binds
itself on the first email it appears in; two cars of one model cannot, and have
to be bound once by hand:

```sh
python -m turonomics_api.cli set Jerry --turo-listing 12345678
```

The log names the unclaimed listing id when it hits a tie, so there is
something to act on rather than just a complaint.
