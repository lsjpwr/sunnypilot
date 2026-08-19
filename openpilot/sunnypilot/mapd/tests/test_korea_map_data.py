"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time

from openpilot.common.constants import CV
from openpilot.sunnypilot.mapd.korea.build_db import (SCHEMA_CAMERAS, SCHEMA_LINKS, insert_cameras,
                                                      insert_links, write_db)
from openpilot.sunnypilot.mapd.korea.db import Camera, Link
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNav
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import KoreaMapData


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


def test_no_data_publishes_zero():
  data = make_data()
  assert data.get_current_speed_limit() == 0.
  assert data.get_next_speed_limit_and_distance() == (0., 0.)
  assert data.get_current_road_name() == ""


def test_link_speed_limit_is_converted_to_ms():
  data = make_data(link=Link(max_spd=60, name="테헤란로"))
  assert abs(data.get_current_speed_limit() - 60 * CV.KPH_TO_MS) < 1e-9
  assert data.get_current_road_name() == "테헤란로"


def test_camera_becomes_the_next_speed_limit():
  data = make_data(camera=Camera(limit_kph=50, distance_m=320., section_m=0))
  limit, distance = data.get_next_speed_limit_and_distance()
  assert abs(limit - 50 * CV.KPH_TO_MS) < 1e-9
  assert distance == 320.


def test_external_source_wins_over_the_database():
  nav = ExternalNav(speed_limit_kph=40., next_speed_limit_kph=30.,
                    next_speed_limit_distance_m=150., road_name="시장길",
                    received_at=time.monotonic())
  data = make_data(link=Link(max_spd=60, name="테헤란로"),
                   camera=Camera(limit_kph=50, distance_m=320., section_m=0),
                   external=StubExternal(nav))
  assert abs(data.get_current_speed_limit() - 40 * CV.KPH_TO_MS) < 1e-9
  assert abs(data.get_next_speed_limit_and_distance()[0] - 30 * CV.KPH_TO_MS) < 1e-9
  assert data.get_next_speed_limit_and_distance()[1] == 150.
  assert data.get_current_road_name() == "시장길"


def test_stale_external_source_falls_back_to_the_database():
  data = make_data(link=Link(max_spd=60, name="테헤란로"), external=StubExternal(None))
  assert abs(data.get_current_speed_limit() - 60 * CV.KPH_TO_MS) < 1e-9
  assert data.get_current_road_name() == "테헤란로"


def test_empty_external_fields_fall_back_per_field():
  # an app that only knows the road name must not blank out the database speed limit
  nav = ExternalNav(road_name="시장길", received_at=time.monotonic())
  data = make_data(link=Link(max_spd=60, name="테헤란로"), external=StubExternal(nav))
  assert abs(data.get_current_speed_limit() - 60 * CV.KPH_TO_MS) < 1e-9
  assert data.get_current_road_name() == "시장길"


def test_open_db_is_a_noop_when_a_file_is_missing(tmp_path):
  """Both files must be present. Half a database is not a usable database."""
  cameras = str(tmp_path / "korea_cameras.sqlite")
  write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0)]))

  data = make_data()
  data.cameras_path = cameras
  data.links_path = str(tmp_path / "does_not_exist.sqlite")
  data.open_db()
  assert data.db is None
  assert not data.open_failed  # a missing file is "not yet copied", not a failure


def test_open_db_opens_both_files(tmp_path):
  cameras = str(tmp_path / "korea_cameras.sqlite")
  links = str(tmp_path / "korea_links.sqlite")
  write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0)]))
  write_db(links, SCHEMA_LINKS,
           lambda con: insert_links(con, [(60, "테헤란로", [(37.5000, 127.0200), (37.5000, 127.0320)])]))

  data = make_data()
  data.cameras_path = cameras
  data.links_path = links
  data.open_db()
  assert data.db is not None
  assert data.db.current_link(37.5000, 127.0260).max_spd == 60
  data.db.close()


def test_open_db_gives_up_after_a_bad_database(tmp_path):
  """A schema mismatch never fixes itself; retrying at 1 Hz would only flood the log."""
  cameras = str(tmp_path / "korea_cameras.sqlite")
  links = str(tmp_path / "korea_links.sqlite")
  write_db(cameras, SCHEMA_CAMERAS, lambda con: insert_cameras(con, [(37.5000, 127.0257, 50, 0)]))
  with open(links, "wb") as f:
    f.write(b"not a database")

  data = make_data()
  data.cameras_path = cameras
  data.links_path = links
  data.open_db()
  assert data.db is None
  assert data.open_failed

  # a second call must not even try again
  data.cameras_path = "/nonexistent/tripwire"
  data.open_db()
  assert data.db is None
