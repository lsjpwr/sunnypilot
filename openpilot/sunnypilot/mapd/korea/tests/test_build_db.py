"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import sqlite3

import pytest

from openpilot.sunnypilot.mapd.korea.build_db import LINK_COLUMNS, SCHEMA_VERSION, FALLBACK_PROJECTED_EPSG, WGS84_EPSG, \
                                                     build, in_korea, insert_links, load_cameras, load_links, \
                                                     pack_geom, to_float, to_int

CSV_HEADER = "무인교통단속카메라관리번호,위도,경도,단속구분,제한속도,과속단속구간길이\n"
CSV_ROWS = (
  "A-1,37.4979,127.0276,과속,60,0\n"        # kept
  "A-2,37.5000,127.0300,구간단속,80,4200\n"  # kept, section
  "A-3,37.5100,127.0400,신호,0,0\n"          # dropped: no speed limit
  "A-4,0,0,과속,60,0\n"                      # dropped: outside Korea
  "A-5,37.5200,127.0500,과속,999,0\n"        # dropped: implausible limit
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


def test_build_writes_schema_and_rtree(tmp_path):
  out = str(tmp_path / "korea_map.sqlite")
  n_cameras, n_links = build(out, cameras_csv=write_csv(tmp_path))
  assert (n_cameras, n_links) == (2, 0)

  con = sqlite3.connect(out)
  assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == SCHEMA_VERSION
  assert con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 2
  # the r-tree must find the 강남역 camera and nothing at sea
  hit = con.execute("SELECT COUNT(*) FROM cameras_idx WHERE maxlat>=? AND minlat<=? AND maxlon>=? AND minlon<=?",
                    (37.4970, 37.4990, 127.0270, 127.0285)).fetchone()[0]
  assert hit == 1
  miss = con.execute("SELECT COUNT(*) FROM cameras_idx WHERE maxlat>=? AND minlat<=? AND maxlon>=? AND minlon<=?",
                     (35.0, 35.1, 129.0, 129.1)).fetchone()[0]
  assert miss == 0
  con.close()


def test_build_is_idempotent(tmp_path):
  out = str(tmp_path / "korea_map.sqlite")
  csv_path = write_csv(tmp_path)
  build(out, cameras_csv=csv_path)
  n_cameras, _ = build(out, cameras_csv=csv_path)
  assert n_cameras == 2
  con = sqlite3.connect(out)
  assert con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 2
  con.close()


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


def test_insert_links_fills_bbox(tmp_path):
  out = str(tmp_path / "links.sqlite")
  build(out)  # empty db with the schema
  con = sqlite3.connect(out)
  n = insert_links(con, [(60, "테헤란로", [(37.4979, 127.0276), (37.5000, 127.0300)])])
  con.commit()
  assert n == 1

  row = con.execute("SELECT max_spd, name FROM links").fetchone()
  assert row == (60, "테헤란로")

  minlat, maxlat, minlon, maxlon = con.execute(
    "SELECT minlat, maxlat, minlon, maxlon FROM links_idx").fetchone()
  assert minlat <= 37.4979 and maxlat >= 37.5000
  assert minlon <= 127.0276 and maxlon >= 127.0300
  con.close()


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
