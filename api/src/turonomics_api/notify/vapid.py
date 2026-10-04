"""Identify this server to a push service, the way RFC 8292 says to.

A push service will relay a message to a browser without knowing who sent it,
but it wants to know *that* the sender is consistent — so it can rate-limit a
misbehaving application server and contact its operator. VAPID is that: a
signed JWT naming the push service as audience, plus the public key it was
signed with.

The keypair is the application server's identity, not a secret shared with
anyone, and it must not change: a browser's subscription is bound to the public
key that created it, so rotating the key invalidates every subscription. Hence
``VAPID_PRIVATE_KEY`` as configuration rather than something generated at boot.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from turonomics_api.notify.ece import b64url_decode, b64url_encode

# Twelve hours. RFC 8292 caps the lifetime at 24, and a token that lives
# exactly as long as the maximum is a token that is expired by the time a
# clock-skewed push service looks at it.
TOKEN_LIFETIME_SECONDS = 12 * 60 * 60


def generate_private_key() -> str:
    """A new application server key, as base64url for ``VAPID_PRIVATE_KEY``.

    Exposed as a function so ``python -m turonomics_api.cli vapid-keys`` can
    print a pair rather than anyone having to find an online generator and
    paste a private key into it.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    return b64url_encode(raw)


def load_private_key(encoded: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64url_decode(encoded), "big"), ec.SECP256R1())


def public_key_of(private: ec.EllipticCurvePrivateKey) -> str:
    """The base64url the browser needs as ``applicationServerKey``."""
    return b64url_encode(
        private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )


def _segment(data: dict[str, object]) -> str:
    # Separators without spaces: the JWT is signed as bytes, so any difference
    # in serialisation is a different token.
    return b64url_encode(json.dumps(data, separators=(",", ":")).encode())


def authorization(
    endpoint: str, *, private: ec.EllipticCurvePrivateKey, subject: str, now: int | None = None
) -> str:
    """The ``Authorization`` header value for one push endpoint.

    The audience is the push service's *origin* — scheme and host, no path.
    Signing over the full endpoint produces a token the service rejects, and
    the rejection says only "invalid JWT".
    """
    parts = urlsplit(endpoint)
    audience = f"{parts.scheme}://{parts.netloc}"
    issued = int(time.time()) if now is None else now
    header = _segment({"typ": "JWT", "alg": "ES256"})
    claims = _segment(
        {"aud": audience, "exp": issued + TOKEN_LIFETIME_SECONDS, "sub": subject}
    )
    signing_input = f"{header}.{claims}".encode()
    der = private.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # JOSE wants the two integers fixed-width and concatenated; ``cryptography``
    # emits DER, whose length varies with the values. A DER signature here is
    # accepted by nothing and reported as a bad token.
    r, s = decode_dss_signature(der)
    signature = b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    token = f"{header}.{claims}.{signature}"
    return f"vapid t={token},k={public_key_of(private)}"


def verify(header_value: str, *, now: int | None = None) -> dict[str, object]:
    """Check a header this module produced, and return its claims.

    Only the tests use this, and that is the point of it being here: a signature
    this module can verify with the key it advertises is the one property worth
    asserting, and asserting it needs the DER round-trip done correctly in both
    directions.
    """
    scheme, _, rest = header_value.partition(" ")
    if scheme != "vapid":
        raise ValueError(f"not a vapid header: {scheme!r}")
    fields = dict(part.split("=", 1) for part in rest.split(",", 1))
    header, claims, signature = fields["t"].split(".")
    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b64url_decode(fields["k"])
    )
    raw = b64url_decode(signature)
    der = utils.encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )
    public.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    decoded: dict[str, object] = json.loads(b64url_decode(claims))
    expires = decoded.get("exp")
    moment = int(time.time()) if now is None else now
    if not isinstance(expires, int) or expires <= moment:
        raise ValueError("token is expired")
    return decoded
