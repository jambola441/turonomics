"""Shared fixtures for all test modules."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from turonomics_api.db.models import Base
from turonomics_api.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Sample CSVs
# ---------------------------------------------------------------------------

TURO_CSV_VALID = b"""\
trip_id,start_time,end_time,license_plate
T001,2024-06-01T09:00:00,2024-06-03T18:00:00,ABC1234
T002,2024-06-05T08:00:00,2024-06-07T20:00:00,XYZ5678
T003,2024-06-10T12:00:00,2024-06-12T10:00:00,LMN9999
"""

EZPASS_CSV_VALID = b"""\
Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount
"10000001","NY ABC1234","PANYNJ","","LNT","2L","06/01/2024","02:00:00 PM","$-19.00"
"10000002"," 00414500433","PANYNJ","","GWB","2L","06/02/2024","08:30:00 AM","$-16.00"
"10000003"," 00789012000","MTAB&T","","RKB","31","06/06/2024","11:00:00 AM","$-9.00"
"10000004","NY QQQ9999","GSP","","BER","1","06/20/2024","03:00:00 PM","$-2.17"
""," ","","","PAYMENT","","06/15/2024","","$25.00"
"""

ALIASES_JSON = """\
{
  "ABC1234": "00414500433",
  "XYZ5678": "00414500433",
  "LMN9999": "00789012000"
}
"""


# ---------------------------------------------------------------------------
# Database (skipped entirely when no Postgres/PostGIS is reachable)
# ---------------------------------------------------------------------------


TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://postgres:devpass@127.0.0.1:5432/turonomics_test"
    ),
)


def _reachable(url: str) -> bool:
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 3})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _reachable(TEST_URL), reason="no Postgres/PostGIS reachable")


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_URL, future=True)
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = maker()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
