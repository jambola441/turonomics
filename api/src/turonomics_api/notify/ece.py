"""Encrypt a push message the way RFC 8291 says to.

A browser's push subscription hands over two secrets: a P-256 public key and a
16-byte authentication secret. The push service that will relay the message is
not trusted with its contents, so the body is encrypted to those two values
before it is posted, and only the browser that subscribed can read it.

This is hand-rolled rather than delegated. ``pywebpush`` is the usual answer,
but it depends on ``http-ece``, whose sdist no longer builds against current
setuptools, and the alternatives want to replace the system PyJWT. Rolling
encryption by hand is normally the wrong instinct — the thing that makes it
defensible here is that RFC 8291 publishes a complete worked example, so the
implementation is checked against the specification's own ciphertext rather
than against itself. ``tests/test_webpush_ece.py`` is that check; if it passes,
every derivation step in here matches the RFC byte for byte.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# One record, as RFC 8291 section 4 requires, and the record size the worked
# example uses. A receiver only has to handle a single record this way.
RECORD_SIZE = 4096

# Uncompressed P-256 points: 0x04 followed by two 32-byte coordinates.
POINT_LENGTH = 65


def b64url_decode(value: str) -> bytes:
    """Decode base64url that may have had its padding stripped.

    Browsers send these unpadded and Python refuses them, which is a tedious
    way to find out a subscription was fine all along.
    """
    text = value.strip().replace("\n", "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _hkdf(*, salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def encrypt(
    payload: bytes,
    *,
    ua_public: bytes,
    auth_secret: bytes,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """The body to POST, header and ciphertext in one blob.

    ``as_private`` and ``salt`` exist so the worked example can be reproduced.
    In use both are random per message, which is the point: the sender's key is
    ephemeral, so a message cannot be linked to another by its key.
    """
    sender = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(sender.public_key())
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = sender.exchange(ec.ECDH(), receiver)

    # The two public keys go into the info, in receiver-then-sender order. Swap
    # them and the derivation still produces a key, just not the receiver's.
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hkdf(salt=auth_secret, ikm=ecdh_secret, info=key_info, length=32)

    salt = salt or os.urandom(16)
    cek = _hkdf(salt=salt, ikm=ikm, info=b"Content-Encoding: aes128gcm\x00", length=16)
    nonce = _hkdf(salt=salt, ikm=ikm, info=b"Content-Encoding: nonce\x00", length=12)

    # 0x02 is the padding delimiter for a final record. Without it the browser
    # decrypts the message and then rejects it as malformed, which is a much
    # more confusing failure than not decrypting at all.
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)

    header = (
        salt
        + RECORD_SIZE.to_bytes(4, "big")
        + len(as_public).to_bytes(1, "big")
        + as_public
    )
    return header + ciphertext
