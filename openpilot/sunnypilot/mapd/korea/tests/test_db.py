"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_BUMPS, SCHEMA_CAMERAS, SCHEMA_LINKS,
                                                      insert_bumps, insert_cameras, insert_links, write_db)
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, CAMERA_CORRIDOR_M, CAMERA_SECTION,
                                                 CAMERA_SPEED, CAMERA_ZONE, KoreaMapDB, has_camera_kind, verify)

# a 1 km east-west stretch of road at 60 km/h, and a parallel one at 100 km/h 300 m north
ROAD_60 = (60, "테헤란로", [(37.5000, 127.0200), (37.5000, 127.0320)])
ROAD_100 = (100, "고속화도로", [(37.5027, 127.0200), (37.5027, 127.0320)])

# camera 500 m east of the start of ROAD_60, and one 500 m west (behind us)
CAM_AHEAD = (37.5000, 127.0257, 50, 0, CAMERA_SPEED)
CAM_BEHIND = (37.5000, 127.0143, 30, 0, CAMERA_SPEED)
CAM_SECTION = (37.5000, 127.0280, 80, 4200, CAMERA_SECTION)

# ~200 m east on ROAD_60: nearer than CAM_AHEAD (~500 m) on the same road
CAM_ZONE_NEAR = (37.5000, 127.0223, 30, 0, CAMERA_ZONE)

# bumps along ROAD_60, which runs east from (37.5000, 127.0200)
BUMP_AHEAD_150 = (37.5000, 127.0217, 0)   # ~150 m east, 원호형
BUMP_AHEAD_300 = (37.5000, 127.0234, 1)   # ~300 m east, 사다리꼴형
BUMP_BEHIND = (37.5000, 127.0183, 0)      # ~150 m west
BUMP_FAR = (37.5000, 127.0290, 0)         # ~800 m east, past BUMP_MAX_DISTANCE_M and the r-tree box
BUMP_VIRTUAL_AHEAD = (37.5000, 127.0217, 2)

# ~450 m east: inside the ~660 m r-tree search box (BUMP_SEARCH_DEG), but past
# BUMP_MAX_DISTANCE_M (400 m) -- unlike BUMP_FAR above, the r-tree itself will return this
# row, so only the Python-level `distance > BUMP_MAX_DISTANCE_M` check can reject it.
BUMP_WITHIN_BOX_BUT_FAR = (37.5000, 127.0251, 0)

# Same ~150 m ahead-distance as BUMP_AHEAD_150, offset north -- lateral to the eastbound
# heading (90 deg) every test in this file drives with. bearing_delta stays well inside
# BUMP_AHEAD_TOLERANCE (45 deg) for both: ~3.8 deg at 10 m north, ~11.3 deg at 30 m north.
# So these two isolate BUMP_CORRIDOR_M from BUMP_AHEAD_TOLERANCE -- the cone alone would
# admit both.
BUMP_LATERAL_10M = (37.500090, 127.021700, 0)   # ~150 m ahead, ~10 m north: inside the corridor
BUMP_LATERAL_30M = (37.500270, 127.021700, 0)   # ~150 m ahead, ~30 m north: outside the corridor

# A 50 km/h road running east for ~1 km, coincident with a short 80 km/h overpass link
# for its first ~790 m -- an overpass and the road under it, or two links meeting at a
# shared node, both digitised with the same start point. current_link's tie-break and
# hysteresis tests use these.
OVERPASS_LOW = (50, "고가도로", [(37.5100, 127.0500), (37.5100, 127.0620)])
OVERPASS_HIGH = (80, "고가차도", [(37.5100, 127.0500), (37.5100, 127.0510)])

# Two parallel east-west roads ~33 m apart -- far enough to never tie, close enough that
# both are legitimate candidates within LINK_MAX_DISTANCE_M. Proves the tie-break only
# fires on genuinely coincident links, not just "a faster road is somewhere nearby".
FAR_LOW = (30, "이면도로", [(37.5200, 127.0500), (37.5200, 127.0620)])
FAR_HIGH = (100, "고속도로", [(37.5203, 127.0500), (37.5203, 127.0620)])

# Two parallel roads ~24.5 m apart -- 245x TIE_DISTANCE_M, nowhere near tied. Dangerous
# direction: the far road is faster (100) than the one the query point actually sits on
# (30), reproducing the reviewer's finding that a stale sticky link ~25 m away could
# outrank the road we are really on.
STICKY_NEAR = (30, "이면도로", [(37.5300, 127.0500), (37.5300, 127.0620)])
STICKY_FAR = (100, "고속도로", [(37.53022, 127.0500), (37.53022, 127.0620)])  # ~24.5 m north

# os.replace() of a file with an open sqlite3 connection on it is a POSIX guarantee
# (existing readers keep the old inode) that Windows does not provide (PermissionError:
# WinError 5, even for a read-only, idle connection). These tests exercise exactly that
# guarantee, so they run on Linux, which is the device platform.
_REPLACE_WHILE_OPEN_SKIP_REASON = "os.replace over an open sqlite file is a POSIX guarantee Windows lacks; these run on Linux, which is the device platform"


def drop_kind(path):
  """Turn a fresh camera database into one built before cameras carried a kind.

  Not a plain `ALTER TABLE cameras DROP COLUMN kind`: SQLite's DROP COLUMN mislocates the
  column boundary when a comma appears inside the `--` comment immediately above the
  dropped column ("error in table cameras after drop column: incomplete input") -- and
  SCHEMA_CAMERAS's comment right above `kind` has one. Rebuilding the table sidesteps that
  parser bug. `id` values must survive unchanged: cameras_idx (the r-tree) references rows
  by id, and a shifted id would silently break next_camera's join.
  """
  con = sqlite3.connect(path)
  try:
    con.execute("CREATE TABLE cameras_new(id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, " +
                "limit_kph INTEGER NOT NULL, section_m INTEGER NOT NULL)")
    con.execute("INSERT INTO cameras_new SELECT id, lat, lon, limit_kph, section_m FROM cameras")
    con.execute("DROP TABLE cameras")
    con.execute("ALTER TABLE cameras_new RENAME TO cameras")
    con.commit()
  finally:
    con.close()


class KoreaMapDBTestCase(unittest.TestCase):
  """Shared temp dir plus the two fixture database builders."""

  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

  def _make_pair(self):
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD, CAM_BEHIND, CAM_SECTION]))
    write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60, ROAD_100]))
    return cams, links

  def _make_links_db(self, links):
    """Like _make_pair, but with a caller-chosen link set and no cameras."""
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links_path = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, []))
    write_db(links_path, SCHEMA_LINKS, lambda con: insert_links(con, links))
    return cams, links_path

  def open_db(self, cams, links):
    """KoreaMapDB closed at teardown, so the tests do not each need a try/finally."""
    database = KoreaMapDB(cams, links)
    self.addCleanup(database.close)
    return database

  def _make_bumps_db(self, bumps):
    path = str(self.tmp_path / "korea_bumps.sqlite")
    write_db(path, SCHEMA_BUMPS, lambda con: insert_bumps(con, bumps))
    return path

  def open_db_with_bumps(self, bumps, links=(ROAD_60,)):
    """KoreaMapDB over a caller-chosen bump set, closed at teardown."""
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links_path = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD]))
    write_db(links_path, SCHEMA_LINKS, lambda con: insert_links(con, list(links)))
    database = KoreaMapDB(cams, links_path, self._make_bumps_db(bumps))
    self.addCleanup(database.close)
    return database


class TestSchemaAndReload(KoreaMapDBTestCase):
  def test_rejects_wrong_schema_version(self):
    cams, links = self._make_pair()
    con = sqlite3.connect(cams)
    con.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    con.commit()
    con.close()
    with self.assertRaises(ValueError):
      KoreaMapDB(cams, links)

  @unittest.skipIf(sys.platform == "win32", _REPLACE_WHILE_OPEN_SKIP_REASON)
  def test_reload_if_changed_picks_up_a_replaced_camera_db(self):
    cams, links = self._make_pair()
    database = self.open_db(cams, links)
    # heading north (0 deg): all 3 fixture cameras sit north of this point, so heading
    # south (as used later in this test, for the post-reload check) would find nothing
    self.assertIsNotNone(database.next_camera(37.4990, 127.0260, 0.))
    self.assertIs(database.reload_if_changed(), False)  # nothing changed yet

    # swap in a camera database holding a different limit, the way Task 13 will
    os.utime(cams, (0, 0))  # force a distinct mtime on fast filesystems
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.4990, 127.0260, 40, 0, CAMERA_SPEED)]))

    self.assertIs(database.reload_if_changed(), True)
    cam = database.next_camera(37.4995, 127.0260, 180.)
    self.assertIsNotNone(cam)
    self.assertEqual(cam.limit_kph, 40)
    self.assertIs(database.reload_if_changed(), False)  # idempotent

  @unittest.skipIf(sys.platform == "win32", _REPLACE_WHILE_OPEN_SKIP_REASON)
  def test_reload_keeps_the_old_db_when_the_new_one_is_broken(self):
    cams, links = self._make_pair()
    database = self.open_db(cams, links)
    # Corrupt it the way a bad refresh actually would: build a garbage file and
    # os.replace it in. Truncating the live file in place instead would destroy the
    # inode the open connection is reading and no amount of defensive code could
    # survive that -- but Task 13 never does that, it always swaps a new file in.
    bad = cams + ".bad"
    with open(bad, "wb") as f:
      f.write(b"not a database at all")
    os.replace(bad, cams)
    os.utime(cams, (0, 0))
    self.assertIs(database.reload_if_changed(), False)
    # the live connection must still answer from the database it already had open --
    # check the specific camera, not just non-None, so a reload silently swapping in
    # different-but-non-None data would fail this
    cam = database.next_camera(37.4990, 127.0260, 0.)
    self.assertIsNotNone(cam)
    self.assertEqual(cam.limit_kph, 50)


class TestLookups(KoreaMapDBTestCase):
  """The default fixture pair: ROAD_60 / ROAD_100 and the three cameras."""

  def setUp(self):
    super().setUp()
    self.db = self.open_db(*self._make_pair())

  def test_current_link_on_the_road(self):
    link = self.db.current_link(37.5000, 127.0260)
    self.assertIsNotNone(link)
    self.assertEqual(link.max_spd, 60)
    self.assertEqual(link.name, "테헤란로")

  def test_current_link_picks_the_nearer_road(self):
    # 30 m north of ROAD_60 is still much closer to it than to ROAD_100
    link = self.db.current_link(37.5003, 127.0260)
    self.assertIsNotNone(link)
    self.assertEqual(link.max_spd, 60)

    # sitting on ROAD_100
    link = self.db.current_link(37.5027, 127.0260)
    self.assertIsNotNone(link)
    self.assertEqual(link.max_spd, 100)

  def test_current_link_returns_none_when_off_road(self):
    # ~1.5 km north of everything
    self.assertIsNone(self.db.current_link(37.5140, 127.0260))

  def test_current_link_heading_filter_drops_crossing_roads(self):
    # both test roads run east-west; driving due north must not match either
    self.assertIsNone(self.db.current_link(37.5000, 127.0260, heading_deg=0.))
    # driving east matches, and so does driving west (a two-way road may be digitised either way)
    self.assertIsNotNone(self.db.current_link(37.5000, 127.0260, heading_deg=90.))
    self.assertIsNotNone(self.db.current_link(37.5000, 127.0260, heading_deg=270.))

  def test_next_camera_only_looks_ahead(self):
    # heading east from between CAM_BEHIND and CAM_AHEAD
    camera = self.db.next_camera(37.5000, 127.0200, heading_deg=90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 50)
    self.assertTrue(400. < camera.distance_m < 600.)
    self.assertEqual(camera.section_m, 0)

  def test_next_camera_picks_the_nearest_ahead(self):
    # heading east from just before CAM_AHEAD: CAM_SECTION is further away
    camera = self.db.next_camera(37.5000, 127.0250, heading_deg=90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 50)

  def test_next_camera_behind_us_is_ignored(self):
    # heading west from the start: only CAM_BEHIND is in front now
    camera = self.db.next_camera(37.5000, 127.0200, heading_deg=270.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 30)

  def test_next_camera_passes_section_field_through(self):
    # section_m is raw provenance, not a real length (see Camera's docstring) -- this only
    # checks it passes through the field unmodified, not that 4200 means anything as a distance
    camera = self.db.next_camera(37.5000, 127.0270, heading_deg=90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 80)
    self.assertEqual(camera.section_m, 4200)

  def test_next_camera_needs_a_heading(self):
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, heading_deg=None))

  def test_next_camera_none_when_far_away(self):
    self.assertIsNone(self.db.next_camera(37.6000, 127.2000, heading_deg=90.))


class TestNextBump(KoreaMapDBTestCase):
  def test_returns_the_nearest_bump_ahead(self):
    database = self.open_db_with_bumps([BUMP_AHEAD_300, BUMP_AHEAD_150, BUMP_BEHIND])
    bump = database.next_bump(37.5000, 127.0200, 90.)   # heading east
    self.assertIsNotNone(bump)
    self.assertAlmostEqual(bump.lon, BUMP_AHEAD_150[1], places=6)
    self.assertEqual(bump.kind, BUMP_ARCH)
    self.assertLess(bump.distance_m, 200.)

  def test_carries_the_shape_through(self):
    database = self.open_db_with_bumps([BUMP_AHEAD_300])
    bump = database.next_bump(37.5000, 127.0200, 90.)
    self.assertIsNotNone(bump)
    self.assertEqual(bump.kind, BUMP_TRAPEZOID)

  def test_ignores_bumps_behind_us(self):
    database = self.open_db_with_bumps([BUMP_BEHIND])
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_ignores_bumps_past_the_horizon(self):
    database = self.open_db_with_bumps([BUMP_FAR])
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_ignores_a_bump_inside_the_rtree_box_but_past_bump_max_distance(self):
    """BUMP_FAR above sits outside the r-tree search box, so it never independently
    exercises the Python-level `distance > BUMP_MAX_DISTANCE_M` check -- the r-tree already
    dropped it. This one is inside the box (~450 m < ~660 m) so only that check can reject it."""
    database = self.open_db_with_bumps([BUMP_WITHIN_BOX_BUT_FAR])
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_ignores_virtual_bumps(self):
    """Paint on the road. Stored so including it later is a query change, never returned."""
    database = self.open_db_with_bumps([BUMP_VIRTUAL_AHEAD])
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_needs_a_heading(self):
    database = self.open_db_with_bumps([BUMP_AHEAD_150])
    self.assertIsNone(database.next_bump(37.5000, 127.0200, None))

  def test_a_missing_bump_database_is_not_a_failure(self):
    """Devices deployed before this feature have no korea_bumps.sqlite. Cameras and links
    must keep working, and next_bump must answer None rather than raise."""
    cams, links = self._make_pair()
    database = KoreaMapDB(cams, links, str(self.tmp_path / "does_not_exist.sqlite"))
    self.addCleanup(database.close)
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))
    self.assertIsNotNone(database.next_camera(37.5000, 127.0200, 90.))
    self.assertIsNotNone(database.current_link(37.5000, 127.0200))

  def test_no_bumps_path_at_all_is_not_a_failure(self):
    cams, links = self._make_pair()
    database = self.open_db(cams, links)
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_a_corrupt_bump_database_is_ignored_not_fatal(self):
    """Cameras and links must keep working even if the bump file exists but will not open
    (an scp interrupted mid-copy, or built against a future SCHEMA_VERSION) -- losing speed
    limits over a database this feature calls optional would be the wrong trade."""
    cams, links = self._make_pair()
    bumps = str(self.tmp_path / "korea_bumps.sqlite")
    with open(bumps, "wb") as f:
      f.write(b"not a database")

    database = KoreaMapDB(cams, links, bumps)
    self.addCleanup(database.close)
    self.assertIsNone(database.bmp)
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))
    self.assertIsNotNone(database.next_camera(37.5000, 127.0200, 90.))
    self.assertIsNotNone(database.current_link(37.5000, 127.0200))

  def test_bump_directly_ahead_is_not_rejected_by_the_corridor(self):
    database = self.open_db_with_bumps([BUMP_AHEAD_150])
    self.assertIsNotNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_bump_inside_the_corridor_is_returned(self):
    database = self.open_db_with_bumps([BUMP_LATERAL_10M])
    self.assertIsNotNone(database.next_bump(37.5000, 127.0200, 90.))

  def test_bump_outside_the_corridor_is_rejected_even_though_the_cone_would_admit_it(self):
    """~30 m north at ~150 m ahead is bearing_delta ~11 deg -- well inside
    BUMP_AHEAD_TOLERANCE (45 deg) -- so only BUMP_CORRIDOR_M can reject this one. This is
    the regression the whole-branch review found: without the corridor, a cone this wide
    also returns bumps on a parallel side street."""
    database = self.open_db_with_bumps([BUMP_LATERAL_30M])
    self.assertIsNone(database.next_bump(37.5000, 127.0200, 90.))


class TestTieBreakAndHysteresis(KoreaMapDBTestCase):
  def test_current_link_prefers_the_higher_limit_when_tied(self):
    database = self.open_db(*self._make_links_db([OVERPASS_LOW, OVERPASS_HIGH]))
    # exactly on the shared start point: both links are 0 m away, a dead tie
    link = database.current_link(37.5100, 127.0500)
    self.assertIsNotNone(link)
    self.assertEqual(link.max_spd, 80)

  def test_current_link_holds_the_match_then_moves_on(self):
    database = self.open_db(*self._make_links_db([OVERPASS_LOW, OVERPASS_HIGH]))
    first = database.current_link(37.5100, 127.0500)
    second = database.current_link(37.5100, 127.0500)
    self.assertIsNotNone(first)
    self.assertEqual(first.max_spd, 80)
    self.assertIsNotNone(second)
    self.assertEqual(second.max_spd, 80)

    # past the overpass (~790 m on): OVERPASS_HIGH no longer covers this point at all, so
    # stickiness releases on its own and OVERPASS_LOW is the only remaining candidate
    later = database.current_link(37.5100, 127.0600)
    self.assertIsNotNone(later)
    self.assertEqual(later.max_spd, 50)

  def test_current_link_tie_break_does_not_apply_across_separate_roads(self):
    database = self.open_db(*self._make_links_db([FAR_LOW, FAR_HIGH]))
    # sitting on FAR_LOW; FAR_HIGH is a real candidate ~33 m away but nowhere near tied
    link = database.current_link(37.5200, 127.0510)
    self.assertIsNotNone(link)
    self.assertEqual(link.max_spd, 30)

  def test_current_link_tied_pair_returns_the_higher_limit_on_repeated_calls(self):
    # NOTE: this alone does not prove hysteresis is doing anything -- max(tied, ...) picks
    # 80 here every time regardless of the sticky branch, since 80 is genuinely the higher
    # limit of the tied pair. It only pins that repeated calls stay consistent. The sticky
    # branch itself is isolated by test_current_link_sticky_link_wins_over_the_tie_break_when_tied.
    database = self.open_db(*self._make_links_db([OVERPASS_LOW, OVERPASS_HIGH]))
    first = database.current_link(37.5100, 127.0500)
    second = database.current_link(37.5100, 127.0500)
    self.assertIsNotNone(first)
    self.assertIsNotNone(second)
    self.assertEqual(first.max_spd, 80)
    self.assertEqual(second.max_spd, 80)

  def test_current_link_sticky_link_wins_over_the_tie_break_when_tied(self):
    """Isolates hysteresis: without the sticky branch the tie-break would pick the other one."""
    cams, links = self._make_links_db([OVERPASS_LOW, OVERPASS_HIGH])
    database = self.open_db(cams, links)
    # Both overpass links are coincident, so the tie-break alone always yields the higher
    # limit (80) -- confirm that baseline first.
    on_top = database.current_link(37.5100, 127.0500)
    self.assertIsNotNone(on_top)
    self.assertEqual(on_top.max_spd, 80, on_top)

    # Pin the previous match to the LOWER link by its real rowid (looked up, not
    # hardcoded, so this does not break if insertion order ever changes). max_spd = 50
    # is unambiguous: OVERPASS_HIGH is the only other row and it is 80.
    con = sqlite3.connect(links)
    low_id = con.execute("SELECT id FROM links WHERE max_spd = 50").fetchone()[0]
    con.close()
    database._last_link_id = low_id

    # Only the sticky branch can make the lower link come back here.
    held = database.current_link(37.5100, 127.0500)
    self.assertIsNotNone(held)
    self.assertEqual(held.max_spd, 50, held)

    # and it keeps holding it
    self.assertEqual(database.current_link(37.5100, 127.0500).max_spd, 50)

  def test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road(self):
    # Reviewer-reported regression: a sticky link ~24.5 m away (245x TIE_DISTANCE_M, nowhere
    # near tied) must never outrank the road the query point is actually sitting on.
    # Dangerous direction: the stale/far link carries the HIGHER limit, so a bug here would
    # silently permit 100 km/h on a road signed for 30. Checked both insertion orders --
    # the old bug outcome depended on the order sqlite happened to return rows in.
    for order in ([STICKY_FAR, STICKY_NEAR], [STICKY_NEAR, STICKY_FAR]):
      with self.subTest(first_inserted=order[0][0]):
        database = self.open_db(*self._make_links_db(order))
        # sit on the far link first, so hysteresis latches onto it for real, the way it
        # would after actually driving that road
        first = database.current_link(37.53022, 127.0500)
        self.assertIsNotNone(first)
        self.assertEqual(first.max_spd, 100)

        # now the query point is on the near, slower road -- the far link is still within
        # LINK_MAX_DISTANCE_M but 24.5 m away is not a tie, so it must not win
        second = database.current_link(37.5300, 127.0500)
        self.assertIsNotNone(second)
        self.assertEqual(second.max_spd, 30)


class TestVerify(KoreaMapDBTestCase):
  """verify() moved here from deploy.py so the on-device downloader can use it without
  importing PC-only build tooling. Same contract, same failure modes."""

  def test_verify_returns_the_row_count(self):
    cameras, links = self._make_pair()
    # _make_pair's cameras file always holds CAM_AHEAD, CAM_BEHIND, CAM_SECTION -- 3 rows
    self.assertEqual(verify(cameras, "cameras", 1), 3)

  def test_verify_rejects_a_file_that_is_not_a_database(self):
    path = self.tmp_path / "garbage.sqlite"
    path.write_bytes(b"not a database")
    with self.assertRaises(sqlite3.DatabaseError):
      verify(str(path), "cameras", 1)

  def test_verify_rejects_a_short_table(self):
    cameras, links = self._make_pair()
    with self.assertRaisesRegex(ValueError, "expected at least"):
      verify(cameras, "cameras", 999999)


class TestReloadAllThree(KoreaMapDBTestCase):
  """reload_if_changed used to watch only the camera file. The downloader replaces the
  link and bump files too, and a process that keeps reading the old inode never sees
  them."""

  @staticmethod
  def _touch_forward(path):
    """Push mtime a second into the future so the change is unambiguous on any filesystem."""
    stamp = os.path.getmtime(path) + 1
    os.utime(path, (stamp, stamp))

  @unittest.skipIf(sys.platform == "win32", _REPLACE_WHILE_OPEN_SKIP_REASON)
  def test_a_replaced_link_database_is_picked_up(self):
    cams, links = self._make_pair()
    database = self.open_db(cams, links)
    self.assertEqual(database.current_link(37.5000, 127.0260).max_spd, 60)

    # same path, same geometry, different speed limit
    write_db(links, SCHEMA_LINKS,
             lambda con: insert_links(con, [(30, "테헤란로", ROAD_60[2])]))
    self._touch_forward(links)

    self.assertTrue(database.reload_if_changed())
    self.assertEqual(database.current_link(37.5000, 127.0260).max_spd, 30)

  def test_a_link_swap_clears_the_sticky_link(self):
    """The builder assigns ids by insertion order, so the same id names a different road
    after a rebuild. A surviving _last_link_id would pick that road inside the tie band."""
    cams, links = self._make_pair()
    database = self.open_db(cams, links)
    database.current_link(37.5000, 127.0260)
    self.assertIsNotNone(database._last_link_id)

    self._touch_forward(links)
    database.reload_if_changed()

    self.assertIsNone(database._last_link_id, "a stale link id survived the swap")

  def test_a_bump_database_that_appears_later_is_picked_up(self):
    """A device with auto-download on starts with no bump file at all. Waiting for a
    reboot to use the one it just downloaded would be a poor trade."""
    cams, links = self._make_pair()
    # _make_bumps_db always writes this exact path, so name it before the file exists
    bumps = str(self.tmp_path / "korea_bumps.sqlite")
    database = KoreaMapDB(cams, links, bumps)
    self.addCleanup(database.close)
    self.assertIsNone(database.bmp)

    self._make_bumps_db([BUMP_AHEAD_150])

    self.assertTrue(database.reload_if_changed())
    self.assertIsNotNone(database.bmp)

  def test_an_unchanged_set_of_files_reports_no_reload(self):
    cams, links = self._make_pair()
    database = self.open_db(cams, links)
    database.reload_if_changed()
    self.assertFalse(database.reload_if_changed())


# ROAD_60 runs east from (37.5000, 127.0200). CAM_AHEAD sits on it 500 m east.
# A camera 33 m north of CAM_AHEAD is a parallel road: inside the 60 deg cone that
# next_camera has always used, outside the 30 m route corridor.
CAM_SIDE_ROAD = (37.5003, 127.0257, 30, 0, CAMERA_SPEED)


class TestRouteCorridorCameras(KoreaMapDBTestCase):
  def setUp(self):
    super().setUp()
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_SIDE_ROAD]))
    write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60]))
    self.db = self.open_db(cams, links)
    self.route = [(37.5000, 127.0200), (37.5000, 127.0320)]

  def test_without_a_route_the_side_road_camera_is_accepted(self):
    """Today's behaviour, and the reason this task exists."""
    camera = self.db.next_camera(37.5000, 127.0200, 90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 30)

  def test_the_route_rejects_the_side_road_camera(self):
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., route=self.route))

  def test_a_camera_on_the_route_survives(self):
    # A distinct path, not a rewrite of self.db's open camera file: os.replace onto a
    # path with a live sqlite3 reader is a POSIX guarantee Windows does not provide (see
    # _REPLACE_WHILE_OPEN_SKIP_REASON above), and reload semantics are not what this test
    # is about.
    cams = str(self.tmp_path / "korea_cameras_on_route.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD]))
    db = self.open_db(cams, str(self.tmp_path / "korea_links.sqlite"))
    camera = db.next_camera(37.5000, 127.0200, 90., route=self.route)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 50)

  def test_an_empty_route_behaves_like_no_route(self):
    self.assertEqual(self.db.next_camera(37.5000, 127.0200, 90.),
                     self.db.next_camera(37.5000, 127.0200, 90., route=[]))

  def test_a_route_running_the_other_way_rejects_everything(self):
    behind = [(37.5000, 127.0200), (37.5000, 127.0080)]
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., route=behind))

  def test_the_braking_corridor_rejects_the_side_road_camera(self):
    """No route at all: the cone still offers the side-road camera for the sign, but it is
    no slowdown target."""
    self.assertIsNotNone(self.db.next_camera(37.5000, 127.0200, 90.))
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., corridor_m=CAMERA_CORRIDOR_M))

  def test_the_braking_corridor_keeps_a_camera_on_the_road(self):
    # A distinct path, for the same reason as test_a_camera_on_the_route_survives.
    cams = str(self.tmp_path / "korea_cameras_on_road.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD]))
    db = self.open_db(cams, str(self.tmp_path / "korea_links.sqlite"))
    camera = db.next_camera(37.5000, 127.0200, 90., corridor_m=CAMERA_CORRIDOR_M)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.limit_kph, 50)


# ~25 m north of the query point's road, ~150 m ahead: distance_to_route against
# TestRouteCorridorBumps.route computes to ~25.02 m (inside ROUTE_CORRIDOR_M, 30 m) while
# the cross-track corridor check computes to the same ~25.02 m (outside BUMP_CORRIDOR_M,
# 20 m) -- 5 m of margin on both sides. BUMP_LATERAL_30M (defined above) does not work for
# this: at ~30.02 m its distance_to_route is *outside* ROUTE_CORRIDOR_M too, so the route
# check alone would already reject it and the test would pass even with the corridor check
# deleted entirely.
BUMP_LATERAL_25M = (37.500225, 127.021700, 0)


class TestRouteCorridorBumps(KoreaMapDBTestCase):
  def setUp(self):
    super().setUp()
    self.db = self.open_db_with_bumps([BUMP_AHEAD_150, BUMP_LATERAL_10M])
    # ROAD_60 itself: east from the query point, which is what every bump test drives
    self.route = [(37.5000, 127.0200), (37.5000, 127.0320)]
    # a route that turns north 50 m ahead -- the bumps stay east, off it
    self.turn = [(37.5000, 127.0200), (37.5000, 127.0206), (37.5030, 127.0206)]

  def test_without_a_route_the_nearest_bump_wins(self):
    bump = self.db.next_bump(37.5000, 127.0200, 90.)
    self.assertIsNotNone(bump)
    self.assertAlmostEqual(bump.lon, 127.0217, places=4)

  def test_a_route_along_the_road_keeps_the_same_bump(self):
    self.assertEqual(self.db.next_bump(37.5000, 127.0200, 90.),
                     self.db.next_bump(37.5000, 127.0200, 90., route=self.route))

  def test_a_route_that_turns_away_rejects_the_bumps(self):
    self.assertIsNone(self.db.next_bump(37.5000, 127.0200, 90., route=self.turn))

  def test_an_empty_route_behaves_like_no_route(self):
    self.assertEqual(self.db.next_bump(37.5000, 127.0200, 90.),
                     self.db.next_bump(37.5000, 127.0200, 90., route=[]))

  def test_the_route_never_admits_what_the_corridor_rejects(self):
    # BUMP_LATERAL_25M is ~25 m north: outside BUMP_CORRIDOR_M (20 m) but inside
    # ROUTE_CORRIDOR_M (30 m). The route must not promote it.
    # self.db is not used below; close it first so open_db_with_bumps (fixed filenames)
    # can replace the same files. Otherwise os.replace races a live reader -- a POSIX
    # guarantee Windows does not provide (see _REPLACE_WHILE_OPEN_SKIP_REASON above).
    self.db.close()
    db = self.open_db_with_bumps([BUMP_LATERAL_25M])
    self.assertIsNone(db.next_bump(37.5000, 127.0200, 90.))
    self.assertIsNone(db.next_bump(37.5000, 127.0200, 90., route=self.route))


class TestCameraKindFilter(KoreaMapDBTestCase):
  """next_camera(kinds=...): the kind toggles decide which cameras exist at all."""

  def setUp(self):
    super().setUp()
    cams = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_ZONE_NEAR, CAM_AHEAD]))
    write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60]))
    self.db = self.open_db(cams, links)

  def test_no_kinds_means_every_kind(self):
    camera = self.db.next_camera(37.5000, 127.0200, 90.)
    self.assertIsNotNone(camera)
    self.assertEqual(camera.kind, CAMERA_ZONE)

  def test_a_nearer_camera_that_is_off_does_not_hide_a_farther_one_that_is_on(self):
    camera = self.db.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED})
    self.assertIsNotNone(camera)
    self.assertEqual((camera.kind, camera.limit_kph), (CAMERA_SPEED, 50))

  def test_every_kind_off_means_no_camera(self):
    self.assertIsNone(self.db.next_camera(37.5000, 127.0200, 90., kinds=frozenset()))

  def test_the_camera_carries_its_coordinate(self):
    camera = self.db.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED})
    self.assertEqual((camera.lat, camera.lon), CAM_AHEAD[:2])


class TestCameraDatabaseWithoutKind(KoreaMapDBTestCase):
  """A camera file built before the kind column. It must work exactly as it did before."""

  @staticmethod
  def _has_kind(path):
    con = sqlite3.connect(path)
    try:
      return has_camera_kind(con)
    finally:
      con.close()

  def test_has_camera_kind_tells_the_two_apart(self):
    cams, _ = self._make_pair()
    self.assertTrue(self._has_kind(cams))
    drop_kind(cams)
    self.assertFalse(self._has_kind(cams))

  def test_every_camera_passes_any_filter(self):
    cams, links = self._make_pair()
    drop_kind(cams)
    camera = self.open_db(cams, links).next_camera(37.5000, 127.0200, 90., kinds=frozenset())
    self.assertIsNotNone(camera, "an old database lost its cameras to a filter it cannot answer")
    self.assertIsNone(camera.kind)
    self.assertEqual(camera.limit_kph, 50)

  @unittest.skipIf(sys.platform == "win32", _REPLACE_WHILE_OPEN_SKIP_REASON)
  def test_a_reload_notices_the_new_column(self):
    """camera_refresh rebuilds an old file within the hour. The running process must start
    filtering then, not at the next reboot."""
    cams, links = self._make_pair()
    drop_kind(cams)
    database = self.open_db(cams, links)
    self.assertIsNotNone(database.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED}))

    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_ZONE_NEAR]))
    stamp = os.path.getmtime(cams) + 1  # an unambiguous change on any filesystem
    os.utime(cams, (stamp, stamp))

    self.assertTrue(database.reload_if_changed())
    self.assertIsNone(database.next_camera(37.5000, 127.0200, 90., kinds={CAMERA_SPEED}))
