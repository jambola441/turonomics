"""Integration tests for the POST /match endpoint using FastAPI TestClient."""

import io

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ALIASES_JSON, EZPASS_CSV_VALID, TURO_CSV_VALID


def _post_match(
    client: TestClient,
    turo: bytes = TURO_CSV_VALID,
    ezpass: bytes = EZPASS_CSV_VALID,
    aliases: str = ALIASES_JSON,
) -> dict:  # type: ignore[type-arg]
    return client.post(
        "/match",
        files={
            "turo_file": ("turo.csv", io.BytesIO(turo), "text/csv"),
            "ezpass_file": ("ezpass.csv", io.BytesIO(ezpass), "text/csv"),
        },
        data={"aliases": aliases},
    )


class TestHealthEndpoint:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestMatchHappyPath:
    def test_returns_200(self, client: TestClient) -> None:
        resp = _post_match(client)
        assert resp.status_code == 200

    def test_response_structure(self, client: TestClient) -> None:
        body = _post_match(client).json()
        assert "trips" in body
        assert "unmatched_tolls" in body

    def test_correct_trip_count(self, client: TestClient) -> None:
        body = _post_match(client).json()
        assert len(body["trips"]) == 3  # 3 trips in TURO_CSV_VALID

    def test_toll_matched_to_trip(self, client: TestClient) -> None:
        """LNT toll (06/01) should match T001 (06/01–06/03, plate ABC1234)."""
        body = _post_match(client).json()
        t001 = next(t for t in body["trips"] if t["trip_id"] == "T001")
        assert t001["total_toll_amount"] > 0
        assert any(toll["plaza"] == "LNT" for toll in t001["tolls"])

    def test_unknown_plate_toll_unmatched(self, client: TestClient) -> None:
        """Toll with QQQ0000 plate (not in any trip) should be unmatched."""
        body = _post_match(client).json()
        assert len(body["unmatched_tolls"]) >= 1

    def test_no_aliases_accepted(self, client: TestClient) -> None:
        """Empty alias map should work without error."""
        resp = _post_match(client, aliases="{}")
        assert resp.status_code == 200

    def test_aliases_omitted_uses_default(self, client: TestClient) -> None:
        resp = client.post(
            "/match",
            files={
                "turo_file": ("turo.csv", io.BytesIO(TURO_CSV_VALID), "text/csv"),
                "ezpass_file": ("ezpass.csv", io.BytesIO(EZPASS_CSV_VALID), "text/csv"),
            },
        )
        assert resp.status_code == 200


class TestMatchErrorCases:
    def test_invalid_turo_csv_returns_422(self, client: TestClient) -> None:
        bad_csv = b"not,a,valid,csv,for,turo\ngarbage\n"
        resp = _post_match(client, turo=bad_csv)
        assert resp.status_code == 422
        assert "Turo CSV" in resp.json()["detail"]

    def test_invalid_ezpass_csv_returns_422(self, client: TestClient) -> None:
        bad_csv = b"completely,wrong,columns\nfoo,bar,baz\n"
        resp = _post_match(client, ezpass=bad_csv)
        assert resp.status_code == 422
        assert "EZPass CSV" in resp.json()["detail"]

    def test_invalid_aliases_json_returns_422(self, client: TestClient) -> None:
        resp = _post_match(client, aliases="{not valid json}")
        assert resp.status_code == 422
        assert "aliases" in resp.json()["detail"].lower()

    def test_empty_turo_csv_returns_422(self, client: TestClient) -> None:
        resp = _post_match(client, turo=b"")
        assert resp.status_code == 422

    def test_empty_ezpass_csv_returns_422(self, client: TestClient) -> None:
        resp = _post_match(client, ezpass=b"")
        assert resp.status_code == 422

    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_wrong_http_method_returns_405(
        self, client: TestClient, method: str
    ) -> None:
        resp = getattr(client, method)("/match")
        assert resp.status_code == 405


def test_requirements_txt_matches_pyproject_dependencies() -> None:
    """The Dockerfile installs from requirements.txt and then runs
    `pip install --no-deps -e .`, so a dependency added to pyproject.toml but
    not to requirements.txt is simply absent in production. Every test passes
    locally and the container dies on import — so the drift is a test failure,
    not a deploy failure.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    declared = set(tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"])
    pinned = {
        line.strip()
        for line in (root / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert declared == pinned, (
        f"only in pyproject: {sorted(declared - pinned)}; "
        f"only in requirements.txt: {sorted(pinned - declared)}"
    )
