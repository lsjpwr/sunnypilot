"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import os
import pathlib
import sqlite3
import struct
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.parameterized import parameterized
from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_CAMERAS, SCHEMA_LINKS, insert_cameras,
                                                      insert_links, write_db)
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, BUMP_VIRTUAL, CAMERA_KIND_PARAMS,
                                                 CAMERA_SECTION, CAMERA_SECTION_END, CAMERA_SIGNAL, CAMERA_SPEED,
                                                 CAMERA_ZONE, Bump, Camera, KoreaMapDB, Link)
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
  data.bumps_path = ""
  data.db = None
  data.open_failed = False
  data._failed_mtimes = ()
  data.external = external
  data.route_source = None
  data.params = StubMemParams()
  data.params.values["KoreaRouteCurveEnabled"] = True  # on by default, like params_keys.h
  data._last_written_destination = None
  data.route = []
  data.curve_points = []
  data.link = link
  data.camera = camera
  data.slowdown_camera = None
  data._camera_anchor = None
  data.bump = None
  data.mem_params = StubMemParams()
  data.bump_enabled = False
  data.bump_targets = {}
  data.camera_kinds = frozenset(CAMERA_KIND_PARAMS)
  data.camera_margin = 50
  data.localizer_valid = True
  data.section = None
  data._section_prev = None
  data._section_passed = None
  return data


class StubMemParams:
  """Doubles as both self.params and self.mem_params: both are plain key/value stores in
  the real Params, and put_calls lets a test count writes without monkeypatching put()."""

  def __init__(self):
    self.values: dict[str, str] = {}
    self.put_calls = 0

  def put(self, key, value, block=False):
    self.values[key] = value
    self.put_calls += 1

  def get(self, key, block=False, return_default=False):
    return self.values.get(key)

  def get_bool(self, key, block=False):
    return bool(self.values.get(key))

  def remove(self, key):
    self.values.pop(key, None)


def make_bump_data(bump=None, enabled=True, arch_kph=25, trapezoid_kph=35, position=None, localizer_valid=True,
                   camera=None, margin=50):
  """A KoreaMapData wired only far enough to exercise publish_targets."""
  data = KoreaMapData.__new__(KoreaMapData)
  data.mem_params = StubMemParams()
  data.bump = bump
  data.bump_enabled = enabled
  data.bump_targets = {BUMP_ARCH: arch_kph * CV.KPH_TO_MS, BUMP_TRAPEZOID: trapezoid_kph * CV.KPH_TO_MS}
  data.slowdown_camera = camera
  data._camera_anchor = None
  data.camera_margin = margin
  data.last_position = position if position is not None else Coordinate(37.5, 127.0)
  data.localizer_valid = localizer_valid
  data.curve_points = []
  data.section = None
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
    data = make_data(camera=Camera(lat=37.5029, lon=127.0276, limit_kph=50, distance_m=320., section_m=0,
                                   kind=CAMERA_SPEED))
    limit, distance = data.get_next_speed_limit_and_distance()
    self.assertAlmostEqual(limit, 50 * CV.KPH_TO_MS, places=9)
    self.assertEqual(distance, 320.)

  def test_external_source_wins_over_the_database(self):
    nav = ExternalNav(speed_limit_kph=40., next_speed_limit_kph=30.,
                      next_speed_limit_distance_m=150., road_name="시장길",
                      received_at=time.monotonic())
    data = make_data(link=Link(max_spd=60, name="테헤란로"),
                     camera=Camera(lat=37.5029, lon=127.0276, limit_kph=50, distance_m=320., section_m=0,
                                   kind=CAMERA_SPEED),
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


class TestUpdateDestination(unittest.TestCase):
  """update_destination() is the only link between the socket/athenad and the route thread:
  nothing downstream of NavDestination ever runs if this method gets a branch wrong. Its
  three branches are driven by key presence in nav.raw, not by nav.destination itself --
  see the method's own docstring for why those must be told apart.
  """

  def test_keys_absent_from_raw_leaves_the_param_untouched(self):
    """No destination_lat/lon in this datagram at all: neither written nor removed, whether
    that means untouched or already cleared."""
    data = make_data(external=StubExternal(ExternalNav(raw={"speed_limit_kph": 60})))
    data.params.put("NavDestination", "sentinel")
    puts_before = data.params.put_calls

    data.update_destination()

    self.assertEqual(data.params.put_calls, puts_before, "a datagram with no destination keys must not write")
    self.assertEqual(data.params.get("NavDestination"), "sentinel")

  def test_keys_present_but_parsed_to_none_removes_the_param(self):
    """0/0 (or an out-of-Korea point) parses to None but the keys are still in raw -- the
    phone's explicit end-of-guidance signal, not a no-op tick."""
    nav = ExternalNav(destination=None, raw={"destination_lat": 0, "destination_lon": 0})
    data = make_data(external=StubExternal(nav))
    data.params.put("NavDestination", json.dumps({"latitude": 37.5665, "longitude": 126.9780,
                                                   "place_name": None, "place_details": None}))
    data._last_written_destination = (37.5665, 126.9780)

    data.update_destination()

    self.assertIsNone(data.params.get("NavDestination"))
    self.assertIsNone(data._last_written_destination)

  def test_a_new_destination_is_written_in_athenads_json_shape(self):
    nav = ExternalNav(destination=(37.5665, 126.9780), road_name="테헤란로",
                      raw={"destination_lat": 37.5665, "destination_lon": 126.9780})
    data = make_data(external=StubExternal(nav))

    data.update_destination()

    self.assertEqual(json.loads(data.params.get("NavDestination")), {
      "latitude": 37.5665, "longitude": 126.9780, "place_name": "테헤란로", "place_details": None,
    })
    self.assertEqual(data._last_written_destination, (37.5665, 126.9780))

  def test_the_same_destination_repeated_is_not_rewritten(self):
    """A phone that resends the same destination every datagram must not touch /data/params
    (flash) once a second forever."""
    nav = ExternalNav(destination=(37.5665, 126.9780), road_name="테헤란로",
                      raw={"destination_lat": 37.5665, "destination_lon": 126.9780})
    data = make_data(external=StubExternal(nav))
    data.update_destination()
    self.assertEqual(data.params.put_calls, 1)

    data.update_destination()  # same nav object, next tick

    self.assertEqual(data.params.put_calls, 1, "a repeated destination must not rewrite the param")

  def test_a_destination_cleared_by_someone_else_is_written_again(self):
    """Regression test for Fix 3: NavDestination is cleared by CLEAR_ON_OFFROAD_TRANSITION
    when the drive ends and by RouteSource._loop on arrival -- korea_main rebuilds this
    object for neither event, so _last_written_destination alone must not decide whether to
    write. Without this, the second drive to the same place (the commute home) would see
    "unchanged" and never rewrite an already-empty param."""
    nav = ExternalNav(destination=(37.5665, 126.9780), road_name="테헤란로",
                      raw={"destination_lat": 37.5665, "destination_lon": 126.9780})
    data = make_data(external=StubExternal(nav))
    data.update_destination()
    self.assertEqual(data.params.put_calls, 1)

    data.params.remove("NavDestination")  # manager on offroad transition, or arrival

    data.update_destination()  # the same destination, next drive

    self.assertEqual(data.params.put_calls, 2, "a destination must be rewritten once the param is cleared")
    self.assertIsNotNone(data.params.get("NavDestination"))


class TestOpenDB(unittest.TestCase):
  def setUp(self):
    super().setUp()
    self.tmp_path = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

  def test_open_db_is_a_noop_when_a_file_is_missing(self):
    """Both files must be present. Half a database is not a usable database."""
    cameras = str(self.tmp_path / "korea_cameras.sqlite")
    write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0, CAMERA_SPEED)]))

    data = make_data()
    data.cameras_path = cameras
    data.links_path = str(self.tmp_path / "does_not_exist.sqlite")
    data.open_db()
    self.assertIsNone(data.db)
    self.assertFalse(data.open_failed)  # a missing file is "not yet copied", not a failure

  def test_open_db_opens_both_files(self):
    cameras = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0, CAMERA_SPEED)]))
    write_db(links, SCHEMA_LINKS,
             lambda con: insert_links(con, [(60, "테헤란로", [(37.5000, 127.0200), (37.5000, 127.0320)])]))

    data = make_data()
    data.cameras_path = cameras
    data.links_path = links
    data.open_db()
    self.assertIsNotNone(data.db)
    self.addCleanup(data.db.close)
    self.assertEqual(data.db.current_link(37.5000, 127.0260).max_spd, 60)

  def bad_pair(self):
    """A good camera database and a links file that is not a database at all."""
    cameras = str(self.tmp_path / "korea_cameras.sqlite")
    links = str(self.tmp_path / "korea_links.sqlite")
    write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0, CAMERA_SPEED)]))
    with open(links, "wb") as f:
      f.write(b"not a database")

    data = make_data()
    data.cameras_path = cameras
    data.links_path = links
    data.bumps_path = str(self.tmp_path / "korea_bumps.sqlite")
    return data, links

  def test_open_db_gives_up_after_a_bad_database(self):
    """A schema mismatch never fixes itself while the file sits there; retrying at 1 Hz
    would only flood the log."""
    data, _ = self.bad_pair()
    data.open_db()
    self.assertIsNone(data.db)
    self.assertTrue(data.open_failed)

    # A second call must not even try again. Counting the constructor is the tripwire,
    # because both paths still name the same bytes -- "db is still None" would be just as
    # true with the short-circuit deleted.
    with mock.patch("openpilot.sunnypilot.mapd.live_map_data.korea_map_data.KoreaMapDB") as ctor:
      data.open_db()
    self.assertEqual(ctor.call_count, 0, "open_db retried after giving up")
    self.assertIsNone(data.db)

  def test_a_replaced_database_clears_the_giving_up(self):
    """map_download installs a good database over a broken one with os.replace while this
    process runs. Sticky open_failed meant the device downloaded the fix and then ignored it
    until a restart -- which is the opposite of what docs/korea_map_update.md promises."""
    data, links = self.bad_pair()
    data.open_db()
    self.assertTrue(data.open_failed)

    write_db(links, SCHEMA_LINKS,
             lambda con: insert_links(con, [(60, "테헤란로", [(37.5000, 127.0200), (37.5000, 127.0320)])]))
    # explicit rather than trusting the clock: a coarse filesystem timestamp would leave the
    # replacement looking untouched and make this pass for the wrong reason
    moved = os.path.getmtime(links) + 10
    os.utime(links, (moved, moved))

    data.open_db()

    self.assertIsNotNone(data.db, "a replaced database still needs a reboot to take effect")
    self.addCleanup(data.db.close)
    self.assertFalse(data.open_failed)
    self.assertEqual(data.db.current_link(37.5000, 127.0260).max_spd, 60)


class StubDB:
  """Counts reload_if_changed() so a test can prove update_location calls it."""

  def __init__(self):
    self.reloads = 0

  def reload_if_changed(self):
    self.reloads += 1
    return False

  def current_link(self, lat, lon, heading_deg=None, route=None):
    return None

  def next_camera(self, lat, lon, heading_deg, route=None, kinds=None, corridor_m=None):
    return None

  def next_bump(self, lat, lon, heading_deg, route=None):
    return None

  def section_starts_near(self, lat, lon):
    return []

  def section_end_ahead(self, lat, lon, heading_deg, limit_kph, route=None):
    return None


class ExplodingDB:
  """Raises the way a corrupt database does, and records that it was closed."""

  def __init__(self, exc):
    self.exc = exc
    self.closed = False

  def reload_if_changed(self):
    return False

  def current_link(self, lat, lon, heading_deg=None, route=None):
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
    # update_location's except path calls close(), which must clear MapTargetVelocities --
    # otherwise a bump resolved just before the corruption hit stays published forever.
    self.assertEqual(data.mem_params.values["MapTargetVelocities"], "[]")

    data.update_location()          # and must not reopen it
    self.assertIsNone(data.db)


class BumpTargetTestCase(unittest.TestCase):
  def test_publishes_the_bump_as_a_map_target_velocity(self):
    data = make_bump_data(bump=Bump(lat=37.5010, lon=127.0010, kind=BUMP_ARCH, distance_m=150.))
    data.publish_targets()

    points = json.loads(data.mem_params.values["MapTargetVelocities"])
    self.assertEqual(len(points), 1)
    self.assertAlmostEqual(points[0]["latitude"], 37.5010)
    self.assertAlmostEqual(points[0]["longitude"], 127.0010)
    self.assertAlmostEqual(points[0]["velocity"], 25 * CV.KPH_TO_MS)

    position = json.loads(data.mem_params.values["LastGPSPosition"])
    self.assertAlmostEqual(position["latitude"], 37.5)
    self.assertAlmostEqual(position["longitude"], 127.0)

  def test_trapezoid_bumps_use_the_gentler_target(self):
    data = make_bump_data(bump=Bump(lat=37.5010, lon=127.0010, kind=BUMP_TRAPEZOID, distance_m=150.))
    data.publish_targets()
    points = json.loads(data.mem_params.values["MapTargetVelocities"])
    self.assertAlmostEqual(points[0]["velocity"], 35 * CV.KPH_TO_MS)

  def test_no_bump_ahead_clears_the_param(self):
    """Not merely 'writes nothing' -- SCC-Map reads this every frame, so a stale point
    left behind would keep braking for a bump we already passed."""
    data = make_bump_data(bump=None)
    data.publish_targets()
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])

  def test_the_toggle_being_off_clears_the_param(self):
    data = make_bump_data(bump=Bump(lat=37.5010, lon=127.0010, kind=BUMP_ARCH, distance_m=150.),
                          enabled=False)
    data.publish_targets()
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])

  def test_no_position_publishes_nothing_rather_than_a_zero_coordinate(self):
    data = make_bump_data(bump=Bump(lat=37.5010, lon=127.0010, kind=BUMP_ARCH, distance_m=150.),
                          position=None)
    data.last_position = None
    data.publish_targets()
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])
    self.assertNotIn("LastGPSPosition", data.mem_params.values)

  def test_an_invalid_localizer_clears_the_param_instead_of_republishing_a_stale_bump(self):
    """last_position/last_bearing only update while the localizer is valid (see
    update_location), so a frozen localizer would otherwise leave self.bump resolving to
    the same point every tick forever -- SCC-Map would see a constant distance and never
    release the slowdown, even though the car keeps moving."""
    data = make_bump_data(bump=Bump(lat=37.5010, lon=127.0010, kind=BUMP_ARCH, distance_m=150.),
                          localizer_valid=False)
    data.publish_targets()
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])

  def test_a_virtual_bump_is_never_published_even_if_one_reaches_here(self):
    """next_bump never returns a virtual bump and bump_targets has no key for one either --
    belt and suspenders: publish_targets's own `target > 0.` guard must independently
    refuse to publish one if it ever did reach this far."""
    data = make_bump_data(bump=Bump(lat=37.5010, lon=127.0010, kind=BUMP_VIRTUAL, distance_m=150.))
    data.publish_targets()
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])


class TestReadBumpParams(OpenpilotTestCase):
  """make_bump_data builds bump_targets with the same arch_kph * CV.KPH_TO_MS expression
  read_bump_params uses, so BumpTargetTestCase above cannot catch a wrong param key name or a
  dropped unit conversion -- only a round-trip through the real Params keys can."""

  def test_reads_the_three_params_into_bump_targets(self):
    data = KoreaMapData.__new__(KoreaMapData)
    data.params = Params()
    data.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
    data.params.put("KoreaSpeedBumpArchSpeed", 25, block=True)
    data.params.put("KoreaSpeedBumpTrapezoidSpeed", 35, block=True)

    data.read_bump_params()

    self.assertTrue(data.bump_enabled)
    self.assertEqual(data.bump_targets, {BUMP_ARCH: 25 * CV.KPH_TO_MS, BUMP_TRAPEZOID: 35 * CV.KPH_TO_MS})

  def test_a_too_low_arch_speed_clamps_to_the_floor(self):
    data = KoreaMapData.__new__(KoreaMapData)
    data.params = Params()
    data.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
    data.params.put("KoreaSpeedBumpArchSpeed", 10, block=True)
    data.params.put("KoreaSpeedBumpTrapezoidSpeed", 35, block=True)

    data.read_bump_params()

    self.assertEqual(data.bump_targets[BUMP_ARCH], 20 * CV.KPH_TO_MS)


class StubSM(dict):
  """A dict that also tolerates SubMaster.update(rate): tick() needs both the subscript
  access update_location uses and the update() call tick() makes before it."""

  def update(self, rate):
    pass


class TestTickOrdering(unittest.TestCase):
  def test_tick_runs_publish_targets_after_the_base_tick(self):
    """Pins the ordering the whole safety story depends on: publish_targets must run
    after super().tick() (sm.update, update_location, publish), on every tick -- not from
    inside update_location, whose early return is covered separately below."""
    calls = []
    data = KoreaMapData.__new__(KoreaMapData)
    data.read_bump_params = lambda: calls.append("read_bump_params")
    data.read_camera_params = lambda: calls.append("read_camera_params")
    data.update_destination = lambda: calls.append("update_destination")
    data.sm = SimpleNamespace(update=lambda rate: calls.append("sm.update"))
    data.update_location = lambda: calls.append("update_location")
    data.publish = lambda: calls.append("publish")
    data.route = []  # falsy, so tick()'s curve_targets step is a no-op between publish and publish_targets
    data.publish_targets = lambda: calls.append("publish_targets")

    data.tick()

    self.assertEqual(calls, ["read_bump_params", "read_camera_params", "update_destination", "sm.update",
                             "update_location", "publish", "publish_targets"])

  def test_an_early_return_in_update_location_still_clears_the_target(self):
    """update_location returns early every tick before the database opens, or before the
    localizer has a fix. publish_targets has to run anyway -- moving its call inside
    update_location, after the early return, would leave MapTargetVelocities unwritten (and
    so stale) for that entire window instead of the empty list SCC-Map is safe to read."""
    data = make_data()
    data.sm = StubSM({'liveLocationKalman': messaging.new_message('liveLocationKalman').liveLocationKalman})
    data.pm = SimpleNamespace(send=lambda *a, **k: None)
    data.read_bump_params = lambda: None  # covered by TestReadBumpParams; irrelevant here
    data.read_camera_params = lambda: None  # covered by TestReadCameraParams; irrelevant here
    data.last_bearing = None
    data.last_position = None  # db is also None -- either alone forces the early return

    data.tick()

    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])


class TestMapTargetVelocitiesMerge(unittest.TestCase):
  """The bump feature owns this param today. Curves have to join it, not replace it."""

  def make(self, bump=None, curve_points=(), enabled=True, localizer_valid=True):
    data = make_bump_data(bump=bump, enabled=enabled, localizer_valid=localizer_valid,
                          position=Coordinate(37.5000, 127.0200))
    data.route = []
    data.curve_points = list(curve_points)
    return data

  def read(self, data):
    data.publish_targets()
    return json.loads(data.mem_params.values["MapTargetVelocities"])

  def test_a_bump_alone_is_unchanged(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0217, kind=BUMP_ARCH, distance_m=150.))
    self.assertEqual(self.read(data), [
      {"latitude": 37.5000, "longitude": 127.0217, "velocity": 25 * CV.KPH_TO_MS},
    ])

  def test_a_curve_alone_is_published(self):
    data = self.make(curve_points=[(37.5000, 127.0234, 12.)])
    self.assertEqual(self.read(data), [
      {"latitude": 37.5000, "longitude": 127.0234, "velocity": 12.},
    ])

  def test_both_are_published_nearest_first(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0234, kind=BUMP_ARCH, distance_m=300.),
                     curve_points=[(37.5000, 127.0217, 12.)])
    self.assertEqual([p["longitude"] for p in self.read(data)], [127.0217, 127.0234])

  def test_nothing_ahead_clears_the_param(self):
    self.assertEqual(self.read(self.make()), [])

  def test_a_disabled_bump_does_not_remove_the_curve(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0234, kind=BUMP_ARCH, distance_m=300.),
                     curve_points=[(37.5000, 127.0217, 12.)], enabled=False)
    self.assertEqual(self.read(data), [
      {"latitude": 37.5000, "longitude": 127.0217, "velocity": 12.},
    ])

  def test_an_invalid_localizer_publishes_nothing(self):
    data = self.make(bump=Bump(lat=37.5000, lon=127.0217, kind=BUMP_ARCH, distance_m=150.),
                     curve_points=[(37.5000, 127.0234, 12.)], localizer_valid=False)
    self.assertEqual(self.read(data), [])


def quarter_turn():
  """A 100 m radius quarter turn starting at its first point: curvy enough for curve targets."""
  center_lat, center_lon, radius_m = 37.5000, 127.0200, 100.
  m_per_deg_lat = 111195.
  m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(center_lat))
  return [(center_lat + radius_m * math.sin(math.radians(a)) / m_per_deg_lat,
           center_lon + radius_m * math.cos(math.radians(a)) / m_per_deg_lon) for a in range(0, 95, 5)]


class TestRouteCurveToggle(unittest.TestCase):
  """KoreaRouteCurveEnabled drops the route's curve slowdowns and keeps the route."""

  def curves(self, enabled):
    data = make_data()
    data.route = quarter_turn()
    data.last_position = Coordinate(*data.route[0])
    data.params.values["KoreaRouteCurveEnabled"] = enabled
    data.update_curves()
    return data.curve_points

  def test_on_the_route_its_curves_are_handed_over(self):
    self.assertTrue(self.curves(True))

  def test_switched_off_no_curve_is_handed_over(self):
    self.assertEqual(self.curves(False), [])


class SingleLocationSM:
  """SubMaster stand-in: update() is a no-op and every key is the same location.

  Named apart from the dict-based StubSM above (TestTickOrdering) -- the two are not
  interchangeable (this one ignores the subscript key entirely) and redefining StubSM here
  would silently shadow that one for every test below it in the file.
  """

  def __init__(self, llk):
    self._llk = llk

  def __getitem__(self, key):
    return self._llk

  def update(self, timeout):
    pass


def valid_llk(lat=37.5000, lon=127.0200, heading_deg=90.):
  return SimpleNamespace(
    status=log.LiveLocationKalman.Status.valid,
    gpsOK=True,
    positionGeodetic=SimpleNamespace(valid=True, value=[lat, lon, 0.]),
    calibratedOrientationNED=SimpleNamespace(value=[0., 0., math.radians(heading_deg)]),
  )


class TestRouteReachesTheLookups(unittest.TestCase):
  def test_the_route_is_handed_to_every_lookup(self):
    data = make_data()
    data.sm = SingleLocationSM(valid_llk())
    data.last_position = Coordinate(37.5000, 127.0200)
    data.last_bearing = 90.
    data.curve_points = []
    route = [(37.5000, 127.0200), (37.5000, 127.0320)]
    data.route_source = SimpleNamespace(latest=lambda: route, set_position=lambda lat, lon: None)

    seen = {}
    data.db = SimpleNamespace(
      reload_if_changed=lambda: False,
      current_link=lambda *a, **k: seen.update(link=k.get("route")),
      next_camera=lambda *a, **k: seen.update(camera=k.get("route")),
      next_bump=lambda *a, **k: seen.update(bump=k.get("route")),
      section_starts_near=lambda *a, **k: [],
      section_end_ahead=lambda *a, **k: None,
    )

    data.update_location()
    self.assertEqual(seen["link"], route)
    self.assertEqual(seen["camera"], route)
    self.assertEqual(seen["bump"], route)

  def test_no_route_source_means_an_empty_route(self):
    data = make_data()
    data.sm = SingleLocationSM(valid_llk())
    data.last_position = Coordinate(37.5000, 127.0200)
    data.route_source = None
    data.db = None
    data.route = [(37.5000, 127.0200)]  # a previously-held route must be dropped, not kept
    data.update_location()
    self.assertEqual(data.route, [])

  def test_the_enabled_kinds_reach_next_camera(self):
    data = make_data()
    data.sm = SingleLocationSM(valid_llk())
    data.last_position = Coordinate(37.5000, 127.0200)
    data.camera_kinds = frozenset({CAMERA_ZONE})

    seen = {}
    data.db = SimpleNamespace(
      reload_if_changed=lambda: False,
      current_link=lambda *a, **k: None,
      next_camera=lambda *a, **k: seen.update(kinds=k.get("kinds")),
      next_bump=lambda *a, **k: None,
      section_starts_near=lambda *a, **k: [],
      section_end_ahead=lambda *a, **k: None,
    )

    data.update_location()
    self.assertEqual(seen["kinds"], frozenset({CAMERA_ZONE}))


class TestReadCameraParams(OpenpilotTestCase):
  """Round-trips the real Params keys, like TestReadBumpParams: a wrong key name or a lost
  default only shows up here."""

  def read(self):
    data = KoreaMapData.__new__(KoreaMapData)
    data.params = Params()
    data.read_camera_params()
    return data

  def test_the_defaults_are_every_kind_on_and_50_m(self):
    """get_bool does not fall back to a default -- manager_init writes the defaults into the
    unset params at boot. So the defaults are checked here, and the read separately below."""
    params = Params()
    for key in CAMERA_KIND_PARAMS.values():
      self.assertTrue(params.get_default_value(key), key)
    self.assertEqual(params.get_default_value("KoreaCameraMargin"), 50)

  def test_a_kind_that_is_off_is_left_out(self):
    params = Params()
    for key in CAMERA_KIND_PARAMS.values():
      params.put_bool(key, True, block=True)
    params.put_bool("KoreaCameraZoneEnabled", False, block=True)
    self.assertEqual(self.read().camera_kinds, {CAMERA_SPEED, CAMERA_SIGNAL, CAMERA_SECTION, CAMERA_SECTION_END})

  def test_the_margin_falls_back_to_its_default_and_is_clamped(self):
    self.assertEqual(self.read().camera_margin, 50)  # unset: get_sanitize_int_param reads the default
    Params().put("KoreaCameraMargin", 500, block=True)
    self.assertEqual(self.read().camera_margin, 300)


class TestCameraTarget(unittest.TestCase):
  """The next camera's SCC-Map point: its limit, camera_margin metres short of the camera."""

  CAR = Coordinate(37.5000, 127.0200)
  AT = Coordinate(37.5000, 127.0257)   # ~503 m east

  def camera(self, at=None, limit_kph=50, car=None):
    at = at if at is not None else self.AT
    car = car if car is not None else self.CAR
    return Camera(lat=at.latitude, lon=at.longitude, limit_kph=limit_kph,
                  distance_m=car.distance_to(at), section_m=0, kind=CAMERA_SPEED)

  def publish(self, data):
    data.publish_targets()
    return json.loads(data.mem_params.values["MapTargetVelocities"])

  def test_the_point_sits_the_margin_short_of_the_camera(self):
    camera = self.camera()
    points = self.publish(make_bump_data(camera=camera, margin=50, position=self.CAR))
    self.assertEqual(len(points), 1)
    point = Coordinate(points[0]["latitude"], points[0]["longitude"])
    self.assertAlmostEqual(self.CAR.distance_to(point), camera.distance_m - 50., delta=0.5)
    self.assertAlmostEqual(point.distance_to(self.AT), 50., delta=0.5)
    # the camera's own limit: no speed limit offset on this path
    self.assertAlmostEqual(points[0]["velocity"], 50 * CV.KPH_TO_MS)

  def test_a_zero_margin_puts_the_point_on_the_camera(self):
    points = self.publish(make_bump_data(camera=self.camera(), margin=0, position=self.CAR))
    self.assertAlmostEqual(points[0]["latitude"], self.AT.latitude)
    self.assertAlmostEqual(points[0]["longitude"], self.AT.longitude)

  def test_inside_the_margin_the_point_is_the_car(self):
    """SCC-Map keeps a point at distance zero due, so the limit holds until the camera is passed."""
    near = Coordinate(37.5000, 127.0203)   # ~26 m east, inside a 50 m margin
    points = self.publish(make_bump_data(camera=self.camera(at=near), margin=50, position=self.CAR))
    self.assertAlmostEqual(points[0]["latitude"], self.CAR.latitude)
    self.assertAlmostEqual(points[0]["longitude"], self.CAR.longitude)

  def test_the_camera_joins_the_bump_and_the_curves_nearest_first(self):
    data = make_bump_data(bump=Bump(lat=37.5000, lon=127.0217, kind=BUMP_ARCH, distance_m=150.),
                          camera=self.camera(), position=self.CAR)
    data.curve_points = [(37.5000, 127.0234, 12.)]
    self.assertEqual([p["velocity"] for p in self.publish(data)], [25 * CV.KPH_TO_MS, 12., 50 * CV.KPH_TO_MS])

  def test_an_invalid_localizer_drops_the_camera_too(self):
    data = make_bump_data(camera=self.camera(), position=self.CAR, localizer_valid=False)
    self.assertEqual(self.publish(data), [])

  def test_the_point_stays_put_while_the_car_approaches(self):
    """The point stays where it was placed, so the distance SCC-Map ramps its target down along
    shrinks steadily as the car closes in."""
    data = make_bump_data(camera=self.camera(), margin=50, position=self.CAR)
    first = self.publish(data)[0]
    closer = Coordinate(37.5000, 127.0220)  # ~177 m further east, still outside the margin
    data.last_position = closer
    data.slowdown_camera = self.camera(car=closer)
    second = self.publish(data)[0]
    self.assertEqual((second["latitude"], second["longitude"]), (first["latitude"], first["longitude"]))

  def test_a_camera_met_again_from_the_other_side_is_placed_anew(self):
    """Without dropping the anchor when the camera goes, a return trip would reuse a point on
    the far side of the camera and reach the limit only after passing it."""
    data = make_bump_data(camera=self.camera(), margin=50, position=self.CAR)
    self.publish(data)                      # placed ~50 m west of the camera
    data.slowdown_camera = None
    self.publish(data)                      # the camera went
    east = Coordinate(37.5000, 127.0314)    # ~503 m east of the camera, driving west
    data.last_position = east
    data.slowdown_camera = self.camera(car=east)
    point = self.publish(data)[0]
    self.assertLess(point["longitude"], east.longitude)
    self.assertGreater(point["longitude"], self.AT.longitude)  # between the car and the camera


class TestSideRoadCamera(unittest.TestCase):
  """The sign keeps the wide cone; the slowdown only takes cameras inside the corridor. A
  30 km/h school-zone camera on a side street must not brake a car on the expressway. On a
  route, the route's own corridor takes the heading line's place -- only while the car is on it."""

  # Straight east for ~265 m, then bending north-east. The last-but-one vertex is ~100 m
  # north of the car's heading line: far outside CAMERA_CORRIDOR_M, but on the road.
  BEND = [(37.5000, 127.0200), (37.5000, 127.0230), (37.5003, 127.0245), (37.5009, 127.0260),
          (37.5018, 127.0270)]

  def publish_with(self, cameras, route=None):
    tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
    cams, links = str(tmp / "korea_cameras.sqlite"), str(tmp / "korea_links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, cameras))
    write_db(links, SCHEMA_LINKS,
             lambda con: insert_links(con, [(80, "올림픽대로", [(37.5000, 127.0200), (37.5000, 127.0320)])]))
    data = make_data()
    if route is not None:
      data.route_source = SimpleNamespace(latest=lambda: route, set_position=lambda lat, lon: None)
    data.db = KoreaMapDB(cams, links)
    self.addCleanup(data.db.close)
    data.sm = SingleLocationSM(valid_llk(37.5000, 127.0200, heading_deg=90.))
    data.last_position = Coordinate(37.5000, 127.0200)
    data.update_location()
    data.publish_targets()
    return data

  def test_a_side_street_camera_shows_on_the_sign_but_does_not_brake(self):
    data = self.publish_with([(37.5009, 127.0228, 30, 0, CAMERA_ZONE)])  # ~250 m ahead, ~100 m north
    self.assertAlmostEqual(data.get_next_speed_limit_and_distance()[0], 30 * CV.KPH_TO_MS)
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])

  def test_a_camera_on_the_road_still_brakes(self):
    data = self.publish_with([(37.5000, 127.0228, 30, 0, CAMERA_ZONE)])  # ~250 m straight ahead
    points = json.loads(data.mem_params.values["MapTargetVelocities"])
    self.assertEqual([p["velocity"] for p in points], [30 * CV.KPH_TO_MS])

  def test_on_the_route_a_camera_round_the_bend_brakes(self):
    data = self.publish_with([(37.5009, 127.0260, 30, 0, CAMERA_ZONE)], route=self.BEND)
    points = json.loads(data.mem_params.values["MapTargetVelocities"])
    self.assertEqual([p["velocity"] for p in points], [30 * CV.KPH_TO_MS])

  def test_on_the_route_a_side_street_camera_still_does_not_brake(self):
    straight = [(37.5000, 127.0200), (37.5000, 127.0320)]
    data = self.publish_with([(37.5009, 127.0228, 30, 0, CAMERA_ZONE)], route=straight)
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])

  def test_off_the_route_a_camera_on_the_road_we_left_does_not_brake(self):
    """RouteSource keeps the old polyline until a reroute succeeds, which with no signal is
    minutes. Here the car has left it for a road ~100 m south: the old road's camera is on
    that route but not on ours."""
    left_behind = [(37.5009, 127.0200), (37.5009, 127.0320)]
    data = self.publish_with([(37.5009, 127.0228, 30, 0, CAMERA_ZONE)], route=left_behind)
    self.assertEqual(json.loads(data.mem_params.values["MapTargetVelocities"]), [])


class TestCurvesOnlyOnTheRoute(unittest.TestCase):
  """The curves follow the slowdown camera's rule: the route's curves count only while the car
  is on the route. RouteSource keeps a polyline the car has left until a reroute succeeds --
  3 s at best, minutes with no signal, and again every time Tesla's nav and TMAP disagree about
  the way. A curve on that road is not on ours, and slowing for it is a phantom brake."""

  # ~97 m east, then ~100 m north: a corner worth ~12 m/s.
  CORNER = [(37.5000, 127.0200), (37.5000, 127.0211), (37.5009, 127.0211)]

  def curves_at(self, lat, lon):
    data = make_data()
    data.read_bump_params = lambda: None
    data.read_camera_params = lambda: None
    data.update_destination = lambda: None
    data.sm = SimpleNamespace(update=lambda rate: None)
    data.update_location = lambda: None  # keeps the route and position set below
    data.publish = lambda: None
    data.publish_targets = lambda: None
    data.route = self.CORNER
    data.last_position = Coordinate(lat, lon)
    data.tick()
    return data.curve_points

  def test_on_the_route_the_corner_ahead_is_a_target(self):
    self.assertEqual([(lat, lon) for lat, lon, _ in self.curves_at(37.5000, 127.0200)], [(37.5000, 127.0211)])

  def test_off_the_route_its_corner_is_not_a_target(self):
    self.assertEqual(self.curves_at(37.4991, 127.0200), [])  # ~100 m south, on a road the route left


class TestSection(unittest.TestCase):
  START = Camera(lat=37.6000, lon=127.0000, limit_kph=100, distance_m=4., section_m=0, kind=CAMERA_SECTION)
  END = Camera(lat=37.6000, lon=127.0600, limit_kph=100, distance_m=5286., section_m=0, kind=CAMERA_SECTION_END)
  START2 = Camera(lat=37.6000, lon=127.0600, limit_kph=100, distance_m=4., section_m=0, kind=CAMERA_SECTION)
  END2 = Camera(lat=37.6000, lon=127.1200, limit_kph=100, distance_m=5286., section_m=0, kind=CAMERA_SECTION_END)
  ALONG = [(37.6000, 126.9990), (37.6000, 127.1300)]  # a TMAP route along the road
  NOTHING = SimpleNamespace(section_starts_near=lambda lat, lon: [], section_end_ahead=lambda *a, **k: None)

  def make(self, starts=(), end=None):
    data = make_data()
    data.last_bearing = 90.
    data.db = SimpleNamespace(section_starts_near=lambda lat, lon: list(starts),
                              section_end_ahead=lambda lat, lon, heading, limit, route=None: end)
    return data

  def test_a_start_with_an_end_ahead_opens_the_section(self):
    data = self.make(starts=[self.START], end=self.END)
    data.update_section(37.6000, 127.0000)
    self.assertEqual(data.section.limit_kph, 100)
    self.assertAlmostEqual(data.section.max_travel_m, 5286. * 1.5 + 2000.)

  def test_a_start_without_an_end_ahead_opens_nothing(self):
    data = self.make(starts=[self.START], end=None)
    data.update_section(37.6000, 127.0000)
    self.assertIsNone(data.section)

  def test_passing_the_end_camera_closes_the_section(self):
    data = self.make(starts=[self.START], end=self.END)
    data.update_section(37.6000, 127.0000)
    data.db = self.NOTHING
    data.update_section(37.6000, 127.0300)
    self.assertIsNotNone(data.section)
    data.update_section(37.6000, 127.0600)
    self.assertIsNone(data.section)

  def test_driving_far_past_the_expected_length_gives_up(self):
    short = Camera(lat=37.6000, lon=127.0600, limit_kph=100, distance_m=100., section_m=0, kind=CAMERA_SECTION_END)
    data = self.make(starts=[self.START], end=short)
    data.update_section(37.6000, 127.0000)  # may drive 100 * 1.5 + 2000 = 2150 m
    data.db = self.NOTHING
    data.update_section(37.6200, 127.0000)  # ~2.2 km north, nowhere near the end camera
    self.assertIsNone(data.section)

  def test_section_cameras_off_means_no_section(self):
    data = self.make(starts=[self.START], end=self.END)
    data.camera_kinds = frozenset()
    data.update_section(37.6000, 127.0000)
    self.assertIsNone(data.section)

  def pass_the_end(self, route=(), start_between=False, start2=None):
    """Open the section, then reach its end camera, where a same-limit start camera stands (beside it
    unless start2 says otherwise) and another end camera of that limit lies ahead. Returns the data and
    the first section."""
    start2 = start2 or self.START2
    data = self.make(starts=[self.START], end=self.END)
    data.route = list(route)
    data.update_section(37.6000, 127.0000)
    first = data.section
    data.db = SimpleNamespace(section_starts_near=lambda lat, lon: [start2],
                              section_end_ahead=lambda lat, lon, heading, limit, route=None: self.END2,
                              section_start_between=lambda lat, lon, heading, end, route, passed: start_between)
    data.update_section(37.6000, 127.0600)
    return data, first

  def test_on_a_route_a_section_starting_where_the_last_one_ends_gets_its_own_start(self):
    data, first = self.pass_the_end(route=self.ALONG)
    self.assertIsNot(data.section, first)
    self.assertEqual(data.section.end_lon, 127.1200)
    self.assertGreater(data.section.started, first.started)

  def test_off_a_route_a_start_beside_the_end_just_passed_opens_nothing(self):
    # most likely the other direction's start: the end it finds ahead belongs to another section
    data, _ = self.pass_the_end()
    self.assertIsNone(data.section)

  def test_off_a_route_the_start_across_the_road_just_before_the_end_opens_nothing(self):
    # the other direction's start, 66 m before our end camera and 29 m across the road: 72 m from it
    across = Camera(lat=37.60026, lon=127.05925, limit_kph=100, distance_m=30., section_m=0, kind=CAMERA_SECTION)
    data, _ = self.pass_the_end(start2=across)
    self.assertIsNone(data.section)

  def test_off_a_route_a_start_past_the_end_still_opens(self):
    # a section that begins 100 m after ours ends
    past = Camera(lat=37.6000, lon=127.06113, limit_kph=100, distance_m=100., section_m=0, kind=CAMERA_SECTION)
    data, _ = self.pass_the_end(start2=past)
    self.assertIsNotNone(data.section)

  def test_on_a_route_a_start_in_between_keeps_the_start_beside_the_end_from_opening(self):
    data, _ = self.pass_the_end(route=self.ALONG, start_between=True)
    self.assertIsNone(data.section)

  def test_a_start_beside_an_end_left_well_behind_opens_again(self):
    # back the other way later in the drive: the other direction's section opens there as usual
    data, _ = self.pass_the_end()
    stub = data.db
    data.db = self.NOTHING
    data.update_section(37.6000, 127.0630)  # ~260 m on: no start beside the end is in reach from here
    data.db = stub
    data.update_section(37.6000, 127.0600)
    self.assertIsNotNone(data.section)

  def test_opening_and_closing_are_logged_without_where(self):
    with mock.patch("openpilot.sunnypilot.mapd.live_map_data.korea_map_data.cloudlog") as log:
      data = self.make(starts=[self.START], end=self.END)
      data.update_section(37.6000, 127.0000)
      data.db = self.NOTHING
      data.update_section(37.6000, 127.0600)
    lines = [call.args[0] % call.args[1:] for call in log.info.call_args_list]
    self.assertEqual(len(lines), 2, lines)
    self.assertIn("opened", lines[0])
    self.assertIn("passed its end camera", lines[1])
    for line in lines:
      self.assertIn("100 km/h", line)
      self.assertNotIn("37.6", line)
      self.assertNotIn("127.", line)

  def test_every_other_way_a_section_ends_is_logged_with_its_reason(self):
    short = Camera(lat=37.6000, lon=127.0600, limit_kph=100, distance_m=100., section_m=0, kind=CAMERA_SECTION_END)

    def drive_past_its_length(data):
      data.update_section(37.6200, 127.0000)  # ~2.2 km north, past 100 * 1.5 + 2000 m

    def turn_the_cameras_off(data):
      data.camera_kinds = frozenset()
      data.update_section(37.6000, 127.0010)

    def close_the_source(data):
      data.db = None
      data.close()

    for reason, end_it in (("drove past its length", drive_past_its_length), ("section cameras off", turn_the_cameras_off),
                           ("map source closed", close_the_source)):
      with self.subTest(reason), mock.patch("openpilot.sunnypilot.mapd.live_map_data.korea_map_data.cloudlog") as log:
        data = self.make(starts=[self.START], end=short)
        data.update_section(37.6000, 127.0000)
        data.db = self.NOTHING
        end_it(data)
        self.assertIsNone(data.section)
        last = log.info.call_args_list[-1].args
        self.assertIn(f"({reason})", last[0] % last[1:])

  def test_the_section_goes_to_shared_memory(self):
    data = self.make(starts=[self.START], end=self.END)
    data.last_position = Coordinate(37.6000, 127.0000)
    data.update_section(37.6000, 127.0000)
    data.publish_targets()
    self.assertAlmostEqual(data.mem_params.values["KoreaSectionSpeedLimit"], 100 * CV.KPH_TO_MS)
    self.assertGreater(data.mem_params.values["KoreaSectionStart"], 0.)
    self.assertEqual(data.mem_params.values["KoreaSectionStart"], data.section.started)
    data.section = None
    data.publish_targets()
    self.assertEqual(data.mem_params.values["KoreaSectionSpeedLimit"], 0.)
    self.assertEqual(data.mem_params.values["KoreaSectionStart"], 0.)

  def test_closing_the_source_drops_the_section(self):
    """A database error or a source switch closes the source. SpeedLimitAssist polls the section
    params and has no idea who last wrote them, so the last section must not stay there."""
    data = self.make(starts=[self.START], end=self.END)
    data.update_section(37.6000, 127.0000)
    data.db = None  # close() also runs before the database has opened
    data.close()
    self.assertIsNone(data.section)
    self.assertEqual(data.mem_params.values["KoreaSectionSpeedLimit"], 0.)
    self.assertEqual(data.mem_params.values["KoreaSectionStart"], 0.)


class TestSectionAtItsEnd(unittest.TestCase):
  """update_section over a real database, driving east at about 35 m a tick (1 Hz at 125 km/h) through a
  100 km/h section whose end has the westbound start beside it, with another 100 km/h section's end
  further east. Off a route nothing opens at our end; on a route a section to that end opens there
  unless a start lies in between."""
  CAMERAS = [
    (37.6000, 127.0000, 100, 0, CAMERA_SECTION),
    (37.6000, 127.0600, 100, 0, CAMERA_SECTION_END),
    (37.60001, 127.06001, 100, 0, CAMERA_SECTION),      # westbound start, beside our end
    (37.60001, 127.00001, 100, 0, CAMERA_SECTION_END),  # westbound end, beside our start
    (37.6000, 127.2100, 100, 0, CAMERA_SECTION_END),    # another 100 km/h section's end, ~13 km east of ours
  ]
  NEXT_START = (37.6000, 127.1500, 100, 0, CAMERA_SECTION)  # that section's start
  ALONG = [(37.6000, 126.9900), (37.6000, 127.3000)]

  def drive(self, cameras, route=()):
    """The end camera of the open section at each tick, by longitude; None for no section."""
    tmp = self.enterContext(tempfile.TemporaryDirectory())
    cams, links = os.path.join(tmp, "cameras.sqlite"), os.path.join(tmp, "links.sqlite")
    write_db(cams, SCHEMA_CAMERAS, lambda con: insert_cameras(con, cameras))
    write_db(links, SCHEMA_LINKS, lambda con: insert_links(con, [(100, "road", self.ALONG)]))
    data = make_data()
    data.db = KoreaMapDB(cams, links)
    self.addCleanup(data.db.close)
    data.last_bearing = 90.
    data.route = list(route)
    seen = {}
    for i in range(560):
      lon = round(126.9990 + i * 0.0004, 4)
      data.update_section(37.6000, lon)
      seen[lon] = data.section.end_lon if data.section is not None else None
    return seen

  @staticmethod
  def ends_open(seen, west, east):
    return {end for lon, end in seen.items() if west < lon < east and end is not None}

  def test_off_a_route_nothing_opens_at_the_end(self):
    seen = self.drive(self.CAMERAS)
    self.assertEqual(self.ends_open(seen, 127.0000, 127.0588), {127.0600})
    self.assertEqual(self.ends_open(seen, 127.0596, 127.2200), set())

  def test_off_a_route_nothing_opens_at_an_end_with_the_westbound_start_across_the_road_before_it(self):
    # the westbound start 66 m before our end camera and 29 m across the road, 72 m from it
    cameras = [*self.CAMERAS[:2], (37.60026, 127.05925, 100, 0, CAMERA_SECTION), *self.CAMERAS[3:]]
    seen = self.drive(cameras)
    self.assertEqual(self.ends_open(seen, 127.0000, 127.0588), {127.0600})
    self.assertEqual(self.ends_open(seen, 127.0596, 127.2200), set())

  def test_on_a_route_a_section_starting_at_the_end_opens_there(self):
    seen = self.drive(self.CAMERAS, route=self.ALONG)
    self.assertEqual(self.ends_open(seen, 127.0596, 127.2048), {127.2100})

  def test_the_next_section_opens_at_its_own_start(self):
    for name, route in (("off a route", ()), ("on a route", self.ALONG)):
      with self.subTest(name):
        seen = self.drive(self.CAMERAS + [self.NEXT_START], route=route)
        self.assertEqual(self.ends_open(seen, 127.0596, 127.1492), set())
        self.assertEqual(self.ends_open(seen, 127.1496, 127.2048), {127.2100})
