"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import sqlite3

import pytest

from openpilot.sunnypilot.mapd.korea.build_db import LINK_COLUMNS, SCHEMA_VERSION, SCHEMA_CAMERAS, SCHEMA_LINKS, \
                                                     FALLBACK_PROJECTED_EPSG, WGS84_EPSG, \
                                                     build_cameras, write_db, in_korea, insert_cameras, \
                                                     insert_links, load_cameras, load_links, \
                                                     pack_geom, to_float, to_int

CSV_HEADER = "무인교통단속카메라관리번호,위도,경도,단속구분,제한속도,과속단속구간길이\n"
CSV_ROWS = (
  "A-1,37.4979,127.0276,과속,60,0\n" +      # kept
  "A-2,37.5000,127.0300,구간단속,80,4200\n" + # kept, section
  "A-3,37.5100,127.0400,신호,0,0\n" +        # dropped: no speed limit
  "A-4,0,0,과속,60,0\n" +                    # dropped: outside Korea
  "A-5,37.5200,127.0500,과속,999,0\n" +      # dropped: implausible limit
  "A-6,,,과속,,\n"                            # dropped: empty row
)


def write_csv(tmp_path, encoding="cp949"):
  path = tmp_path / "cameras.csv"
  path.write_text(CSV_HEADER + CSV_ROWS, encoding=encoding)
  return str(path)


def test_to_float_and_to_int_tolerate_junk():
  assert to_float("60") == 60.
  assert to_float(" 60.5 ") == 60.5
  assert to_float("") == 0.
  assert to_float(None) == 0.
  assert to_float("없음") == 0.
  assert to_int("60.9") == 60


def test_in_korea_bounds():
  assert in_korea(37.5, 127.0)
  assert not in_korea(0., 0.)
  assert not in_korea(37.5, 139.7)   # Tokyo
  assert not in_korea(48.9, 127.0)   # too far north


def test_load_cameras_keeps_only_speed_cameras(tmp_path):
  rows = list(load_cameras(write_csv(tmp_path)))
  assert len(rows) == 2, rows
  assert rows[0] == (37.4979, 127.0276, 60, 0)
  assert rows[1] == (37.5000, 127.0300, 80, 4200)


def test_load_cameras_reads_utf8_too(tmp_path):
  rows = list(load_cameras(write_csv(tmp_path, encoding="utf-8-sig")))
  assert len(rows) == 2


def test_build_cameras_writes_only_camera_tables(tmp_path):
  csv_path = tmp_path / "cams.csv"
  csv_path.write_text(CSV_HEADER + CSV_ROWS, encoding="cp949")
  out = str(tmp_path / "korea_cameras.sqlite")

  n = build_cameras(out, str(csv_path))
  assert n == 2

  con = sqlite3.connect(out)
  tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  assert "cameras" in tables
  assert "links" not in tables, tables
  assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == SCHEMA_VERSION
  assert con.execute("SELECT COUNT(*) FROM cameras_idx").fetchone()[0] == 2
  con.close()


def test_build_cameras_rtree_finds_camera_by_bbox(tmp_path):
  out = str(tmp_path / "korea_cameras.sqlite")
  build_cameras(out, write_csv(tmp_path))

  con = sqlite3.connect(out)
  # the r-tree must find the 강남역 camera and nothing at sea off Busan
  hit = con.execute("SELECT COUNT(*) FROM cameras_idx WHERE maxlat>=? AND minlat<=? AND maxlon>=? AND minlon<=?",
                    (37.4970, 37.4990, 127.0270, 127.0285)).fetchone()[0]
  assert hit == 1
  miss = con.execute("SELECT COUNT(*) FROM cameras_idx WHERE maxlat>=? AND minlat<=? AND maxlon>=? AND minlon<=?",
                     (35.0, 35.1, 129.0, 129.1)).fetchone()[0]
  assert miss == 0
  con.close()


def test_build_cameras_is_idempotent(tmp_path):
  out = str(tmp_path / "korea_cameras.sqlite")
  csv_path = write_csv(tmp_path)

  assert build_cameras(out, csv_path) == 2
  assert build_cameras(out, csv_path) == 2  # os.replace over a live file, not a merge

  con = sqlite3.connect(out)
  assert con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 2
  con.close()


def test_build_links_writes_only_link_tables(tmp_path):
  out = str(tmp_path / "korea_links.sqlite")

  def fill(con):
    return insert_links(con, [(60, "테헤란로", [(37.4979, 127.0276), (37.5000, 127.0300)])])

  n = write_db(out, SCHEMA_LINKS, fill)
  assert n == 1

  con = sqlite3.connect(out)
  tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  assert "links" in tables
  assert "cameras" not in tables, tables
  assert con.execute("SELECT COUNT(*) FROM links_idx").fetchone()[0] == 1
  con.close()


def test_insert_links_bbox_matches_geometry(tmp_path):
  out = str(tmp_path / "korea_links.sqlite")
  points = [(37.4979, 127.0276), (37.5000, 127.0300)]

  def fill(con):
    return insert_links(con, [(60, "테헤란로", points)])

  write_db(out, SCHEMA_LINKS, fill)

  con = sqlite3.connect(out)
  lats = [p[0] for p in points]
  lons = [p[1] for p in points]
  minlat, maxlat, minlon, maxlon = con.execute(
    "SELECT minlat, maxlat, minlon, maxlon FROM links_idx").fetchone()
  con.close()

  # sqlite stores r-tree bounds as float32 and rounds OUTWARD by design, so the stored
  # box is always a superset of the true one. That direction is the safe one: the r-tree
  # is a coarse filter and point_segment_distance ranks precisely afterwards, so a box
  # that is slightly too big costs a few extra candidates while one that is slightly too
  # small silently drops the road the car is on.
  assert minlat <= min(lats), (minlat, min(lats))
  assert maxlat >= max(lats), (maxlat, max(lats))
  assert minlon <= min(lons), (minlon, min(lons))
  assert maxlon >= max(lons), (maxlon, max(lons))

  # ...but it must still be tight enough to be a useful filter: float32 at Korean
  # longitudes is good to ~1.5e-5 deg, roughly 1.5 m.
  assert maxlat - minlat < (max(lats) - min(lats)) + 1e-4
  assert maxlon - minlon < (max(lons) - min(lons)) + 1e-4


def test_write_db_leaves_the_old_file_untouched_when_fill_raises(tmp_path):
  out = str(tmp_path / "korea_cameras.sqlite")

  def good(con):
    return insert_cameras(con, [(37.4979, 127.0276, 60, 0)])

  assert write_db(out, SCHEMA_CAMERAS, good) == 1
  before = open(out, "rb").read()

  def boom(con):
    raise RuntimeError("build failed halfway")

  with pytest.raises(RuntimeError):
    write_db(out, SCHEMA_CAMERAS, boom)

  assert open(out, "rb").read() == before, "a failed rebuild must not touch the live database"
  assert not os.path.exists(out + ".tmp"), "the temp file must be cleaned up"


import struct


def test_pack_geom_roundtrips():
  points = [(37.4979, 127.0276), (37.5000, 127.0300)]
  blob = pack_geom(points)
  assert len(blob) == 2 * 8  # two float32 pairs
  flat = struct.unpack(f"<{len(blob) // 4}f", blob)
  # float32 keeps ~0.5 m at Korean latitudes
  assert abs(flat[0] - 37.4979) < 1e-5
  assert abs(flat[1] - 127.0276) < 1e-5
  assert abs(flat[2] - 37.5000) < 1e-5
  assert abs(flat[3] - 127.0300) < 1e-5


def test_pack_geom_empty():
  assert pack_geom([]) == b""


def test_load_links_reprojects_utm_k_when_no_prj(tmp_path):
  """The ITS default: no .prj file alongside the .shp, coordinates are raw UTM-K (EPSG:5179) metres."""
  import shapefile
  from pyproj import CRS, Transformer

  lat, lon = 37.4979, 127.0276
  lat2, lon2 = 37.5000, 127.0300
  to_utm = Transformer.from_crs(CRS.from_epsg(WGS84_EPSG), CRS.from_epsg(FALLBACK_PROJECTED_EPSG), always_xy=True)
  x1, y1 = to_utm.transform(lon, lat)
  x2, y2 = to_utm.transform(lon2, lat2)

  shp_path = str(tmp_path / "link_utm.shp")
  with shapefile.Writer(shp_path, shapeType=shapefile.POLYLINE) as w:
    w.field(LINK_COLUMNS["max_spd"], "N")
    w.field(LINK_COLUMNS["name"], "C", size=80)
    w.line([[(x1, y1), (x2, y2)]])
    w.record(**{LINK_COLUMNS["max_spd"]: 60, LINK_COLUMNS["name"]: "TestRoad"})
  # deliberately no .prj written -- this is the real ITS distribution shape

  links = list(load_links(shp_path))
  assert len(links) == 1
  max_spd, name, points = links[0]
  assert max_spd == 60
  assert abs(points[0][0] - lat) < 1e-4
  assert abs(points[0][1] - lon) < 1e-4
  assert abs(points[1][0] - lat2) < 1e-4
  assert abs(points[1][1] - lon2) < 1e-4


def test_load_links_honors_wgs84_prj(tmp_path):
  """A .prj present and geographic (not projected): coordinates are already degrees, untouched."""
  import shapefile
  from pyproj import CRS

  lat, lon = 37.4979, 127.0276
  lat2, lon2 = 37.5000, 127.0300

  shp_path = str(tmp_path / "link_wgs84.shp")
  with shapefile.Writer(shp_path, shapeType=shapefile.POLYLINE) as w:
    w.field(LINK_COLUMNS["max_spd"], "N")
    w.field(LINK_COLUMNS["name"], "C", size=80)
    w.line([[(lon, lat), (lon2, lat2)]])
    w.record(**{LINK_COLUMNS["max_spd"]: 60, LINK_COLUMNS["name"]: "TestRoad"})
  with open(tmp_path / "link_wgs84.prj", "w", encoding="utf-8") as f:
    f.write(CRS.from_epsg(WGS84_EPSG).to_wkt())

  links = list(load_links(shp_path))
  assert len(links) == 1
  max_spd, name, points = links[0]
  assert max_spd == 60
  assert abs(points[0][0] - lat) < 1e-6
  assert abs(points[0][1] - lon) < 1e-6
  assert abs(points[1][0] - lat2) < 1e-6
  assert abs(points[1][1] - lon2) < 1e-6


def test_load_links_raises_when_every_point_falls_outside_korea(tmp_path):
  """Fix 2: a wrong CRS assumption (or a wrong-region file) must fail loudly, not go silent."""
  import shapefile
  from pyproj import CRS, Transformer

  # Tokyo, correctly reprojected from UTM-K metres -- still outside in_korea's box either way,
  # which is exactly the "every point survived reprojection but none belong" case Fix 2 guards.
  lat, lon = 35.6762, 139.6503
  lat2, lon2 = 35.6800, 139.6600
  to_utm = Transformer.from_crs(CRS.from_epsg(WGS84_EPSG), CRS.from_epsg(FALLBACK_PROJECTED_EPSG), always_xy=True)
  x1, y1 = to_utm.transform(lon, lat)
  x2, y2 = to_utm.transform(lon2, lat2)

  shp_path = str(tmp_path / "link_outside_korea.shp")
  with shapefile.Writer(shp_path, shapeType=shapefile.POLYLINE) as w:
    w.field(LINK_COLUMNS["max_spd"], "N")
    w.field(LINK_COLUMNS["name"], "C", size=80)
    w.line([[(x1, y1), (x2, y2)]])
    w.record(**{LINK_COLUMNS["max_spd"]: 60, LINK_COLUMNS["name"]: "TestRoad"})
  # no .prj -- same ITS-native shape as the UTM-K test above

  with pytest.raises(ValueError, match="CRS"):
    list(load_links(shp_path))
