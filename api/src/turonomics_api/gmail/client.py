"""Gmail API client.

Turo has had no public API since April 2023, so its notification emails are the
only ingress that works without a browser open. This reads them.

The operator never handles a token. They click Connect once, Google redirects
back here, and the server keeps the grant from then on.

Two ways this differs from the Bouncie client next door, both of which matter:

* Google refresh tokens do **not** rotate. The same one keeps working, so
  there is no chain to break and no re-exchange to self-heal with.
* There is also no never-expiring authorization code to fall back on. A
  Google authorization code is single-use and dies in minutes. So if the grant
  is ever revoked — by the operator, or by Google expiring it because the
  consent screen is still in Testing — the only recovery is a human clicking
  Connect again. ``status`` exists so that state is visible rather than
  silently stale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import OAuthToken

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1"
PROVIDER = "gmail"

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
EXPIRY_SKEW = timedelta(seconds=90)
DEFAULT_REDIRECT_URI = "https://turonomics.onrender.com/api/gmail/callback"


class GmailError(RuntimeError):
    """A Gmail request failed in a way the caller should see."""


class GmailNotConnected(GmailError):
    """No stored grant. A human has to click Connect."""


@dataclass(frozen=True)
class GmailConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    address: str | None

    @classmethod
    def from_env(cls) -> GmailConfig:
        try:
            return cls(
                client_id=os.environ["GOOGLE_CLIENT_ID"],
                client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                redirect_uri=os.environ.get("GMAIL_REDIRECT_URI", DEFAULT_REDIRECT_URI),
                # Which mailbox may be connected. Without it the callback would
                # accept whoever happened to complete the flow, and the app has
                # no sign-in of its own to lean on yet.
                address=(os.environ.get("GMAIL_ADDRESS") or "").strip().lower() or None,
            )
        except KeyError as exc:  # pragma: no cover - configuration error
            raise GmailError(f"missing environment variable: {exc.args[0]}") from exc


def authorize_url(config: GmailConfig, *, state: str) -> str:
    """Where to send the operator to grant access.

    ``access_type=offline`` with ``prompt=consent`` is what makes Google return
    a refresh token. Without both, a second authorisation returns only an
    access token and the grant silently lasts an hour.
    """
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )


class GmailClient:
    def __init__(
        self,
        session: Session,
        config: GmailConfig | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._session = session
        self._config = config or GmailConfig.from_env()
        self._http = http

    # -- token lifecycle ---------------------------------------------------

    def _client(self) -> httpx.Client:
        return self._http or httpx.Client(timeout=30.0)

    def _store(self, payload: dict[str, Any], *, keep_refresh: str | None = None) -> OAuthToken:
        row = self._session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        expires_at = datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
        # Google omits refresh_token on a plain refresh. Dropping the stored one
        # because this response lacked it would disconnect the mailbox on the
        # first refresh, an hour after connecting.
        refresh = payload.get("refresh_token") or keep_refresh
        if row is None:
            row = OAuthToken(provider=PROVIDER, access_token="", refresh_token=None, expires_at=expires_at)
            self._session.add(row)
        row.access_token = payload["access_token"]
        row.refresh_token = refresh
        row.expires_at = expires_at
        if keep_refresh is not None:
            row.refresh_count += 1
        self._session.flush()
        return row

    def exchange_code(self, code: str) -> OAuthToken:
        """Turn the callback's one-time code into a stored grant."""
        with self._client() as http:
            resp = http.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "redirect_uri": self._config.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if resp.status_code != 200:
            raise GmailError(f"code exchange failed ({resp.status_code}): {resp.text[:200]}")
        payload = resp.json()
        if not payload.get("refresh_token"):
            raise GmailError(
                "Google returned no refresh token. The consent screen is probably still "
                "in Testing, or this account has already granted access — revoke it at "
                "myaccount.google.com/permissions and connect again."
            )
        return self._store(payload)

    def access_token(self) -> str:
        row = self._session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        if row is None:
            raise GmailNotConnected("Gmail is not connected; visit /api/gmail/connect")
        if row.expires_at - EXPIRY_SKEW > datetime.now(UTC):
            return row.access_token
        if not row.refresh_token:
            raise GmailNotConnected("stored Gmail grant has no refresh token; reconnect")

        with self._client() as http:
            resp = http.post(
                TOKEN_URL,
                data={
                    "refresh_token": row.refresh_token,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            # invalid_grant means revoked or expired: no amount of retrying
            # fixes it, and saying so beats a generic failure every ten minutes.
            raise GmailNotConnected(
                f"refresh failed ({resp.status_code}) — reconnect Gmail: {resp.text[:200]}"
            )
        return self._store(resp.json(), keep_refresh=row.refresh_token).access_token

    # -- reading mail ------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._client() as http:
            resp = http.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.access_token()}"},
            )
        if resp.status_code != 200:
            raise GmailError(f"GET {path} failed ({resp.status_code}): {resp.text[:200]}")
        result: dict[str, Any] = resp.json()
        return result

    def address(self) -> str:
        """The connected mailbox. Covered by gmail.readonly, so no extra scope."""
        return str(self._get("/users/me/profile").get("emailAddress", ""))

    def search(self, query: str, *, limit: int = 50) -> list[str]:
        """Message ids matching a Gmail search query, newest first."""
        page = self._get("/users/me/messages", {"q": query, "maxResults": limit})
        return [m["id"] for m in page.get("messages") or []]

    def message(self, message_id: str) -> dict[str, Any]:
        return self._get(f"/users/me/messages/{message_id}", {"format": "full"})
