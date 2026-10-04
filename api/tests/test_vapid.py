"""The application server's identity to a push service (RFC 8292).

Every failure mode here produces the same message from a push service — some
variation on "invalid JWT" — so the useful tests are the specific, silent ways
to get it wrong: signing over the wrong audience, and emitting a DER signature
where JOSE wants two fixed-width integers.
"""

from __future__ import annotations

import json
import time

import pytest

from turonomics_api.notify.ece import b64url_decode
from turonomics_api.notify.vapid import (
    TOKEN_LIFETIME_SECONDS,
    authorization,
    generate_private_key,
    load_private_key,
    public_key_of,
    verify,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/fASa1b2C3d4:APA91bHxxxxx?weird=1"


@pytest.fixture()
def private():
    return load_private_key(generate_private_key())


def test_a_header_this_module_wrote_verifies_with_the_key_it_advertises(private):
    claims = verify(authorization(ENDPOINT, private=private, subject="mailto:a@b.com"))
    assert claims["sub"] == "mailto:a@b.com"


def test_the_audience_is_the_origin_not_the_endpoint(private):
    """Signing over the full endpoint produces a token every push service
    rejects, and the rejection says only that the JWT is invalid."""
    claims = verify(authorization(ENDPOINT, private=private, subject="mailto:a@b.com"))
    assert claims["aud"] == "https://fcm.googleapis.com"


def test_the_signature_is_two_fixed_width_integers_not_der(private):
    """``cryptography`` emits DER, whose length varies with the values; JOSE
    wants r and s concatenated at 32 bytes each. A DER signature here is
    accepted by nothing."""
    header = authorization(ENDPOINT, private=private, subject="mailto:a@b.com")
    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert len(b64url_decode(token.split(".")[2])) == 64


def test_the_advertised_key_is_the_one_a_browser_subscribes_with(private):
    """``k=`` must be the uncompressed public point: the browser is given the
    same bytes as ``applicationServerKey``, and a subscription is bound to
    them."""
    header = authorization(ENDPOINT, private=private, subject="mailto:a@b.com")
    advertised = dict(part.split("=", 1) for part in header.split(" ", 1)[1].split(",", 1))["k"]
    assert advertised == public_key_of(private)
    assert len(b64url_decode(advertised)) == 65


def test_the_token_expires_inside_the_24_hour_cap(private):
    """RFC 8292 caps the lifetime at 24 hours, and a token that lives exactly
    that long is expired by the time a clock-skewed service reads it."""
    now = int(time.time())
    header = authorization(ENDPOINT, private=private, subject="mailto:a@b.com", now=now)
    claims = verify(header)
    assert claims["exp"] == now + TOKEN_LIFETIME_SECONDS
    assert TOKEN_LIFETIME_SECONDS < 24 * 60 * 60


def test_an_expired_token_is_rejected(private):
    stale = authorization(
        ENDPOINT, private=private, subject="mailto:a@b.com", now=int(time.time()) - 86_400
    )
    with pytest.raises(ValueError, match="expired"):
        verify(stale)


def test_a_signature_from_another_key_does_not_verify(private):
    """The check has to be a real signature check, not a decode."""
    other = load_private_key(generate_private_key())
    header = authorization(ENDPOINT, private=private, subject="mailto:a@b.com")
    forged = header.replace(f"k={public_key_of(private)}", f"k={public_key_of(other)}")
    with pytest.raises(Exception):  # noqa: B017 - cryptography raises InvalidSignature
        verify(forged)


def test_the_jwt_header_names_es256(private):
    header = authorization(ENDPOINT, private=private, subject="mailto:a@b.com")
    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert json.loads(b64url_decode(token.split(".")[0])) == {"typ": "JWT", "alg": "ES256"}


def test_a_generated_key_is_a_32_byte_scalar():
    assert len(b64url_decode(generate_private_key())) == 32
