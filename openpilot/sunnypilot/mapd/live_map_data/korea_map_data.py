"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

korea_map_data: fills liveMapDataSP from the Korean public datasets, replacing the prior map source.
Everything downstream -- SpeedLimitAssist, the onroad speed limit widget, DEC --
reads liveMapDataSP and needs no change.

Current speed limits come from ITS 표준노드링크 MAX_SPD. The "next" speed limit is
the next speed camera ahead: SpeedLimitAssist already slows for speedLimitAhead at
speedLimitAheadDistance, which is exactly what a camera calls for.
"""
import math
import os
import sqlite3

from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.mapd.korea.db import Camera, KoreaMapDB, Link
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNav, ExternalNavSource
from openpilot.sunnypilot.mapd.live_map_data.base_map_data import BaseMapData
from openpilot.sunnypilot.navd.helpers import Coordinate

KOREA_MAP_DIR = Paths.korea_map_root()
KOREA_CAMERAS_PATH = os.path.join(KOREA_MAP_DIR, "korea_cameras.sqlite")
KOREA_LINKS_PATH = os.path.join(KOREA_MAP_DIR, "korea_links.sqlite")


class KoreaMapData(BaseMapData):
  def __init__(self, cameras_path: str = KOREA_CAMERAS_PATH, links_path: str = KOREA_LINKS_PATH,
               external: ExternalNavSource | None = None):
    super().__init__()
    self.cameras_path = cameras_path
    self.links_path = links_path
    self.db: KoreaMapDB | None = None
    self.open_failed = False
    self.external = external
    self.link: Link | None = None
    self.camera: Camera | None = None

  def open_db(self) -> None:
    """Opened lazily: the process still runs, publishing nothing, until both files land.

    A file that is not there yet is not a failure -- the user is still copying it, so we
    keep looking every tick. A file that IS there and will not open (schema mismatch,
    corruption) never fixes itself, so we log once and stop: retrying at 1 Hz would do
    nothing but fill the log until the next restart.
    """
    if self.db is not None or self.open_failed:
      return
    if not (os.path.exists(self.cameras_path) and os.path.exists(self.links_path)):
      return
    try:
      self.db = KoreaMapDB(self.cameras_path, self.links_path)
      cloudlog.info("korea_map: opened %s + %s", self.cameras_path, self.links_path)
    except Exception:
      self.open_failed = True
      cloudlog.exception("korea_map: giving up on %s + %s", self.cameras_path, self.links_path)

  def nav(self) -> ExternalNav | None:
    return self.external.latest() if self.external is not None else None

  def update_location(self) -> None:
    location = self.sm['liveLocationKalman']
    self.localizer_valid = (location.status == log.LiveLocationKalman.Status.valid) and location.positionGeodetic.valid

    if self.localizer_valid:
      self.last_bearing = math.degrees(location.calibratedOrientationNED.value[2]) % 360.
      self.last_position = Coordinate(location.positionGeodetic.value[0], location.positionGeodetic.value[1])

    self.link = None
    self.camera = None

    self.open_db()
    if self.db is None or self.last_position is None:
      return

    # Task 13 swaps the camera file underneath us with os.replace. This is the only place
    # that notices; drop it and a refresh never reaches the running process.
    self.db.reload_if_changed()

    lat, lon = self.last_position.latitude, self.last_position.longitude
    try:
      self.link = self.db.current_link(lat, lon, self.last_bearing)
      self.camera = self.db.next_camera(lat, lon, self.last_bearing)
    except sqlite3.DatabaseError:
      # A corrupt page raises on every query from here on, so retrying at 1 Hz would only
      # crash-loop the process. Drop the database and keep publishing zeros: no speed limit
      # is a safe answer, a dead mapd is a worse one.
      self.open_failed = True
      self.db.close()
      self.db = None
      self.link = None
      self.camera = None
      cloudlog.exception("korea_map: dropping the database after a query error")

  def get_current_speed_limit(self) -> float:
    nav = self.nav()
    if nav is not None and nav.speed_limit_kph > 0.:
      return nav.speed_limit_kph * CV.KPH_TO_MS
    if self.link is not None and self.link.max_spd > 0:
      return self.link.max_spd * CV.KPH_TO_MS
    return 0.

  def get_next_speed_limit_and_distance(self) -> tuple[float, float]:
    nav = self.nav()
    if nav is not None and nav.next_speed_limit_kph > 0.:
      return nav.next_speed_limit_kph * CV.KPH_TO_MS, nav.next_speed_limit_distance_m
    if self.camera is not None:
      return self.camera.limit_kph * CV.KPH_TO_MS, self.camera.distance_m
    return 0., 0.

  def get_current_road_name(self) -> str:
    nav = self.nav()
    if nav is not None and nav.road_name:
      return nav.road_name
    return self.link.name if self.link is not None else ""
