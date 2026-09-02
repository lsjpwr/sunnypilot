"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import importlib.util
import os
import pathlib
import sqlite3
import struct
import tempfile
import unittest

from openpilot.sunnypilot.mapd.korea.build_db import LINK_COLUMNS, SCHEMA_VERSION, SCHEMA_CAMERAS, SCHEMA_LINKS, \
                                                     FALLBACK_PROJECTED_EPSG, WGS84_EPSG, \
                                                     build_bumps, build_cameras, write_db, in_korea, \
                                                     classify_kind, insert_cameras, \
                                                     insert_links, load_bumps, load_cameras, load_links, \
                                                     pack_geom, to_float, to_int
from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH, BUMP_TRAPEZOID, BUMP_VIRTUAL

# pyshp and pyproj are PC-only build tooling: build_db.py imports them lazily and documents
# them as a manual `pip install`, deliberately keeping them off the device and out of every
# dependency file. So the shapefile tests skip wherever the tooling is not installed rather
# than failing the suite -- which is what they used to do everywhere, CI included.
HAS_SHAPEFILE_TOOLING = all(importlib.util.find_spec(m) is not None for m in ("shapefile", "pyproj"))
_NO_TOOLING_REASON = "needs pyshp and pyproj (PC-only build tooling, see build_db.py)"

CSV_HEADER = "무인교통단속카메라관리번호,위도,경도,단속구분,제한속도,과속단속구간길이\n"
CSV_ROWS = (
  "A-1,37.4979,127.0276,과속,60,0\n" +      # kept
  "A-2,37.5000,127.0300,구간단속,80,4200\n" + # kept, section
  "A-3,37.5100,127.0400,신호,0,0\n" +        # dropped: no speed limit
  "A-4,0,0,과속,60,0\n" +                    # dropped: outside Korea
  "A-5,37.5200,127.0500,과속,999,0\n" +      # dropped: implausible limit
  "A-6,,,과속,,\n"                            # dropped: empty row
)

BUMP_CSV_HEADER = "과속방지턱관리번호,WGS84위도,WGS84경도,과속방지턱형태구분\n"
BUMP_CSV_ROWS = (
  "B-1,37.5000,127.0000,원호형\n" +        # kept, arch
  "B-2,37.5010,127.0010,가상방지턱\n" +     # kept, virtual (stored, filtered at query time)
  "B-3,37.5020,127.0020,사다리꼴형\n" +     # kept, trapezoid
  "B-4,0,0,원호형\n" +                      # dropped: outside Korea
  "B-5,48.8566,2.3522,원호형\n" +           # dropped: Paris
  "B-6,,,사다리꼴형\n"                      # dropped: empty row
)


class BuildDBTestCase(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

  def write_csv(self, encoding="cp949"):
    path = self.tmp_path / "cameras.csv"
    path.write_text(CSV_HEADER + CSV_ROWS, encoding=encoding)
    return str(path)

  def write_bump_csv(self, encoding="cp949"):
    path = self.tmp_path / "bumps.csv"
    path.write_text(BUMP_CSV_HEADER + BUMP_CSV_ROWS, encoding=encoding)
    return str(path)


class TestParsers(unittest.TestCase):
  def test_to_float_and_to_int_tolerate_junk(self):
    self.assertEqual(to_float("60"), 60.)
    self.assertEqual(to_float(" 60.5 "), 60.5)
    self.assertEqual(to_float(""), 0.)
    self.assertEqual(to_float(None), 0.)
    self.assertEqual(to_float("없음"), 0.)
    self.assertEqual(to_int("60.9"), 60)

  def test_in_korea_bounds(self):
    self.assertTrue(in_korea(37.5, 127.0))
    self.assertFalse(in_korea(0., 0.))
    self.assertFalse(in_korea(37.5, 139.7))   # Tokyo
    self.assertFalse(in_korea(48.9, 127.0))   # too far north

  def test_pack_geom_roundtrips(self):
    points = [(37.4979, 127.0276), (37.5000, 127.0300)]
    blob = pack_geom(points)
    self.assertEqual(len(blob), 2 * 8)  # two float32 pairs
    flat = struct.unpack(f"<{len(blob) // 4}f", blob)
    # float32 keeps ~0.5 m at Korean latitudes
    self.assertLess(abs(flat[0] - 37.4979), 1e-5)
    self.assertLess(abs(flat[1] - 127.0276), 1e-5)
    self.assertLess(abs(flat[2] - 37.5000), 1e-5)
    self.assertLess(abs(flat[3] - 127.0300), 1e-5)

  def test_pack_geom_empty(self):
    self.assertEqual(pack_geom([]), b"")


class TestLoadCameras(BuildDBTestCase):
  def test_load_cameras_keeps_only_speed_cameras(self):
    rows = list(load_cameras(self.write_csv()))
    self.assertEqual(len(rows), 2, rows)
    self.assertEqual(rows[0], (37.4979, 127.0276, 60, 0))
    self.assertEqual(rows[1], (37.5000, 127.0300, 80, 4200))

  def test_load_cameras_reads_utf8_too(self):
    rows = list(load_cameras(self.write_csv(encoding="utf-8-sig")))
    self.assertEqual(len(rows), 2)


class TestWriteDB(BuildDBTestCase):
  def test_build_cameras_writes_only_camera_tables(self):
    csv_path = self.tmp_path / "cams.csv"
    csv_path.write_text(CSV_HEADER + CSV_ROWS, encoding="cp949")
    out = str(self.tmp_path / "korea_cameras.sqlite")

    n = build_cameras(out, str(csv_path))
    self.assertEqual(n, 2)

    con = sqlite3.connect(out)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    self.assertIn("cameras", tables)
    self.assertNotIn("links", tables, tables)
    self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], SCHEMA_VERSION)
    self.assertEqual(con.execute("SELECT COUNT(*) FROM cameras_idx").fetchone()[0], 2)
    con.close()

  def test_build_cameras_rtree_finds_camera_by_bbox(self):
    out = str(self.tmp_path / "korea_cameras.sqlite")
    build_cameras(out, self.write_csv())

    con = sqlite3.connect(out)
    # the r-tree must find the 강남역 camera and nothing at sea off Busan
    hit = con.execute("SELECT COUNT(*) FROM cameras_idx WHERE maxlat>=? AND minlat<=? AND maxlon>=? AND minlon<=?",
                      (37.4970, 37.4990, 127.0270, 127.0285)).fetchone()[0]
    self.assertEqual(hit, 1)
    miss = con.execute("SELECT COUNT(*) FROM cameras_idx WHERE maxlat>=? AND minlat<=? AND maxlon>=? AND minlon<=?",
                       (35.0, 35.1, 129.0, 129.1)).fetchone()[0]
    self.assertEqual(miss, 0)
    con.close()

  def test_build_cameras_is_idempotent(self):
    out = str(self.tmp_path / "korea_cameras.sqlite")
    csv_path = self.write_csv()

    self.assertEqual(build_cameras(out, csv_path), 2)
    self.assertEqual(build_cameras(out, csv_path), 2)  # os.replace over a live file, not a merge

    con = sqlite3.connect(out)
    self.assertEqual(con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0], 2)
    con.close()

  def test_build_links_writes_only_link_tables(self):
    out = str(self.tmp_path / "korea_links.sqlite")

    def fill(con):
      return insert_links(con, [(60, "테헤란로", [(37.4979, 127.0276), (37.5000, 127.0300)])])

    n = write_db(out, SCHEMA_LINKS, fill)
    self.assertEqual(n, 1)

    con = sqlite3.connect(out)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    self.assertIn("links", tables)
    self.assertNotIn("cameras", tables, tables)
    self.assertEqual(con.execute("SELECT COUNT(*) FROM links_idx").fetchone()[0], 1)
    con.close()

  def test_insert_links_bbox_matches_geometry(self):
    out = str(self.tmp_path / "korea_links.sqlite")
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
    self.assertLessEqual(minlat, min(lats))
    self.assertGreaterEqual(maxlat, max(lats))
    self.assertLessEqual(minlon, min(lons))
    self.assertGreaterEqual(maxlon, max(lons))

    # ...but it must still be tight enough to be a useful filter: float32 at Korean
    # longitudes is good to ~1.5e-5 deg, roughly 1.5 m.
    self.assertLess(maxlat - minlat, (max(lats) - min(lats)) + 1e-4)
    self.assertLess(maxlon - minlon, (max(lons) - min(lons)) + 1e-4)

  def test_write_db_leaves_the_old_file_untouched_when_fill_raises(self):
    out = str(self.tmp_path / "korea_cameras.sqlite")

    def good(con):
      return insert_cameras(con, [(37.4979, 127.0276, 60, 0)])

    self.assertEqual(write_db(out, SCHEMA_CAMERAS, good), 1)
    with open(out, "rb") as f:
      before = f.read()

    def boom(con):
      raise RuntimeError("build failed halfway")

    with self.assertRaises(RuntimeError):
      write_db(out, SCHEMA_CAMERAS, boom)

    with open(out, "rb") as f:
      self.assertEqual(f.read(), before, "a failed rebuild must not touch the live database")
    self.assertFalse(os.path.exists(out + ".tmp"), "the temp file must be cleaned up")


@unittest.skipUnless(HAS_SHAPEFILE_TOOLING, _NO_TOOLING_REASON)
class TestLoadLinks(BuildDBTestCase):
  def test_load_links_reprojects_utm_k_when_no_prj(self):
    """The ITS default: no .prj file alongside the .shp, coordinates are raw UTM-K (EPSG:5179) metres."""
    import shapefile
    from pyproj import CRS, Transformer

    lat, lon = 37.4979, 127.0276
    lat2, lon2 = 37.5000, 127.0300
    to_utm = Transformer.from_crs(CRS.from_epsg(WGS84_EPSG), CRS.from_epsg(FALLBACK_PROJECTED_EPSG), always_xy=True)
    x1, y1 = to_utm.transform(lon, lat)
    x2, y2 = to_utm.transform(lon2, lat2)

    shp_path = str(self.tmp_path / "link_utm.shp")
    with shapefile.Writer(shp_path, shapeType=shapefile.POLYLINE) as w:
      w.field(LINK_COLUMNS["max_spd"], "N")
      w.field(LINK_COLUMNS["name"], "C", size=80)
      w.line([[(x1, y1), (x2, y2)]])
      w.record(**{LINK_COLUMNS["max_spd"]: 60, LINK_COLUMNS["name"]: "TestRoad"})
    # deliberately no .prj written -- this is the real ITS distribution shape

    links = list(load_links(shp_path))
    self.assertEqual(len(links), 1)
    max_spd, _name, points = links[0]
    self.assertEqual(max_spd, 60)
    self.assertLess(abs(points[0][0] - lat), 1e-4)
    self.assertLess(abs(points[0][1] - lon), 1e-4)
    self.assertLess(abs(points[1][0] - lat2), 1e-4)
    self.assertLess(abs(points[1][1] - lon2), 1e-4)

  def test_load_links_honors_wgs84_prj(self):
    """A .prj present and geographic (not projected): coordinates are already degrees, untouched."""
    import shapefile
    from pyproj import CRS

    lat, lon = 37.4979, 127.0276
    lat2, lon2 = 37.5000, 127.0300

    shp_path = str(self.tmp_path / "link_wgs84.shp")
    with shapefile.Writer(shp_path, shapeType=shapefile.POLYLINE) as w:
      w.field(LINK_COLUMNS["max_spd"], "N")
      w.field(LINK_COLUMNS["name"], "C", size=80)
      w.line([[(lon, lat), (lon2, lat2)]])
      w.record(**{LINK_COLUMNS["max_spd"]: 60, LINK_COLUMNS["name"]: "TestRoad"})
    with open(self.tmp_path / "link_wgs84.prj", "w", encoding="utf-8") as f:
      f.write(CRS.from_epsg(WGS84_EPSG).to_wkt())

    links = list(load_links(shp_path))
    self.assertEqual(len(links), 1)
    max_spd, _name, points = links[0]
    self.assertEqual(max_spd, 60)
    self.assertLess(abs(points[0][0] - lat), 1e-6)
    self.assertLess(abs(points[0][1] - lon), 1e-6)
    self.assertLess(abs(points[1][0] - lat2), 1e-6)
    self.assertLess(abs(points[1][1] - lon2), 1e-6)

  def test_load_links_raises_when_every_point_falls_outside_korea(self):
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

    shp_path = str(self.tmp_path / "link_outside_korea.shp")
    with shapefile.Writer(shp_path, shapeType=shapefile.POLYLINE) as w:
      w.field(LINK_COLUMNS["max_spd"], "N")
      w.field(LINK_COLUMNS["name"], "C", size=80)
      w.line([[(x1, y1), (x2, y2)]])
      w.record(**{LINK_COLUMNS["max_spd"]: 60, LINK_COLUMNS["name"]: "TestRoad"})
    # no .prj -- same ITS-native shape as the UTM-K test above

    with self.assertRaisesRegex(ValueError, "CRS"):
      list(load_links(shp_path))


class TestClassifyKind(unittest.TestCase):
  def test_maps_the_three_shapes(self):
    # the four values the 2026-05-15 release ships: 원호형 117823, 가상형 19532,
    # 기타 2299, 사다리꼴 1486
    self.assertEqual(classify_kind("원호형"), BUMP_ARCH)
    self.assertEqual(classify_kind("사다리꼴"), BUMP_TRAPEZOID)
    self.assertEqual(classify_kind("가상형"), BUMP_VIRTUAL)

  def test_matches_on_substrings_not_equality(self):
    """Agencies spell the same shape several ways across submissions."""
    self.assertEqual(classify_kind("사다리꼴형"), BUMP_TRAPEZOID)
    self.assertEqual(classify_kind(" 사다리꼴식 "), BUMP_TRAPEZOID)
    self.assertEqual(classify_kind("가상방지턱"), BUMP_VIRTUAL)
    self.assertEqual(classify_kind("가상(노면표시)"), BUMP_VIRTUAL)

  def test_unknown_shape_is_treated_as_arch(self):
    """Arch carries the lowest target speed: guessing harsh costs comfort, guessing
    gentle costs the suspension."""
    self.assertEqual(classify_kind(""), BUMP_ARCH)
    self.assertEqual(classify_kind(None), BUMP_ARCH)
    self.assertEqual(classify_kind("기타"), BUMP_ARCH)


class TestLoadBumps(BuildDBTestCase):
  def test_load_bumps_keeps_korean_rows_only(self):
    rows = list(load_bumps(self.write_bump_csv()))
    self.assertEqual(len(rows), 3, rows)
    self.assertEqual(rows[0], (37.5000, 127.0000, BUMP_ARCH))
    self.assertEqual(rows[1], (37.5010, 127.0010, BUMP_VIRTUAL))
    self.assertEqual(rows[2], (37.5020, 127.0020, BUMP_TRAPEZOID))

  def test_load_bumps_reads_utf8_too(self):
    self.assertEqual(len(list(load_bumps(self.write_bump_csv(encoding="utf-8-sig")))), 3)

  def test_load_bumps_raises_on_a_renamed_column(self):
    """A silent zero-row load would ship an empty database that looks like a working one."""
    path = self.tmp_path / "renamed.csv"
    path.write_text("id,lat,lon,shape\nB-1,37.5,127.0,원호형\n", encoding="cp949")
    with self.assertRaises(KeyError):
      list(load_bumps(str(path)))

  def test_build_bumps_writes_a_queryable_database(self):
    out = str(self.tmp_path / "korea_bumps.sqlite")
    self.assertEqual(build_bumps(out, self.write_bump_csv()), 3)

    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    try:
      self.assertEqual(con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                       SCHEMA_VERSION)
      self.assertEqual(con.execute("SELECT COUNT(*) FROM bumps").fetchone()[0], 3)
      # every bump row needs a matching r-tree entry, or next_bump would never see it
      self.assertEqual(con.execute("SELECT COUNT(*) FROM bumps_idx").fetchone()[0], 3)
      kinds = [r[0] for r in con.execute("SELECT kind FROM bumps ORDER BY id")]
      self.assertEqual(kinds, [BUMP_ARCH, BUMP_VIRTUAL, BUMP_TRAPEZOID])
    finally:
      con.close()
