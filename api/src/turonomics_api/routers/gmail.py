"""Connect a mailbox without anyone handling a token.

The operator clicks Connect, Google asks them to approve, and the callback
stores the grant. That is the whole flow.

Two things guard it, because the app has no sign-in of its own yet:

* **A signed ``state``.** Google hands back whatever we sent, so a nonce we can
  verify proves the callback belongs to a flow this server started, rather than
  a link someone else constructed. It carries a timestamp and expires.
* **An address allowlist.** The state only proves the flow started here; it
  does not prove *whose* mailbox got approved. Without ``GMAIL_ADDRESS`` set,
  anyone who reached the connect URL could attach their own mailbox and
  overwrite the stored grant. So the callback checks the connected address and
  refuses anything else.

Neither is a substitute for real authentication. They are what is proportionate
until D8's Google sign-in exists, and the allowlist is the one doing the work.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import OAuthToken
from turonomics_api.gmail.client import (
    PROVIDER,
    GmailClient,
    GmailConfig,
    GmailError,
    authorize_url,
)

router = APIRouter(prefix="/api/gmail", tags=["gmail"])
log = logging.getLogger("turonomics.gmail")

DbSession = Annotated[Session, Depends(get_session)]

STATE_TTL_SECONDS = 600


def _state_secret() -> bytes:
    """Keyed on the OAuth client secret, so there is no extra thing to set.

    It never leaves the server and is only used to sign a nonce we hand to
    Google and get back.
    """
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not secret:
        raise HTTPException(503, "Gmail is not configured; set GOOGLE_CLIENT_SECRET")
    return hashlib.sha256(secret.encode()).digest()


def _sign_state() -> str:
    raw = f"{int(time.time())}.{secrets.token_urlsafe(16)}"
    sig = hmac.new(_state_secret(), raw.encode(), hashlib.sha256).digest()
    return f"{raw}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def _verify_state(state: str) -> None:
    try:
        issued_str, nonce, sig = state.split(".", 2)
        raw = f"{issued_str}.{nonce}"
        issued = int(issued_str)
    except ValueError as exc:
        raise HTTPException(400, "malformed state") from exc

    expected = hmac.new(_state_secret(), raw.encode(), hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).decode().rstrip("=")
    if not hmac.compare_digest(sig, expected_b64):
        raise HTTPException(400, "state did not verify")
    if time.time() - issued > STATE_TTL_SECONDS:
        raise HTTPException(400, "state expired; start the connect flow again")


class GmailStatus(BaseModel):
    connected: bool
    address: str | None = None
    detail: str | None = None


@router.get("/connect")
def connect() -> RedirectResponse:
    """Send the operator to Google's consent screen."""
    try:
        config = GmailConfig.from_env()
    except GmailError as exc:
        raise HTTPException(503, str(exc)) from exc
    return RedirectResponse(authorize_url(config, state=_sign_state()), status_code=302)


@router.get("/callback")
def callback(
    session: DbSession,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if error:
        # The operator pressed Cancel, or Google refused. Not a server fault.
        raise HTTPException(400, f"Google returned: {error}")
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    _verify_state(state)

    try:
        config = GmailConfig.from_env()
        client = GmailClient(session, config)
        client.exchange_code(code)
        address = client.address()
    except GmailError as exc:
        raise HTTPException(502, str(exc)) from exc

    if config.address and address.lower() != config.address:
        # Approved, but by the wrong account. Drop the grant rather than keep a
        # mailbox nobody asked for, and do not echo the address back.
        session.execute(delete(OAuthToken).where(OAuthToken.provider == PROVIDER))
        session.commit()
        log.warning("rejected a Gmail grant for an address other than GMAIL_ADDRESS")
        raise HTTPException(403, "that account is not the configured mailbox")

    session.commit()
    log.info("Gmail connected for %s", address)
    return RedirectResponse(os.environ.get("UI_URL", "/") + "?gmail=connected", status_code=302)


@router.get("/status", response_model=GmailStatus)
def status(session: DbSession) -> GmailStatus:
    """Whether the mailbox is still connected.

    Worth an endpoint because a revoked grant is otherwise invisible: mail
    simply stops arriving, which looks exactly like no new mail.
    """
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
    if row is None:
        return GmailStatus(connected=False, detail="never connected")
    try:
        return GmailStatus(connected=True, address=GmailClient(session).address())
    except GmailError as exc:
        return GmailStatus(connected=False, detail=str(exc))
