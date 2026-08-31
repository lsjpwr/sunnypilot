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

from openpilot.sunnypilot.mapd.korea.build_db import SCHEMA_CAMERAS, SCHEMA_LINKS, insert_cameras, insert_links, write_db
from openpilot.sunnypilot.mapd.korea.db import KoreaMapDB

# a 1 km east-west stretch of road at 60 km/h, and a parallel one at 100 km/h 300 m north
ROAD_60 = (60, "테헤란로", [(37.5000, 127.0200), (37.5000, 127.0320)])
ROAD_100 = (100, "고속화도로", [(37.5027, 127.0200), (37.5027, 127.0320)])

# camera 500 m east of the start of ROAD_60, and one 500 m west (behind us)
CAM_AHEAD = (37.5000, 127.0257, 50, 0)
CAM_BEHIND = (37.5000, 127.0143, 30, 0)
CAM_SECTION = (37.5000, 127.0280, 80, 4200)

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
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.4990, 127.0260, 40, 0)]))

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
