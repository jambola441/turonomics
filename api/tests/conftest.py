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
T001,2024-06-01T09:00:00,2024-06-03T18:00:00,ABC1234
T002,2024-06-05T08:00:00,2024-06-07T20:00:00,XYZ5678
T003,2024-06-10T12:00:00,2024-06-12T10:00:00,LMN9999
""".encode()

EZPASS_CSV_VALID = """\
Date,Time,Location,Debit,Tag,License Plate
06/01/2024,14:00:00,Verrazano Bridge,19.00,E-ZPass-123456,ABC1234
06/02/2024,08:30:00,Goethals Bridge,16.00,E-ZPass-123456,ABC1234
06/06/2024,11:00:00,Queens Midtown Tunnel,9.00,E-ZPass-789012,XYZ5678
06/20/2024,15:00:00,Lincoln Tunnel,17.00,E-ZPass-999999,QQQ0000
""".encode()

ALIASES_JSON = """\
{
  "John Smith": {
    "transponder_ids": ["E-ZPass-123456"],
    "license_plates": ["ABC1234", "XYZ5678"]
  },
  "Jane Doe": {
    "transponder_ids": ["E-ZPass-789012"],
    "license_plates": ["LMN9999"]
  }
}
"""
