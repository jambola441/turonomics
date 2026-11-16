"""The static site is a different origin, so every non-simple request it makes
is preflighted. A method missing from the CORS allow-list fails in the browser
as a bare "Failed to fetch" while the same call works from curl and from tests,
which is how the site's remove buttons broke without anything noticing.
"""

import pytest
from fastapi.testclient import TestClient

SITE = "https://turonomics-site.onrender.com"


def _preflight(client: TestClient, method: str, path: str) -> int:
    response = client.options(
        path,
        headers={
            "Origin": SITE,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization",
        },
    )
    return response.status_code


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # docs/trips/index.html "Remove this rental", and the tolls page's
        # remove-rental button.
        ("DELETE", "/api/trips/00000000-0000-0000-0000-000000000000"),
        # docs/tolls/index.html removing a crossing.
        ("DELETE", "/api/tolls/00000000-0000-0000-0000-000000000000"),
        ("POST", "/api/invoices/00000000-0000-0000-0000-000000000000/recovered"),
        ("GET", "/api/trips"),
    ],
)
def test_site_may_send_the_methods_it_uses(client: TestClient, method: str, path: str) -> None:
    assert _preflight(client, method, path) == 200


def test_other_origins_are_still_refused(client: TestClient) -> None:
    response = client.options(
        "/api/trips/00000000-0000-0000-0000-000000000000",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "DELETE"},
    )
    assert response.status_code == 400
