"""Shared fixtures for all test modules."""

import pytest
from fastapi.testclient import TestClient

from turonomics_api.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Sample CSVs
# ---------------------------------------------------------------------------

TURO_CSV_VALID = """\
trip_id,start_time,end_time,license_plate
T001,2024-06-01T09:00:00,2024-06-03T18:00:00,NYABC1234
T002,2024-06-05T08:00:00,2024-06-07T20:00:00,NYXYZ5678
T003,2024-06-10T12:00:00,2024-06-12T10:00:00,NYLMN9999
""".encode()

EZPASS_CSV_VALID = """\
Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount
"10000001","NY ABC1234","PANYNJ","","LNT","2L","06/01/2024","02:00:00 PM","$-19.00"
"10000002"," 00414500433","PANYNJ","","GWB","2L","06/02/2024","08:30:00 AM","$-16.00"
"10000003"," 00789012000","MTAB&T","","RKB","31","06/06/2024","11:00:00 AM","$-9.00"
"10000004","NY QQQ9999","GSP","","BER","1","06/20/2024","03:00:00 PM","$-2.17"
""," ","","","PAYMENT","","06/15/2024","","$25.00"
""".encode()

ALIASES_JSON = """\
{
  "John Smith": {
    "transponder_ids": ["00414500433"],
    "license_plates": ["NYABC1234", "NYXYZ5678"]
  },
  "Jane Doe": {
    "transponder_ids": ["00789012000"],
    "license_plates": ["NYLMN9999"]
  }
}
"""
