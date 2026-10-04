# Alerts

## What an alert is for

The run sheet answers "where are my cars and what has to move". It only helps
if someone is looking at it. A street-cleaning deadline at 8:30am is exactly
the moment nobody is: the tab is closed, the phone is on a nightstand, and the
first sign of trouble is a ticket on the windscreen.

So alerts are the run sheet reaching out. The rule is that an alert must be
worth the interruption, because the cost of a bad one is not annoyance — it is
that the next one gets ignored too.

## Shape

Three pieces, deliberately separate:

| Module | Question it answers |
| --- | --- |
| `notify/alerts.py` | Should the operator be told about this, now, and in what words? |
| `notify/dispatch.py` | Has this already been said? |
| `notify/webpush.py` | Did the push service accept it? |

The first is a fleet question with a right answer that can be asserted in a
test. The third is plumbing. Keeping them apart means the rules run and log
what they *would* have sent with no channel configured at all — which is how
the timing gets checked against a real fleet before any key exists.

## Stages, not conditions

A deadline stays true for hours. "Is the deadline close" is therefore useless
as a trigger: the poll runs every ten minutes and would fire sixty-six times
before a single cleaning window.

Instead an alert is pinned to a *stage* of the deadline's approach, and each
stage fires once:

- `lead720` — twelve hours out. "Move it tonight, at a civilised hour."
- `lead60` — one hour out. "Go now."
- `overdue` — the deadline has passed and the task is still open.

`ASP_ALERT_LEAD_MINUTES` changes the leads. The earlier default was
`720,120,60,15`, which is four notifications per car per cleaning night and
sixteen across this fleet. Fifteen minutes is also past useful: finding another
legal spot in this neighbourhood takes longer than that.

When several stages are eligible at once — a deploy, a weekend, a service that
slept — the **shortest** one wins. Firing them all sends every missed warning in
the same second, each less true than the last.

### The dedupe key includes the deadline

`task:{id}:{due_by}:{stage}`.

A move task is reused for the life of a parking session, and its `due_by` rolls
forward to the next cleaning window once one passes — same row, new obligation.
Keyed on the task alone, next week's warning is suppressed by this week's
having already gone out, and the car quietly stops being alerted on forever.

### What is not recorded

A send that failed transiently on every subscription is **not** written to
`notification`, so the next poll retries it. Recording it would discard the
alert over a push service having a bad minute, and nothing would ever send it
again. A 404 or 410 is the opposite: permanent, so the subscription row is
deleted rather than counted.

## Why the encryption is hand-rolled

Web push encrypts the payload to the browser's own keys (RFC 8291) and signs
the request with an application server key (RFC 8292). `pywebpush` is the usual
way to get both. It depends on `http-ece`, whose sdist no longer builds against
current setuptools; the alternatives want to replace the system PyJWT.

Rolling encryption by hand is normally the wrong instinct. What makes it
defensible here is that RFC 8291 publishes a complete worked example — keys,
salt, plaintext, and the exact output bytes — so `tests/test_webpush_ece.py`
measures the implementation against the standard rather than against itself. A
round-trip test would pass just as happily on a consistently wrong derivation,
and a consistently wrong derivation produces a notification that silently never
arrives.

## Setting it up

```sh
python -m turonomics_api.cli vapid-keys
```

Set `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT` on the API service. **Do not rotate
the key casually**: a browser's subscription is bound to the public key that
created it, so a new key silently invalidates every subscribed device.

Then open the fleet view and press *Enable alerts*.

### iOS

Safari will not deliver web push to a site opened as a tab. The site has to be
added to the home screen first — Share, then Add to Home Screen — which is why
there is a manifest, an `apple-touch-icon`, and a button that says so rather
than failing silently on an iPhone.

### Why the endpoints are open

`/api/push/subscribe` is unauthenticated unless `PUSH_TOKEN` is set. This is the
opposite default from `/api/sync`, and for a different reason: there, an open
endpoint burns the Bouncie rate limit the fleet depends on. Here, the worst a
stranger can do is register their own browser to receive this fleet's move
reminders — and an alerting system the operator cannot switch on without first
inventing a token is one that never gets switched on.

## WhatsApp

Wanted, and not built yet, because it is not symmetric with push. It needs a
provider (Meta's Cloud API or Twilio), a business account, a verified phone
number not already on consumer WhatsApp, and — the real constraint —
business-initiated messages outside a 24-hour window must use a **template
pre-approved by Meta**. Free-form business-initiated text is not allowed.

The 24-hour window does open by replying, but an alert system that stops
working because nobody replied yesterday is not an alert system.

`Alert` and the dispatcher are already channel-agnostic, so adding it is a
module beside `webpush.py`, not a rewrite.
