#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

build_db: turns the Korean public map datasets into the one sqlite file the device
reads. Runs on a PC, never on the comma device -- shapefile and pyproj are imported
lazily inside the link loader so the device never needs them.

  cameras: 전국무인교통단속카메라표준데이터  https://www.data.go.kr/data/15028200/standard.do
  links:   전국표준노드링크                  https://www.its.go.kr/nodelink/

Usage:
  pip install pyshp pyproj
  python -m openpilot.sunnypilot.mapd.korea.build_db \
      --cameras 전국무인교통단속카메라표준데이터.csv \
      --links MOCT_LINK.shp \
      --out korea_map.sqlite
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

# verified against the 2026 release; re-check with the inspection step if a load returns 0 rows
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
UTM_K_EPSG = 5179  # ITS ships 표준노드링크 in UTM-K
WGS84_EPSG = 4326

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE cameras(
  id        INTEGER PRIMARY KEY,
  lat       REAL    NOT NULL,
  lon       REAL    NOT NULL,
  limit_kph INTEGER NOT NULL,
  section_m INTEGER NOT NULL
);
CREATE VIRTUAL TABLE cameras_idx USING rtree(id, minlat, maxlat, minlon, maxlon);

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
  speed, so there is nothing for the longitudinal controller to do with them.
  """
  rows = read_csv_rows(path)
  if rows and CAMERA_COLUMNS["lat"] not in rows[0]:
    raise KeyError(f"expected column {CAMERA_COLUMNS['lat']!r}, got {list(rows[0])}")

  for row in rows:
    limit_kph = to_int(row.get(CAMERA_COLUMNS["limit"]))
    if not 0 < limit_kph <= MAX_SPEED_LIMIT_KPH:
      continue
    lat = to_float(row.get(CAMERA_COLUMNS["lat"]))
    lon = to_float(row.get(CAMERA_COLUMNS["lon"]))
    if not in_korea(lat, lon):
      continue
    yield lat, lon, limit_kph, to_int(row.get(CAMERA_COLUMNS["section"]))


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
  source = CRS.from_wkt(wkt) if wkt else CRS.from_epsg(UTM_K_EPSG)
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
      f"{path}: read {read} shapes but kept none -- every coordinate fell outside Korea. "
      f"This usually means the CRS assumption is wrong (no .prj is treated as UTM-K "
      f"EPSG:{UTM_K_EPSG}). Inspect the first few points before rebuilding."
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


def build(out_path: str, cameras_csv: str | None = None, links_shp: str | None = None) -> tuple[int, int]:
  """Rebuild out_path from scratch. Returns (camera count, link count)."""
  if os.path.exists(out_path):
    os.remove(out_path)

  con = sqlite3.connect(out_path)
  try:
    con.executescript(SCHEMA)
    con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    n_cameras = insert_cameras(con, load_cameras(cameras_csv)) if cameras_csv else 0
    n_links = insert_links(con, load_links(links_shp)) if links_shp else 0
    con.commit()
    con.execute("VACUUM")
  finally:
    con.close()
  return n_cameras, n_links


def main() -> None:
  parser = argparse.ArgumentParser(description="Build the Korean map database.")
  parser.add_argument("--cameras", help="전국무인교통단속카메라표준데이터 CSV")
  parser.add_argument("--links", help="전국표준노드링크 LINK shapefile (.shp)")
  parser.add_argument("--out", default="korea_map.sqlite")
  args = parser.parse_args()

  if not args.cameras and not args.links:
    parser.error("at least one of --cameras / --links is required")

  n_cameras, n_links = build(args.out, args.cameras, args.links)
  size_mb = os.path.getsize(args.out) / 1e6
  print(f"{args.out}: {n_cameras} cameras, {n_links} links, {size_mb:.1f} MB")


if __name__ == "__main__":
  main()
