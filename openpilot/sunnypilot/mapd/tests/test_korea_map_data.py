"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pathlib
import sqlite3
import struct
import tempfile
import time
import unittest

import openpilot.cereal.messaging as messaging
from openpilot.common.constants import CV
from openpilot.common.parameterized import parameterized
from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_CAMERAS, SCHEMA_LINKS, insert_cameras,
                                                      insert_links, write_db)
from openpilot.sunnypilot.mapd.korea.db import Camera, Link
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNav
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import KoreaMapData
from openpilot.sunnypilot.navd.helpers import Coordinate


class StubExternal:
  def __init__(self, nav):
    self.nav = nav

  def latest(self):
    return self.nav


def make_data(link=None, camera=None, external=None):
  """Build the object without BaseMapData.__init__ so no messaging sockets are needed."""
  data = KoreaMapData.__new__(KoreaMapData)
  data.cameras_path = ""
  data.links_path = ""
  data.db = None
  data.open_failed = False
  data.external = external
  data.link = link
  data.camera = camera
  return data


def location_message():
  """A default liveLocationKalman: status uninitialized, positionGeodetic invalid.

  That leaves last_position as whatever the test preset, which is what we want -- this
  test is about the reload call, not about the localizer.
  """
  return {'liveLocationKalman': messaging.new_message('liveLocationKalman').liveLocationKalman}


class TestPublishedValues(unittest.TestCase):
  def test_no_data_publishes_zero(self):
    data = make_data()
    self.assertEqual(data.get_current_speed_limit(), 0.)
    self.assertEqual(data.get_next_speed_limit_and_distance(), (0., 0.))
    self.assertEqual(data.get_current_road_name(), "")

  def test_link_speed_limit_is_converted_to_ms(self):
    data = make_data(link=Link(max_spd=60, name="테헤란로"))
    self.assertAlmostEqual(data.get_current_speed_limit(), 60 * CV.KPH_TO_MS, places=9)
    self.assertEqual(data.get_current_road_name(), "테헤란로")

  def test_camera_becomes_the_next_speed_limit(self):
    data = make_data(camera=Camera(limit_kph=50, distance_m=320., section_m=0))
    limit, distance = data.get_next_speed_limit_and_distance()
    self.assertAlmostEqual(limit, 50 * CV.KPH_TO_MS, places=9)
    self.assertEqual(distance, 320.)

  def test_external_source_wins_over_the_database(self):
    nav = ExternalNav(speed_limit_kph=40., next_speed_limit_kph=30.,
                      next_speed_limit_distance_m=150., road_name="시장길",
                      received_at=time.monotonic())
    data = make_data(link=Link(max_spd=60, name="테헤란로"),
                     camera=Camera(limit_kph=50, distance_m=320., section_m=0),
                     external=StubExternal(nav))
    self.assertAlmostEqual(data.get_current_speed_limit(), 40 * CV.KPH_TO_MS, places=9)
    self.assertAlmostEqual(data.get_next_speed_limit_and_distance()[0], 30 * CV.KPH_TO_MS, places=9)
    self.assertEqual(data.get_next_speed_limit_and_distance()[1], 150.)
    self.assertEqual(data.get_current_road_name(), "시장길")

  def test_stale_external_source_falls_back_to_the_database(self):
    data = make_data(link=Link(max_spd=60, name="테헤란로"), external=StubExternal(None))
    self.assertAlmostEqual(data.get_current_speed_limit(), 60 * CV.KPH_TO_MS, places=9)
    self.assertEqual(data.get_current_road_name(), "테헤란로")

  def test_empty_external_fields_fall_back_per_field(self):
    # an app that only knows the road name must not blank out the database speed limit
    nav = ExternalNav(road_name="시장길", received_at=time.monotonic())
    data = make_data(link=Link(max_spd=60, name="테헤란로"), external=StubExternal(nav))
    self.assertAlmostEqual(data.get_current_speed_limit(), 60 * CV.KPH_TO_MS, places=9)
    self.assertEqual(data.get_current_road_name(), "시장길")


class TestOpenDB(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

  def test_open_db_is_a_noop_when_a_file_is_missing(self):
    """Both files must be present. Half a database is not a usable database."""
    cameras = str(self.tmp_path / "korea_cameras.sqlite")
    write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0)]))

    data = make_data()
    data.cameras_path = cameras
    data.links_path = str(self.tmp_path / "does_not_exist.sqlite")
    data.open_db()
    self.assertIsNone(data.db)
    self.assertFalse(data.open_failed)  # a missing file is "not yet copied", not a failure

  def test_open_db_opens_both_files(self):
    cameras = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0)]))
    write_db(links, SCHEMA_LINKS,
             lambda con: insert_links(con, [(60, "테헤란로", [(37.5000, 127.0200), (37.5000, 127.0320)])]))

    data = make_data()
    data.cameras_path = cameras
    data.links_path = links
    data.open_db()
    self.assertIsNotNone(data.db)
    self.addCleanup(data.db.close)
    self.assertEqual(data.db.current_link(37.5000, 127.0260).max_spd, 60)

  def test_open_db_gives_up_after_a_bad_database(self):
    """A schema mismatch never fixes itself; retrying at 1 Hz would only flood the log."""
    cameras = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0)]))
    with open(links, "wb") as f:
      f.write(b"not a database")

    data = make_data()
    data.cameras_path = cameras
    data.links_path = links
    data.open_db()
    self.assertIsNone(data.db)
    self.assertTrue(data.open_failed)

    # A second call must not even try again. Both paths point at openable files now, so
    # only the open_failed short-circuit can keep db None -- aiming the tripwire at a
    # nonexistent path instead would be satisfied by the os.path.exists precheck and would
    # pass whether or not the short-circuit exists.
    data.links_path = cameras
    data.open_db()
    self.assertIsNone(data.db, "open_db retried after giving up")


class StubDB:
  """Counts reload_if_changed() so a test can prove update_location calls it."""

  def __init__(self):
    self.reloads = 0

  def reload_if_changed(self):
    self.reloads += 1
    return False

  def current_link(self, lat, lon, heading_deg=None):
    return None

  def next_camera(self, lat, lon, heading_deg):
    return None


class ExplodingDB:
  """Raises the way a corrupt database does, and records that it was closed."""

  def __init__(self, exc):
    self.exc = exc
    self.closed = False

  def reload_if_changed(self):
    return False

  def current_link(self, lat, lon, heading_deg=None):
    raise self.exc

  def next_camera(self, lat, lon, heading_deg):
    raise self.exc

  def close(self):
    self.closed = True


class TestUpdateLocation(unittest.TestCase):
  def test_update_location_reloads_a_swapped_camera_database(self):
    """Task 13 swaps korea_cameras.sqlite with os.replace while this process runs.

    update_location is the only caller of reload_if_changed in the repo; without it a
    refreshed camera database never reaches a running process and the failure is silent.
    """
    data = make_data()
    data.sm = location_message()
    data.db = StubDB()
    data.last_position = Coordinate(37.5000, 127.0260)
    data.last_bearing = None

    data.update_location()
    data.update_location()

    self.assertEqual(data.db.reloads, 2)

  def test_update_location_does_not_reload_before_the_database_is_open(self):
    """The guard order matters: reload_if_changed on a None db is an AttributeError."""
    data = make_data()
    data.sm = location_message()
    data.last_position = Coordinate(37.5000, 127.0260)
    data.last_bearing = None

    data.update_location()  # db stays None -- cameras_path/links_path are ""

    self.assertIsNone(data.db)

  # Both are reachable from one truncated file. A corrupt page raises out of sqlite, but a
  # short geometry blob raises struct.error out of _unpack_geom -- not a sqlite exception at
  # all. Either one escaping reaches mapd_manager's bare `while True` and takes the process
  # down, which raises processNotRunning and blocks engagement.
  @parameterized.expand([sqlite3.DatabaseError("database disk image is malformed"),
                         struct.error("unpack requires a buffer of 16 bytes")])
  def test_a_corrupt_database_is_dropped_instead_of_killing_the_process(self, exc):
    db = ExplodingDB(exc)
    data = make_data()
    data.sm = location_message()
    data.db = db
    data.last_position = Coordinate(37.5000, 127.0260)
    data.last_bearing = None

    data.update_location()          # must not raise

    self.assertIsNone(data.db)
    self.assertTrue(data.open_failed)
    self.assertTrue(db.closed)
    self.assertEqual(data.get_current_speed_limit(), 0.)

    data.update_location()          # and must not reopen it
    self.assertIsNone(data.db)
