"""Bouncie API client.

Shapes here come from their published OpenAPI spec and from calling the live
API, not from inference. Two details that a reasonable guess gets wrong:

* The API authorization header is the **raw** access token. Not ``Bearer
  <token>``, despite the token response carrying ``token_type: "Bearer"``.
* There is no ignition event. A parked vehicle is one whose last trip ended.

Token lifecycle: access tokens last an hour, refresh tokens rotate on every
use, and an unused refresh token eventually expires. The authorization code,
unusually, never expires — so a broken refresh chain self-heals by exchanging
the code again rather than needing a human at a browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import OAuthToken

AUTH_BASE = "https://auth.bouncie.com"
API_BASE = "https://api.bouncie.dev"
PROVIDER = "bouncie"

# Refresh a little early rather than racing the expiry.
EXPIRY_SKEW = timedelta(seconds=90)
DEFAULT_REDIRECT_URI = "http://localhost:8000/auth/bouncie/callback"


class BouncieError(RuntimeError):
    """A Bouncie request failed in a way the caller should see."""


@dataclass(frozen=True)
class BouncieConfig:
    client_id: str
    client_secret: str
    auth_code: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> BouncieConfig:
        try:
            return cls(
                client_id=os.environ["BOUNCIE_CLIENT_ID"],
                client_secret=os.environ["BOUNCIE_CLIENT_SECRET"],
                auth_code=os.environ["BOUNCIE_AUTH_CODE"],
                redirect_uri=os.environ.get("BOUNCIE_REDIRECT_URI", DEFAULT_REDIRECT_URI),
            )
        except KeyError as exc:  # pragma: no cover - configuration error
            raise BouncieError(f"missing environment variable: {exc.args[0]}") from exc


def authorize_url(config: BouncieConfig, state: str | None = None) -> str:
    """The URL a human visits once to grant access."""
    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
    }
    if state:
        params["state"] = state
    query = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return f"{AUTH_BASE}/dialog/authorize?{query}"


class BouncieClient:
    def __init__(
        self,
        session: Session,
        config: BouncieConfig | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._session = session
        self._config = config or BouncieConfig.from_env()
        self._http = http or httpx.Client(timeout=30.0)

    # -- tokens ------------------------------------------------------------

    def _store(self, payload: dict[str, Any], *, refreshed: bool) -> OAuthToken:
        row = self._session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
        expires_at = datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
        if row is None:
            row = OAuthToken(provider=PROVIDER, access_token="", refresh_count=0)
            self._session.add(row)
        row.access_token = payload["access_token"]
        # Rotation: the new refresh token replaces the old one, which is now
        # dead. Persisting both halves together is what keeps the chain intact
        # if the process dies immediately after.
        row.refresh_token = payload.get("refresh_token") or row.refresh_token
        row.expires_at = expires_at
        row.obtained_at = datetime.now(UTC)
        if refreshed:
            row.refresh_count += 1
        self._session.commit()
        return row

    def _post_token(self, body: dict[str, str]) -> dict[str, Any]:
        resp = self._http.post(
            f"{AUTH_BASE}/oauth/token",
            json={
                **body,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            },
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise BouncieError(f"token endpoint returned {resp.status_code}: {resp.text[:200]}")
        payload: dict[str, Any] = resp.json()
        if "access_token" not in payload:
            raise BouncieError("token response carried no access_token")
        return payload

    def exchange_auth_code(self) -> OAuthToken:
        return self._store(
            self._post_token(
                {
                    "grant_type": "authorization_code",
                    "code": self._config.auth_code,
                    "redirect_uri": self._config.redirect_uri,
                }
            ),
            refreshed=False,
        )

    def refresh(self, refresh_token: str) -> OAuthToken:
        return self._store(
            self._post_token({"grant_type": "refresh_token", "refresh_token": refresh_token}),
            refreshed=True,
        )

    def access_token(self) -> str:
        """A usable access token, obtained however is necessary."""
        row = self._session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))

        if row is not None and row.expires_at - EXPIRY_SKEW > datetime.now(UTC):
            return row.access_token

        if row is not None and row.refresh_token:
            try:
                return self.refresh(row.refresh_token).access_token
            except BouncieError:
                # Refresh tokens expire if unused, and rotation means a crash
                # mid-refresh can orphan the chain. The authorization code does
                # not expire, so fall back to it rather than paging a human.
                self._session.rollback()

        return self.exchange_auth_code().access_token

    # -- API ---------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._http.get(
            f"{API_BASE}{path}",
            params=params,
            # Raw token. Not "Bearer <token>" — that returns 401.
            headers={"Authorization": self.access_token(), "Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise BouncieError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def vehicles(self) -> list[dict[str, Any]]:
        result = self._get("/v1/vehicles")
        return list(result) if isinstance(result, list) else []

    def trips(self, imei: str, starts_after: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"imei": imei, "gps-format": "geojson"}
        if starts_after:
            params["starts-after"] = starts_after
        result = self._get("/v1/trips", params)
        return list(result) if isinstance(result, list) else []
