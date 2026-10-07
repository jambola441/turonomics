"""Where each E-ZPass plaza is, by the code a statement names it with.

Researched from the agencies' own plaza lists and OpenStreetMap's toll gantries
(sources on every row), for the 68 codes on this fleet's statements. Kept as
data in the code rather than a table: it changes when a statement shows a new
code, which is a reviewed edit, not something to write from a request.

A code alone is ambiguous. ``17`` is both the NY Thruway's Newburgh exit and
the NJ Turnpike's Secaucus interchange, and nothing in the code says which —
the statement's Agency column does, so a lookup with an agency matches on it
and one without matches only a code that means one thing.

Confidence is the research's: ``high`` — the facility and its gantry are both
sourced; ``medium`` — the facility is sourced and the point is approximate;
``low`` — the code's meaning was a guess. A low row is used only when the
statement's agency agrees with it, which turns the guess into a match.

Six codes were not identified at all and are not here: TCN, 104, 109, 521,
522, 583. A crossing at one of them is listed on the map, not placed.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

_ROWS = """\
agency,code,name,lat,lon,confidence,zone,source
MTA,RKB,"RFK Bridge, Bronx plaza (Bruckner Expwy)",40.8026,-73.9166,high,,https://www.openstreetmap.org/node/471244278
MTA,RKM,"RFK Bridge, Manhattan plaza (Randalls Island)",40.7991,-73.9251,high,,https://www.openstreetmap.org/node/13703746579
MTA,BWB,Bronx-Whitestone Bridge,40.8134,-73.8365,high,,https://www.openstreetmap.org/node/5621394552
MTA,VNB,Verrazzano-Narrows Bridge (Staten Island),40.6022,-74.0628,high,,https://www.openstreetmap.org/node/5621353068
MTA,TNB,Throgs Neck Bridge,40.8175,-73.8016,high,,https://www.openstreetmap.org/node/471826001
MTA,HHB,Henry Hudson Bridge,40.8758,-73.9257,high,,https://www.openstreetmap.org/node/60915162
MTA,HCT,"Hugh L. Carey Tunnel, Manhattan portal",40.7055,-74.0151,medium,,https://www.openstreetmap.org/node/4149947889
MTA,QMT,"Queens-Midtown Tunnel, Manhattan portal",40.7460,-73.9735,medium,,https://www.openstreetmap.org/node/588166448
MTA,CRZ,Congestion relief zone (Manhattan below 60th St),40.7380,-73.9900,medium,zone,https://congestionreliefzone.mta.info/
PANYNJ,GWU,"George Washington Bridge, upper level (Fort Lee)",40.8537,-73.9631,high,,https://www.openstreetmap.org/node/14119432859
PANYNJ,GWL,"George Washington Bridge, lower level (Fort Lee)",40.8546,-73.9699,high,,https://www.openstreetmap.org/way/197394846
PANYNJ,HT,Holland Tunnel (Jersey City),40.7299,-74.0386,high,,https://www.openstreetmap.org/node/599062332
PANYNJ,LT,Lincoln Tunnel (Weehawken),40.7649,-74.0235,high,,https://www.openstreetmap.org/node/103856544
PANYNJ,GB,Goethals Bridge (Staten Island),40.6288,-74.1841,high,,https://www.openstreetmap.org/node/11751864992
NYSTA,YK,Yonkers toll (I-87),40.9801,-73.8553,high,,https://www.openstreetmap.org/node/11949050628
NYSTA,NR,New Rochelle toll (I-95),40.9292,-73.7663,high,,https://www.openstreetmap.org/node/9137818476
NYSTA,HA,Harriman toll (I-87),41.3123,-74.1248,high,,https://www.openstreetmap.org/node/10379981743
NYSTA,MCB,Gov. Mario M. Cuomo Bridge (Tarrytown),41.0651,-73.8647,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,15,Woodbury toll,41.3091,-74.1252,medium,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,16H,Exit 16 Harriman,41.3107,-74.1227,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,17,Exit 17 Newburgh (I-84),41.5095,-74.0745,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,18,Exit 18 New Paltz,41.7355,-74.0657,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,19,Exit 19 Kingston,41.9476,-74.0275,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,20E,Exit 20 Saugerties,42.0831,-73.9733,medium,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,20W,Exit 20 Saugerties,42.0831,-73.9733,medium,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,21,Exit 21 Catskill,42.2501,-73.8844,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,23,Exit 23 Albany (I-787),42.6329,-73.7814,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,24,Exit 24 Albany (I-90),42.6978,-73.8455,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,25,Exit 25 Schenectady (I-890),42.7540,-73.9336,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,25A,Exit 25A Schenectady (I-88),42.7904,-74.0159,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSTA,27,Exit 27 Amsterdam,42.9230,-74.1965,high,,https://data.ny.gov/api/views/7jkf-259w/rows.csv?accessType=DOWNLOAD
NYSBA,RVW,Rip Van Winkle Bridge,42.2249,-73.8564,high,,https://www.openstreetmap.org/node/212354294
NYSBA,KRB,Kingston-Rhinecliff Bridge,41.9803,-73.9638,high,,https://www.openstreetmap.org/node/9430582967
NYSBA,MHB,Mid-Hudson Bridge,41.7084,-73.9619,high,,https://www.openstreetmap.org/node/58838472
NYSBA,NBB,Newburgh-Beacon Bridge,41.5180,-73.9817,high,,https://www.openstreetmap.org/node/12042842113
NYSBA,BMB,Bear Mountain Bridge,41.3200,-73.9887,high,,https://www.openstreetmap.org/node/3762911787
NJTP,1,NJ Turnpike Interchange 1 (Delaware Memorial Br),39.6858,-75.4479,high,,https://www.openstreetmap.org/node/1884351475
NJTP,3,NJ Turnpike Interchange 3,39.8616,-75.0748,high,,https://www.openstreetmap.org/node/3214118941
NJTP,6,NJ Turnpike Interchange 6 (PA Turnpike),40.1258,-74.7007,high,,https://www.openstreetmap.org/way/42197345
NJTP,13,NJ Turnpike Interchange 13 (Elizabeth),40.6407,-74.2100,high,,https://www.openstreetmap.org/node/254354554
NJTP,13A,NJ Turnpike Interchange 13A (Newark Airport),40.6689,-74.1841,high,,https://www.openstreetmap.org/way/171775318
NJTP,14C,NJ Turnpike Interchange 14C (Jersey City),40.7063,-74.0619,medium,,https://www.openstreetmap.org/way/84830313
NJTP,15E,NJ Turnpike Interchange 15E (Newark),40.7308,-74.1245,medium,,https://www.openstreetmap.org/way/171775504
NJTP,15W,NJ Turnpike Interchange 15W (Kearny),40.7543,-74.1231,high,,https://www.openstreetmap.org/way/58821362
NJTP,17,NJ Turnpike Interchange 17 (Secaucus),40.7823,-74.0526,medium,,https://www.openstreetmap.org/way/58820958
NJTP,18W,NJ Turnpike Interchange 18W (Carlstadt),40.8115,-74.0601,medium,,https://www.openstreetmap.org/node/2018470548
NJTP,19W,NJ Turnpike Interchange 19W (Carlstadt),40.8141,-74.0587,high,,https://www.openstreetmap.org/node/103103629
GSP,BER,Garden State Parkway Bergen toll,40.9084,-74.0975,high,,https://www.openstreetmap.org/way/45439686
GSP,PVK,Garden State Parkway Pascack Valley toll,40.9796,-74.0710,high,,https://www.openstreetmap.org/node/1841475108
GSP,PRN,Garden State Parkway Paramus North,40.9600,-74.0655,high,,https://www.openstreetmap.org/way/173305128
DRJTBC,DWG,Delaware Water Gap Toll Bridge,40.9832,-75.1375,high,,https://www.openstreetmap.org/node/5625185998
DRJTBC,ODW,Delaware Water Gap Toll Bridge (open-road lanes),40.9832,-75.1375,high,,https://www.openstreetmap.org/node/5625185998
DRJTBC,I78,I-78 Toll Bridge,40.6748,-75.2008,high,,https://www.openstreetmap.org/node/1858756598
DRJTBC,O78,I-78 Toll Bridge (open-road lanes),40.6748,-75.2008,high,,https://www.openstreetmap.org/node/1858756598
DRBA,DMB,Delaware Memorial Bridge,39.6963,-75.5454,high,,https://www.openstreetmap.org/way/70949350
DELDOT,D95,"I-95 Delaware Turnpike, Newark",39.6459,-75.7621,low,,https://www.openstreetmap.org/node/6003259281
MDTA,JFK,"I-95 JFK Memorial Highway, Perryville",39.5855,-76.0882,low,,https://www.openstreetmap.org/node/9876571478
MDTA,FMT,Fort McHenry Tunnel,39.2644,-76.5651,low,,https://www.openstreetmap.org/node/11146586023
PTC,DRB,PA Turnpike Delaware River Bridge,40.1211,-74.8448,low,,https://www.openstreetmap.org/node/6276572480
NHDOT,HAM,"Hampton tolls, I-95 NH",42.9625,-70.8563,low,,https://www.openstreetmap.org/node/885709933
NHDOT,BDF,"Bedford tolls, F.E. Everett Tpk NH",42.9150,-71.4652,low,,https://www.openstreetmap.org/way/136954717
MAINE,YRK,Maine Turnpike York toll,43.1802,-70.6487,low,,https://www.openstreetmap.org/node/10074126633
"""


@dataclass(frozen=True)
class Plaza:
    agency: str
    code: str
    name: str
    lat: float
    lon: float
    confidence: str
    # A charge for entering an area rather than passing a gantry; its point is
    # the area's middle, and a route says more.
    zone: bool
    source: str


PLAZAS: tuple[Plaza, ...] = tuple(
    Plaza(
        agency=row["agency"],
        code=row["code"],
        name=row["name"],
        lat=float(row["lat"]),
        lon=float(row["lon"]),
        confidence=row["confidence"],
        zone=row["zone"] == "zone",
        source=row["source"],
    )
    for row in csv.DictReader(io.StringIO(_ROWS))
)

# How a statement's Agency column may name each agency, squeezed to letters
# and digits. Read off the statements as they arrive: one not listed here falls
# back to matching on the code alone, which is safe, only less often useful.
_AGENCY_NAMES: dict[str, str] = {
    "MTABT": "MTA", "MTA": "MTA", "TBTA": "MTA", "CBDTP": "MTA", "MTABRIDGESTUNNELS": "MTA",
    "MTABRIDGESANDTUNNELS": "MTA",
    "PANYNJ": "PANYNJ", "PA": "PANYNJ", "PORTAUTHORITY": "PANYNJ",
    "NYSTA": "NYSTA", "NYSTHRUWAY": "NYSTA", "THRUWAY": "NYSTA",
    "NYSBA": "NYSBA",
    "NJTP": "NJTP", "NJTA": "NJTP", "NJTPK": "NJTP", "NJTURNPIKE": "NJTP",
    "GSP": "GSP", "NJGSP": "GSP", "GARDENSTATEPARKWAY": "GSP",
    "DRJTBC": "DRJTBC", "DRBA": "DRBA",
    "DELDOT": "DELDOT", "DE": "DELDOT",
    "MDTA": "MDTA",
    "PTC": "PTC", "PATURNPIKE": "PTC", "PATPK": "PTC",
    "NHDOT": "NHDOT",
    "MAINETPK": "MAINE", "MAINETURNPIKE": "MAINE", "MTURNPIKE": "MAINE",
}

USABLE = frozenset({"high", "medium"})


def _squeeze(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def locate(code: str, agency: str | None = None) -> Plaza | None:
    """The plaza a statement line names, or None if nothing honest can say.

    With an agency this file recognises, the (agency, code) pair decides —
    and a low-confidence row counts, because the statement has just confirmed
    the agency it guessed. Without one, the code alone, and only when exactly
    one well-sourced plaza carries it.
    """
    wanted = code.strip().upper()
    candidates = [p for p in PLAZAS if p.code == wanted]
    family = _AGENCY_NAMES.get(_squeeze(agency)) if agency else None
    if family is not None:
        matched = [p for p in candidates if p.agency == family]
        return matched[0] if len(matched) == 1 else None
    usable = [p for p in candidates if p.confidence in USABLE]
    return usable[0] if len(usable) == 1 else None
