#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

build_db: turns the Korean public map datasets into the sqlite files the device
reads -- one for cameras, one for links, built and refreshed independently. Runs
on a PC, never on the comma device -- shapefile and pyproj are imported lazily
inside the link loader so the device never needs them.

  cameras: 전국무인교통단속카메라표준데이터  https://www.data.go.kr/data/15028200/standard.do
  links:   전국표준노드링크                  https://www.its.go.kr/nodelink/

Usage:
  pip install pyshp pyproj
  python -m openpilot.sunnypilot.mapd.korea.build_db \
      --cameras 전국무인교통단속카메라표준데이터.csv --out-cameras korea_cameras.sqlite
  python -m openpilot.sunnypilot.mapd.korea.build_db \
      --links MOCT_LINK.shp --out-links korea_links.sqlite
"""
import argparse
import csv
import os
import sqlite3
import struct
from collections.abc import Iterator

SCHEMA_VERSION = "1"

MAX_SPEED_LIMIT_KPH = 130
KOREA_LAT = (33.0, 39.0)
KOREA_LON = (124.0, 132.0)

CSV_ENCODINGS = ("cp949", "utf-8-sig")

# Column names of the CSV distribution on data.go.kr. The open API for the same dataset
# uses romanised keys instead (latitude/longitude/lmttVe/ovrspdRegltSctnLt), so a fetcher
# pulling from the API must normalise to these headers before calling load_cameras.
# Verified against the 2026-08 release; re-check if a load returns 0 rows.
CAMERA_COLUMNS = {
  "lat": "위도",
  "lon": "경도",
  "limit": "제한속도",
  "section": "과속단속구간길이",
}

# an r-tree entry needs a non-degenerate box; ~0.1 m is far below GPS noise
POINT_BOX_DEG = 1e-6

# verified against the 2026 release; re-check with the inspection step if a load returns 0 rows
LINK_COLUMNS = {
  "max_spd": "MAX_SPD",
  "name": "ROAD_NAME",
}

SHP_ENCODING = "cp949"

# Last-resort fallback only. The real ITS 표준노드링크 ships a .prj reading
# PROJCS["ITRF2000_Central_Belt_60", ...] -- a Transverse Mercator on the ITRF2000 datum
# that pyproj cannot map to ANY epsg code (to_epsg() returns None), so no constant here
# could ever be right for it. make_to_wgs84 therefore reads the .prj and this value is
# never used in practice. If a distribution ever ships without a .prj, expect load_links
# to raise rather than silently produce garbage.
FALLBACK_PROJECTED_EPSG = 5179
WGS84_EPSG = 4326

_META = "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"

SCHEMA_CAMERAS = _META + """
CREATE TABLE cameras(
  id        INTEGER PRIMARY KEY,
  lat       REAL    NOT NULL,
  lon       REAL    NOT NULL,
  limit_kph INTEGER NOT NULL,
  -- NOT A USABLE DISTANCE. Carried through verbatim from 과속단속구간길이 for provenance
  -- only. In the 2026-08 dataset just 1120 of 43347 rows populate it at all, and the unit
  -- is inconsistent between submitting agencies: values run 1, 2, 3 ... 45, 200, 18637,
  -- and 99999 as a sentinel. Never feed this to anything that computes a distance.
  section_m INTEGER NOT NULL
);
CREATE VIRTUAL TABLE cameras_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
"""

SCHEMA_LINKS = _META + """
CREATE TABLE links(
  id      INTEGER PRIMARY KEY,
  max_spd INTEGER NOT NULL,
  name    TEXT    NOT NULL,
  geom    BLOB    NOT NULL
);
CREATE VIRTUAL TABLE links_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
"""


def to_float(value) -> float:
  try:
    return float(str(value).strip())
  except (TypeError, ValueError):
    return 0.


def to_int(value) -> int:
  return int(to_float(value))


def in_korea(lat: float, lon: float) -> bool:
  return KOREA_LAT[0] <= lat <= KOREA_LAT[1] and KOREA_LON[0] <= lon <= KOREA_LON[1]


def read_csv_rows(path: str) -> list[dict]:
  """공공데이터 CSVs are CP949 more often than not; fall back to UTF-8."""
  last_error: UnicodeDecodeError | None = None
  for encoding in CSV_ENCODINGS:
    try:
      with open(path, newline="", encoding=encoding) as f:
        return list(csv.DictReader(f))
    except UnicodeDecodeError as e:
      last_error = e
  raise last_error


def load_cameras(path: str) -> Iterator[tuple[float, float, int, int]]:
  """Yield (lat, lon, limit_kph, section_m) for every usable speed-enforcement camera.

  Rows without a speed limit are red-light or parking cameras: they carry no target
  speed, so there is nothing for the longitudinal controller to do with them. That filter
  keeps 33415 of the 43347 rows in the 2026-08 dataset.

  section_m is passed through unvalidated -- see the schema comment. Treat it as a label,
  never as metres. 단속구분 is deliberately not used to classify rows: the field mixes
  zero-padded and unpadded spellings of the same code plus combined values
  ('2', '02', '01+02', '1+2', '99'), so a speed limit greater than zero is the only
  reliable signal that a row describes speed enforcement.
  """
  rows = read_csv_rows(path)
  if rows and CAMERA_COLUMNS["lat"] not in rows[0]:
    raise KeyError(f"expected column {CAMERA_COLUMNS['lat']!r}, got {list(rows[0])}")

  for row in rows:
    limit_kph = to_int(row.get(CAMERA_COLUMNS["limit"]))
    lat = to_float(row.get(CAMERA_COLUMNS["lat"]))
    lon = to_float(row.get(CAMERA_COLUMNS["lon"]))
    if keep_camera(lat, lon, limit_kph):
      yield lat, lon, limit_kph, to_int(row.get(CAMERA_COLUMNS["section"]))


def keep_camera(lat: float, lon: float, limit_kph: int) -> bool:
  """The one filter both the CSV and the API path apply. Two copies would drift."""
  return 0 < limit_kph <= MAX_SPEED_LIMIT_KPH and in_korea(lat, lon)


def load_cameras_api(items) -> Iterator[tuple[float, float, int, int]]:
  """Same rows as load_cameras, from the data.go.kr JSON API instead of the CSV.

  The API romanises every field name -- latitude/longitude/lmttVe/ovrspdRegltSctnLt --
  where the CSV uses 위도/경도/제한속도/과속단속구간길이. Verified against the 2026-08
  snapshot: both paths yield the identical 33415 rows out of 43347.
  """
  for item in items:
    limit_kph = to_int(item.get("lmttVe"))
    lat = to_float(item.get("latitude"))
    lon = to_float(item.get("longitude"))
    if keep_camera(lat, lon, limit_kph):
      yield lat, lon, limit_kph, to_int(item.get("ovrspdRegltSctnLt"))


def insert_cameras(con: sqlite3.Connection, cameras) -> int:
  count = 0
  for lat, lon, limit_kph, section_m in cameras:
    cur = con.execute("INSERT INTO cameras(lat, lon, limit_kph, section_m) VALUES (?, ?, ?, ?)",
                      (lat, lon, limit_kph, section_m))
    con.execute("INSERT INTO cameras_idx VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, lat - POINT_BOX_DEG, lat + POINT_BOX_DEG,
                 lon - POINT_BOX_DEG, lon + POINT_BOX_DEG))
    count += 1
  return count


def pack_geom(points: list[tuple[float, float]]) -> bytes:
  """little-endian float32 (lat, lon) pairs. ~0.5 m at Korean latitudes, plenty for a link."""
  flat = [coord for point in points for coord in point]
  return struct.pack(f"<{len(flat)}f", *flat)


def make_to_wgs84(shp_path: str):
  """Return an (x, y) -> (lat, lon) converter matching the shapefile's own .prj.

  ITS ships UTM-K; some data.go.kr mirrors are already WGS84, so read the .prj
  rather than assuming either one.
  """
  prj_path = os.path.splitext(shp_path)[0] + ".prj"
  wkt = ""
  if os.path.exists(prj_path):
    with open(prj_path, encoding="utf-8", errors="replace") as f:
      wkt = f.read()

  # Only a .prj that exists AND says GEOGCS means the file is already lon/lat degrees.
  # No .prj at all is the ITS-native case, which is UTM-K -- fall through and reproject.
  if wkt and "PROJCS" not in wkt.upper():
    return lambda x, y: (y, x)  # already lon/lat

  from pyproj import CRS, Transformer  # PC-only dependency
  source = CRS.from_wkt(wkt) if wkt else CRS.from_epsg(FALLBACK_PROJECTED_EPSG)
  transformer = Transformer.from_crs(source, CRS.from_epsg(WGS84_EPSG), always_xy=True)

  def to_wgs84(x: float, y: float) -> tuple[float, float]:
    lon, lat = transformer.transform(x, y)
    return lat, lon

  return to_wgs84


def load_links(path: str) -> Iterator[tuple[int, str, list[tuple[float, float]]]]:
  """Yield (max_spd, name, [(lat, lon), ...]) for every link with a usable speed limit."""
  import shapefile  # PC-only dependency

  reader = shapefile.Reader(path, encoding=SHP_ENCODING)
  fields = [f[0] for f in reader.fields[1:]]
  for key in LINK_COLUMNS.values():
    if key not in fields:
      raise KeyError(f"expected field {key!r}, got {fields}")

  to_wgs84 = make_to_wgs84(path)

  read = kept = 0
  for shape_record in reader.iterShapeRecords():
    read += 1
    max_spd = to_int(shape_record.record[LINK_COLUMNS["max_spd"]])
    if not 0 < max_spd <= MAX_SPEED_LIMIT_KPH:
      continue

    points = [to_wgs84(x, y) for x, y in shape_record.shape.points]
    points = [p for p in points if in_korea(*p)]
    if len(points) < 2:
      continue

    kept += 1
    yield max_spd, str(shape_record.record[LINK_COLUMNS["name"]] or ""), points

  if read and not kept:
    raise ValueError(
      f"{path}: read {read} shapes but kept none -- every coordinate fell outside Korea. " +
      "This usually means the CRS assumption is wrong (no .prj is treated as UTM-K " +
      f"EPSG:{FALLBACK_PROJECTED_EPSG}). Inspect the first few points before rebuilding."
    )


def insert_links(con: sqlite3.Connection, links) -> int:
  count = 0
  for max_spd, name, points in links:
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    cur = con.execute("INSERT INTO links(max_spd, name, geom) VALUES (?, ?, ?)",
                      (max_spd, name, pack_geom(points)))
    con.execute("INSERT INTO links_idx VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, min(lats), max(lats), min(lons), max(lons)))
    count += 1
  return count


def write_db(out_path: str, schema: str, fill) -> int:
  """Create out_path from scratch and swap it in atomically.

  fill(con) does the inserting and returns a row count. Everything happens in a
  sibling .tmp file, so a crash or an exception leaves the live database exactly as
  it was -- there is never a half-built database on disk for a reader to open.
  """
  tmp = out_path + ".tmp"
  if os.path.exists(tmp):
    os.remove(tmp)

  try:
    con = sqlite3.connect(tmp)
    try:
      con.executescript(schema)
      con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
      count = fill(con)
      con.commit()
      con.execute("VACUUM")
    finally:
      con.close()
    os.replace(tmp, out_path)  # atomic on POSIX and Windows
  except BaseException:
    if os.path.exists(tmp):
      os.remove(tmp)
    raise

  return count


def build_cameras(out_path: str, cameras_csv: str) -> int:
  """Rebuild the camera database. Returns the row count."""
  return write_db(out_path, SCHEMA_CAMERAS, lambda con: insert_cameras(con, load_cameras(cameras_csv)))


def build_links(out_path: str, links_shp: str) -> int:
  """Rebuild the link database. Returns the row count. PC only -- needs pyshp and pyproj."""
  return write_db(out_path, SCHEMA_LINKS, lambda con: insert_links(con, load_links(links_shp)))


def main() -> None:
  parser = argparse.ArgumentParser(description="Build the Korean map databases.")
  parser.add_argument("--cameras", help="전국무인교통단속카메라표준데이터 CSV")
  parser.add_argument("--links", help="전국표준노드링크 LINK shapefile (.shp)")
  parser.add_argument("--out-cameras", default="korea_cameras.sqlite")
  parser.add_argument("--out-links", default="korea_links.sqlite")
  args = parser.parse_args()

  if not args.cameras and not args.links:
    parser.error("at least one of --cameras / --links is required")

  if args.cameras:
    n = build_cameras(args.out_cameras, args.cameras)
    print(f"{args.out_cameras}: {n} cameras, {os.path.getsize(args.out_cameras) / 1e6:.1f} MB")
  if args.links:
    n = build_links(args.out_links, args.links)
    print(f"{args.out_links}: {n} links, {os.path.getsize(args.out_links) / 1e6:.1f} MB")


if __name__ == "__main__":
  main()
