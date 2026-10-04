"""Find the car a Turo email is about, from the link behind its photo.

The body of every Turo notification names the vehicle as free text — "Toyota
Corolla 2025" — which is not an identifier. This fleet has two Corollas, so
``match_vehicle`` scores shared words, ties, and correctly refuses to guess;
the result was that twenty of every forty messages were dropped.

Turo does carry an unambiguous identifier, in the href behind the car's photo.
A probe of the real mailbox (see docs/design/02-turo-email-shapes.md) found it
on every trip-bearing email type, in one of two shapes:

    https://turo.com/us/en/car-rental/united-states/brooklyn-ny/toyota/corolla/12345678
    https://turo.com/us/en/suv-rental/united-states/brooklyn-ny/toyota/4runner/12345678
    https://turo.com/your-car/12345678

The trailing number is the listing id. It is stable for the life of a listing,
which is what makes it worth storing.

Matched by shape rather than by "the last number in any turo.com link",
because the same emails carry ``/drivers/<id>`` for the guest's profile and
``/reservation/<id>`` for the trip — both numeric, neither a car. Picking the
wrong one would attach every trip to the same imaginary vehicle.
"""

from __future__ import annotations

import html as html_module
import re

# An optional /us/en style locale prefix, then the listing path. The body type
# varies with the car (car-rental, suv-rental, minivan-rental…), and the
# segments between it and the id are country/city/make/model — matched loosely
# because a listing that moves city should not stop resolving.
_LOCALE = r"(?:[a-z]{2}/[a-z]{2}/)?"
_LISTING = re.compile(
    rf"turo\.com/{_LOCALE}[a-z0-9-]+-rental/(?:[^/\s\"'<>]+/){{2,5}}(\d{{4,}})(?![\d])",
    re.IGNORECASE,
)
# The host's own view of the car, which the listing-management emails link to.
_YOUR_CAR = re.compile(rf"turo\.com/{_LOCALE}your-car/(\d{{4,}})(?![\d])", re.IGNORECASE)

_HREF = re.compile(r"""(?is)\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s">]+))""")


def listing_ids(html: str) -> list[str]:
    """Every distinct Turo listing id linked from this markup, in order.

    Plural because an email could link more than one car, and because "exactly
    one" is a property worth asserting rather than assuming — a message that
    points at two listings is not evidence about either.
    """
    if not html:
        return []
    found: list[str] = []
    for match in _HREF.finditer(html):
        href = html_module.unescape(next((g for g in match.groups() if g), ""))
        for pattern in (_LISTING, _YOUR_CAR):
            hit = pattern.search(href)
            if hit:
                found.append(hit.group(1))
                break
    return list(dict.fromkeys(found))


def listing_id(html: str) -> str | None:
    """The listing id this email is about, or ``None`` if it is not unambiguous.

    Returns ``None`` rather than the first of several. The whole point of this
    module is to replace a guess with a fact; an email linking two cars has not
    told us which one it is about, and falling back to the fuzzy match there is
    strictly better than inventing certainty.
    """
    found = listing_ids(html)
    return found[0] if len(found) == 1 else None
