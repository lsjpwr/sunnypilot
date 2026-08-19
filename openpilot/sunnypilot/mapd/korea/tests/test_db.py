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

# Windows will not os.replace() (nor otherwise swap the bytes of) a file that has an open
# sqlite3 connection -- PermissionError: WinError 5 -- even a read-only, idle connection.
# Confirmed with an isolated repro outside this suite: identical write_db()-style
# tmp-file-then-os.replace sequence fails on win32 with an open ro connection on the
# destination, and succeeds with nothing open. This is a Windows file-sharing limitation,
# not a defect in reload_if_changed(): the real target is the comma device (Linux), where
# POSIX rename semantics let existing readers keep the old inode. Skipped here rather than
# weakened, so the assertions stay meaningful on POSIX CI where they do exercise the swap.
_REPLACE_WHILE_OPEN_SKIP_REASON = "os.replace() of a file with an open sqlite3 connection always raises PermissionError on win32"


def _make_pair(tmp_path):
  cams = str(tmp_path / "korea_cameras.sqlite")
  links = str(tmp_path / "korea_links.sqlite")
  write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [CAM_AHEAD, CAM_BEHIND, CAM_SECTION]))
  write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [ROAD_60, ROAD_100]))
  return cams, links


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
    os.utime(cams, (0, 0))
    with open(cams, "wb") as f:
      f.write(b"not a database at all")
    assert database.reload_if_changed() is False
    # the live connection must still answer from the database it already had open
    assert database.next_camera(37.4990, 127.0260, 0.) is not None
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


def test_next_camera_reports_section_length(db):
  camera = db.next_camera(37.5000, 127.0270, heading_deg=90.)
  assert camera is not None
  assert camera.limit_kph == 80
  assert camera.section_m == 4200


def test_next_camera_needs_a_heading(db):
  assert db.next_camera(37.5000, 127.0200, heading_deg=None) is None


def test_next_camera_none_when_far_away(db):
  assert db.next_camera(37.6000, 127.2000, heading_deg=90.) is None
