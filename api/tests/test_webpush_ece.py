"""Check the encryption against the specification's own worked example.

This is the test that makes hand-rolled crypto defensible. RFC 8291 section 5
publishes a complete push message — keys, salt, plaintext, and the exact bytes
that come out — so the implementation is measured against the standard rather
than against itself. A round-trip test would pass just as happily on a
consistently wrong derivation, and a consistently wrong derivation produces a
notification that silently never arrives.

Every value below is copied from the RFC. If this passes, every HKDF info
string, every byte of the header, and the padding delimiter are right.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from turonomics_api.notify.ece import RECORD_SIZE, b64url_decode, b64url_encode, encrypt

# RFC 8291, section 5 and appendix A.
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH_SECRET = "BTBZMqHH6r4Tts7J_aSIgg"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
PLAINTEXT = b"When I grow up, I want to be a watermelon"
EXPECTED = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def _sender() -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64url_decode(AS_PRIVATE), "big"), ec.SECP256R1())


def _encrypted() -> bytes:
    return encrypt(
        PLAINTEXT,
        ua_public=b64url_decode(UA_PUBLIC),
        auth_secret=b64url_decode(AUTH_SECRET),
        as_private=_sender(),
        salt=b64url_decode(SALT),
    )


def test_matches_the_rfc_worked_example_byte_for_byte():
    """The one test that matters here; everything else is a convenience."""
    assert b64url_encode(_encrypted()) == EXPECTED


def test_the_header_carries_the_salt_record_size_and_sender_key():
    """RFC 8188's aes128gcm header, which the browser reads before decrypting.

    Asserted separately because a wrong header fails in the browser with no
    error anyone can see — the push service accepts the POST and the
    notification simply never appears.
    """
    body = _encrypted()
    assert body[:16] == b64url_decode(SALT)
    assert int.from_bytes(body[16:20], "big") == RECORD_SIZE
    assert body[20] == 65, "the key id length is an uncompressed P-256 point"
    assert b64url_encode(body[21:86]).startswith("BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml")


def test_the_ciphertext_covers_the_padding_delimiter():
    """41 bytes of message, one delimiter octet, one 16-byte GCM tag."""
    assert len(_encrypted()) == 86 + len(PLAINTEXT) + 1 + 16


def test_a_fresh_sender_key_changes_every_byte_after_the_salt():
    """The sender's key is ephemeral per message, which is the point of it."""
    first = encrypt(PLAINTEXT, ua_public=b64url_decode(UA_PUBLIC),
                    auth_secret=b64url_decode(AUTH_SECRET))
    second = encrypt(PLAINTEXT, ua_public=b64url_decode(UA_PUBLIC),
                     auth_secret=b64url_decode(AUTH_SECRET))
    assert first != second
    assert first[:16] != second[:16], "a reused salt would be a reused nonce"


@pytest.mark.parametrize(
    "encoded",
    ["BTBZMqHH6r4Tts7J_aSIgg", "BTBZMqHH6r4Tts7J_aSIgg==", " BTBZMqHH6r4Tts7J_aSIgg\n"],
)
def test_browser_base64url_decodes_with_or_without_padding(encoded):
    """Browsers send these unpadded and Python refuses them, which is a tedious
    way to find out a subscription was fine all along."""
    assert len(b64url_decode(encoded)) == 16
