"""DATABASE_URL normalisation.

Getting this wrong fails at container boot in production and nowhere else, so
the real formats are pinned here. Render's internal hostname has no port and no
dots, which is the shape most likely to trip a naive parser.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from turonomics_api.db.base import database_url

RENDER_INTERNAL = "postgresql://turonomics_db_user:s3cr3t@dpg-dalk3b942hec73cotuh0-a/turonomics_db"
RENDER_EXTERNAL = (
    "postgresql://turonomics_db_user:s3cr3t"
    "@dpg-dalk3b942hec73cotuh0-a.oregon-postgres.render.com/turonomics_db"
)
HEROKU_STYLE = "postgres://user:pw@host:5432/db"
SUPABASE_POOLER = "postgresql://postgres.abcd:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"


@pytest.mark.parametrize(
    ("raw", "expected_host", "expected_db"),
    [
        (RENDER_INTERNAL, "dpg-dalk3b942hec73cotuh0-a", "turonomics_db"),
        (RENDER_EXTERNAL, "dpg-dalk3b942hec73cotuh0-a.oregon-postgres.render.com", "turonomics_db"),
        (HEROKU_STYLE, "host", "db"),
        (SUPABASE_POOLER, "aws-0-us-east-1.pooler.supabase.com", "postgres"),
    ],
)
def test_real_provider_urls_parse_with_the_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected_host: str, expected_db: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", raw)
    url = make_url(database_url())
    assert url.drivername == "postgresql+psycopg"
    assert url.host == expected_host
    assert url.database == expected_db


def test_an_already_qualified_url_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Double-rewriting would produce postgresql+psycopg+psycopg://."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    assert database_url() == "postgresql+psycopg://u:p@h/db"


def test_a_password_containing_the_scheme_is_not_mangled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the leading scheme is rewritten; generated passwords contain
    anything, and a global replace would corrupt one."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:postgres%3A%2F%2Fx@h/db")
    url = make_url(database_url())
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "postgres://x"
