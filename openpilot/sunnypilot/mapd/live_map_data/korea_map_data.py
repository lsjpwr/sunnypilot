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
import json
import math
import os
import platform

from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot import get_sanitize_int_param
from openpilot.sunnypilot.mapd.korea.db import BUMP_ARCH, BUMP_TRAPEZOID, Bump, Camera, KoreaMapDB, Link
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNav, ExternalNavSource
from openpilot.sunnypilot.mapd.live_map_data.base_map_data import BaseMapData
from openpilot.sunnypilot.navd.helpers import Coordinate

KOREA_MAP_DIR = Paths.korea_map_root()
KOREA_CAMERAS_PATH = os.path.join(KOREA_MAP_DIR, "korea_cameras.sqlite")
KOREA_LINKS_PATH = os.path.join(KOREA_MAP_DIR, "korea_links.sqlite")
KOREA_BUMPS_PATH = os.path.join(KOREA_MAP_DIR, "korea_bumps.sqlite")

# km/h. Bounds, not defaults -- the defaults live in params_keys.h. The floor is
# SmartCruiseControl's MIN_V (20 km/h): the controller clamps anything lower up to 20, so
# the setting would silently do nothing.
BUMP_ARCH_SPEED_RANGE = (20, 40)
BUMP_TRAPEZOID_SPEED_RANGE = (20, 50)


class KoreaMapData(BaseMapData):
  def __init__(self, cameras_path: str = KOREA_CAMERAS_PATH, links_path: str = KOREA_LINKS_PATH,
               bumps_path: str = KOREA_BUMPS_PATH, external: ExternalNavSource | None = None):
    super().__init__()
    self.cameras_path = cameras_path
    self.links_path = links_path
    self.bumps_path = bumps_path
    self.db: KoreaMapDB | None = None
    self.open_failed = False
    self.external = external
    self.link: Link | None = None
    self.camera: Camera | None = None
    self.bump: Bump | None = None
    # SmartCruiseControlMap reads its input from /dev/shm, not from the message bus.
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.bump_enabled = False
    self.bump_targets: dict[int, float] = {}

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
      self.db = KoreaMapDB(self.cameras_path, self.links_path, self.bumps_path)
      cloudlog.info("korea_map: opened %s + %s (bumps: %s)", self.cameras_path, self.links_path,
                    "yes" if self.db.bmp is not None else "no")
    except Exception:
      self.open_failed = True
      cloudlog.exception("korea_map: giving up on %s + %s", self.cameras_path, self.links_path)

  def close(self) -> None:
    """Release the sqlite handles and forget the last match.

    open_db() is lazy, so self.db is None for every tick before both files land: a source
    switch in that window must not take the process down with an AttributeError. Does not
    touch self.external -- korea_main owns that one and stops it itself.
    """
    if self.db is not None:
      self.db.close()
    self.db = None
    self.link = None
    self.camera = None
    self.bump = None
    # The source is going away. SCC-Map polls this param every frame and has no idea who
    # last wrote it, so a point left here would be acted on by whatever runs next.
    self.mem_params.put("MapTargetVelocities", "[]")

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
    self.bump = None

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
      self.bump = self.db.next_bump(lat, lon, self.last_bearing)
    except Exception:
      # Deliberately broad. A corrupt page raises sqlite3.DatabaseError, but a truncated
      # geometry blob raises struct.error from _unpack_geom -- not a sqlite exception at
      # all -- and anything that escapes here reaches mapd_manager's bare `while True`,
      # kills the process, and raises processNotRunning, which blocks engagement. Whatever
      # the corruption is, it raises on every query from here on, so retrying at 1 Hz would
      # only crash-loop. Drop the database and keep publishing zeros: no speed limit is a
      # safe answer, a dead mapd is a worse one.
      self.open_failed = True
      self.close()
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

  def read_bump_params(self) -> None:
    """Read at the 1 Hz tick rate rather than on a frame counter: this process ticks once
    a second, so a counter would only add state to save nothing."""
    self.bump_enabled = self.params.get_bool("KoreaSpeedBumpEnabled")
    arch = get_sanitize_int_param("KoreaSpeedBumpArchSpeed", *BUMP_ARCH_SPEED_RANGE, self.params)
    trapezoid = get_sanitize_int_param("KoreaSpeedBumpTrapezoidSpeed", *BUMP_TRAPEZOID_SPEED_RANGE, self.params)
    # BUMP_VIRTUAL is deliberately absent: next_bump never returns one, and a missing key
    # here means publish_bump_target would fall through to "no target" if one ever arrived.
    self.bump_targets = {BUMP_ARCH: arch * CV.KPH_TO_MS, BUMP_TRAPEZOID: trapezoid * CV.KPH_TO_MS}

  def publish_bump_target(self) -> None:
    """Hand the next bump to SmartCruiseControlMap through the two mem_params it reads.

    Written on every tick, cleared when there is nothing ahead. SCC-Map has no staleness
    check of its own -- it trusts whatever is in the param -- so 'stop writing' is not a
    way to turn this off; only an empty list is.

    Also requires localizer_valid: last_position/last_bearing only update while the
    localizer is valid (see update_location), so a localizer that stops updating would
    otherwise freeze self.bump at whatever it last resolved to and republish that same
    point forever -- the car keeps moving, SCC-Map keeps seeing a constant distance, and
    the slowdown never releases.
    """
    points: list[dict[str, float]] = []
    if self.bump_enabled and self.bump is not None and self.last_position is not None and self.localizer_valid:
      target = self.bump_targets.get(self.bump.kind, 0.)
      if target > 0.:
        points = [{"latitude": self.bump.lat, "longitude": self.bump.lon, "velocity": target}]

    self.mem_params.put("MapTargetVelocities", json.dumps(points))
    if self.last_position is not None:
      self.mem_params.put("LastGPSPosition", json.dumps(self.last_position.as_dict()))

  def tick(self) -> None:
    """Override rather than calling from update_location: update_location returns early on
    every tick before the database opens, and the clearing write has to happen anyway."""
    self.read_bump_params()
    super().tick()
    self.publish_bump_target()
