"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import sqlite3
import sys

import pytest

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


def _make_pair(tmp_path):
  cams = str(tmp_path / "korea_cameras.sqlite")
  links = str(tmp_path / "korea_links.sqlite")
  write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD, CAM_BEHIND, CAM_SECTION]))
  write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60, ROAD_100]))
  return cams, links


def _make_links_db(tmp_path, links):
  """Like _make_pair, but with a caller-chosen link set and no cameras."""
  cams = str(tmp_path / "korea_cameras.sqlite")
  links_path = str(tmp_path / "korea_links.sqlite")
  write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, []))
  write_db(links_path, SCHEMA_LINKS, lambda con: insert_links(con, links))
  return cams, links_path


@pytest.fixture
def db(tmp_path):
  cams, links = _make_pair(tmp_path)
  database = KoreaMapDB(cams, links)
  yield database
  database.close()


def test_rejects_wrong_schema_version(tmp_path):
  cams, links = _make_pair(tmp_path)
  con = sqlite3.connect(cams)
  con.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
  con.commit()
  con.close()
  with pytest.raises(ValueError):
    KoreaMapDB(cams, links)


@pytest.mark.skipif(sys.platform == "win32", reason=_REPLACE_WHILE_OPEN_SKIP_REASON)
def test_reload_if_changed_picks_up_a_replaced_camera_db(tmp_path):
  cams, links = _make_pair(tmp_path)
  database = KoreaMapDB(cams, links)
  try:
    # heading north (0 deg): all 3 fixture cameras sit north of this point, so heading
    # south (as used later in this test, for the post-reload check) would find nothing
    assert database.next_camera(37.4990, 127.0260, 0.) is not None
    assert database.reload_if_changed() is False  # nothing changed yet

    # swap in a camera database holding a different limit, the way Task 13 will
    os.utime(cams, (0, 0))  # force a distinct mtime on fast filesystems
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.4990, 127.0260, 40, 0)]))

    assert database.reload_if_changed() is True
    cam = database.next_camera(37.4995, 127.0260, 180.)
    assert cam is not None and cam.limit_kph == 40
    assert database.reload_if_changed() is False  # idempotent
  finally:
    database.close()


@pytest.mark.skipif(sys.platform == "win32", reason=_REPLACE_WHILE_OPEN_SKIP_REASON)
def test_reload_keeps_the_old_db_when_the_new_one_is_broken(tmp_path):
  cams, links = _make_pair(tmp_path)
  database = KoreaMapDB(cams, links)
  try:
    # Corrupt it the way a bad refresh actually would: build a garbage file and
    # os.replace it in. Truncating the live file in place instead would destroy the
    # inode the open connection is reading and no amount of defensive code could
    # survive that -- but Task 13 never does that, it always swaps a new file in.
    bad = cams + ".bad"
    with open(bad, "wb") as f:
      f.write(b"not a database at all")
    os.replace(bad, cams)
    os.utime(cams, (0, 0))
    assert database.reload_if_changed() is False
    # the live connection must still answer from the database it already had open --
    # check the specific camera, not just non-None, so a reload silently swapping in
    # different-but-non-None data would fail this
    cam = database.next_camera(37.4990, 127.0260, 0.)
    assert cam is not None and cam.limit_kph == 50
  finally:
    database.close()


def test_current_link_on_the_road(db):
  link = db.current_link(37.5000, 127.0260)
  assert link is not None
  assert link.max_spd == 60
  assert link.name == "테헤란로"


def test_current_link_picks_the_nearer_road(db):
  # 30 m north of ROAD_60 is still much closer to it than to ROAD_100
  link = db.current_link(37.5003, 127.0260)
  assert link is not None and link.max_spd == 60

  # sitting on ROAD_100
  link = db.current_link(37.5027, 127.0260)
  assert link is not None and link.max_spd == 100


def test_current_link_returns_none_when_off_road(db):
  # ~1.5 km north of everything
  assert db.current_link(37.5140, 127.0260) is None


def test_current_link_heading_filter_drops_crossing_roads(db):
  # both test roads run east-west; driving due north must not match either
  assert db.current_link(37.5000, 127.0260, heading_deg=0.) is None
  # driving east matches, and so does driving west (a two-way road may be digitised either way)
  assert db.current_link(37.5000, 127.0260, heading_deg=90.) is not None
  assert db.current_link(37.5000, 127.0260, heading_deg=270.) is not None


def test_next_camera_only_looks_ahead(db):
  # heading east from between CAM_BEHIND and CAM_AHEAD
  camera = db.next_camera(37.5000, 127.0200, heading_deg=90.)
  assert camera is not None
  assert camera.limit_kph == 50
  assert 400. < camera.distance_m < 600.
  assert camera.section_m == 0


def test_next_camera_picks_the_nearest_ahead(db):
  # heading east from just before CAM_AHEAD: CAM_SECTION is further away
  camera = db.next_camera(37.5000, 127.0250, heading_deg=90.)
  assert camera is not None and camera.limit_kph == 50


def test_next_camera_behind_us_is_ignored(db):
  # heading west from the start: only CAM_BEHIND is in front now
  camera = db.next_camera(37.5000, 127.0200, heading_deg=270.)
  assert camera is not None and camera.limit_kph == 30


def test_next_camera_passes_section_field_through(db):
  # section_m is raw provenance, not a real length (see Camera's docstring) -- this only
  # checks it passes through the field unmodified, not that 4200 means anything as a distance
  camera = db.next_camera(37.5000, 127.0270, heading_deg=90.)
  assert camera is not None
  assert camera.limit_kph == 80
  assert camera.section_m == 4200


def test_next_camera_needs_a_heading(db):
  assert db.next_camera(37.5000, 127.0200, heading_deg=None) is None


def test_next_camera_none_when_far_away(db):
  assert db.next_camera(37.6000, 127.2000, heading_deg=90.) is None


def test_current_link_prefers_the_higher_limit_when_tied(tmp_path):
  cams, links = _make_links_db(tmp_path, [OVERPASS_LOW, OVERPASS_HIGH])
  database = KoreaMapDB(cams, links)
  try:
    # exactly on the shared start point: both links are 0 m away, a dead tie
    link = database.current_link(37.5100, 127.0500)
    assert link is not None and link.max_spd == 80
  finally:
    database.close()


def test_current_link_holds_the_match_then_moves_on(tmp_path):
  cams, links = _make_links_db(tmp_path, [OVERPASS_LOW, OVERPASS_HIGH])
  database = KoreaMapDB(cams, links)
  try:
    first = database.current_link(37.5100, 127.0500)
    second = database.current_link(37.5100, 127.0500)
    assert first is not None and first.max_spd == 80
    assert second is not None and second.max_spd == 80

    # past the overpass (~790 m on): OVERPASS_HIGH no longer covers this point at all, so
    # stickiness releases on its own and OVERPASS_LOW is the only remaining candidate
    later = database.current_link(37.5100, 127.0600)
    assert later is not None and later.max_spd == 50
  finally:
    database.close()


def test_current_link_tie_break_does_not_apply_across_separate_roads(tmp_path):
  cams, links = _make_links_db(tmp_path, [FAR_LOW, FAR_HIGH])
  database = KoreaMapDB(cams, links)
  try:
    # sitting on FAR_LOW; FAR_HIGH is a real candidate ~33 m away but nowhere near tied
    link = database.current_link(37.5200, 127.0510)
    assert link is not None and link.max_spd == 30
  finally:
    database.close()


def test_current_link_tied_pair_returns_the_higher_limit_on_repeated_calls(tmp_path):
  # NOTE: this alone does not prove hysteresis is doing anything -- max(tied, ...) picks
  # 80 here every time regardless of the sticky branch, since 80 is genuinely the higher
  # limit of the tied pair. It only pins that repeated calls stay consistent. The sticky
  # branch itself is isolated by test_current_link_sticky_link_wins_over_the_tie_break_when_tied.
  cams, links = _make_links_db(tmp_path, [OVERPASS_LOW, OVERPASS_HIGH])
  database = KoreaMapDB(cams, links)
  try:
    first = database.current_link(37.5100, 127.0500)
    second = database.current_link(37.5100, 127.0500)
    assert first is not None and second is not None
    assert first.max_spd == second.max_spd == 80
  finally:
    database.close()


def test_current_link_sticky_link_wins_over_the_tie_break_when_tied(tmp_path):
  """Isolates hysteresis: without the sticky branch the tie-break would pick the other one."""
  cams, links = _make_links_db(tmp_path, [OVERPASS_LOW, OVERPASS_HIGH])
  database = KoreaMapDB(cams, links)
  try:
    # Both overpass links are coincident, so the tie-break alone always yields the higher
    # limit (80) -- confirm that baseline first.
    on_top = database.current_link(37.5100, 127.0500)
    assert on_top is not None and on_top.max_spd == 80, on_top

    # Pin the previous match to the LOWER link by its real rowid (looked up, not
    # hardcoded, so this does not break if insertion order ever changes). max_spd = 50
    # is unambiguous: OVERPASS_HIGH is the only other row and it is 80.
    con = sqlite3.connect(links)
    low_id = con.execute("SELECT id FROM links WHERE max_spd = 50").fetchone()[0]
    con.close()
    database._last_link_id = low_id

    # Only the sticky branch can make the lower link come back here.
    held = database.current_link(37.5100, 127.0500)
    assert held is not None and held.max_spd == 50, held

    # and it keeps holding it
    assert database.current_link(37.5100, 127.0500).max_spd == 50
  finally:
    database.close()


def test_current_link_stale_sticky_link_does_not_outrank_the_nearer_road(tmp_path):
  # Reviewer-reported regression: a sticky link ~24.5 m away (245x TIE_DISTANCE_M, nowhere
  # near tied) must never outrank the road the query point is actually sitting on.
  # Dangerous direction: the stale/far link carries the HIGHER limit, so a bug here would
  # silently permit 100 km/h on a road signed for 30. Checked both insertion orders --
  # the old bug's outcome depended on the order sqlite happened to return rows in.
  for order in ([STICKY_FAR, STICKY_NEAR], [STICKY_NEAR, STICKY_FAR]):
    cams, links = _make_links_db(tmp_path, order)
    database = KoreaMapDB(cams, links)
    try:
      # sit on the far link first, so hysteresis latches onto it for real, the way it
      # would after actually driving that road
      first = database.current_link(37.53022, 127.0500)
      assert first is not None and first.max_spd == 100

      # now the query point is on the near, slower road -- the far link is still within
      # LINK_MAX_DISTANCE_M but 24.5 m away is not a tie, so it must not win
      second = database.current_link(37.5300, 127.0500)
      assert second is not None and second.max_spd == 30
    finally:
      database.close()
