"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

korea_map_data: fills liveMapDataSP from the Korean public datasets, replacing the prior map source.
Everything downstream -- SpeedLimitAssist, the onroad speed limit widget, DEC --
reads liveMapDataSP and needs no change.

Current speed limits come from ITS 표준노드링크 MAX_SPD. The "next" speed limit is the next
speed camera ahead, and on liveMapDataSP it only feeds the speed-limit-ahead sign:
SpeedLimitResolver ignores speedLimitAhead in Korea mode. The slowdown goes to
SmartCruiseControlMap instead, as one more MapTargetVelocities point beside the bumps and
the curves (publish_targets). SpeedLimitAssist would hold it behind a confirmation prompt
under 80 km/h and add the speed limit offset on top.
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
from openpilot.sunnypilot.mapd.korea.db import (BUMP_ARCH, BUMP_TRAPEZOID, CAMERA_KIND_PARAMS, Bump, Camera,
                                                 KoreaMapDB, Link, mtime_or_none)
from openpilot.sunnypilot.mapd.korea.external_source import ExternalNav, ExternalNavSource
from openpilot.sunnypilot.mapd.korea.route import RouteSource, curve_targets
from openpilot.sunnypilot.mapd.live_map_data.base_map_data import BaseMapData, MAX_SPEED_LIMIT
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

# m, how far before a camera to be at its limit. Bounds, not the default -- that lives in
# params_keys.h, like the bump speeds.
CAMERA_MARGIN_RANGE = (0, 300)


class KoreaMapData(BaseMapData):
  def __init__(self, cameras_path: str = KOREA_CAMERAS_PATH, links_path: str = KOREA_LINKS_PATH,
               bumps_path: str = KOREA_BUMPS_PATH, external: ExternalNavSource | None = None,
               route_source: RouteSource | None = None):
    super().__init__()
    self.cameras_path = cameras_path
    self.links_path = links_path
    self.bumps_path = bumps_path
    self.db: KoreaMapDB | None = None
    self.open_failed = False
    self._failed_mtimes: tuple[float | None, ...] = ()
    self.external = external
    self.route_source = route_source
    # Last destination this object wrote to NavDestination, or None. An optimisation only --
    # so a phone that repeats the same destination every datagram does not touch /data/params
    # (flash) once a second forever -- not the source of truth. The param is: it is cleared by
    # two parties this object hears nothing about (CLEAR_ON_OFFROAD_TRANSITION wipes it when
    # the drive ends; RouteSource._loop removes it on arrival), and korea_main does not rebuild
    # this object on either event, so this cache can say "unchanged" while the param itself is
    # already empty.
    self._last_written_destination: tuple[float, float] | None = None
    self.route: list[tuple[float, float]] = []
    self.curve_points: list[tuple[float, float, float]] = []
    self.link: Link | None = None
    self.camera: Camera | None = None
    self.bump: Bump | None = None
    # SmartCruiseControlMap reads its input from /dev/shm, not from the message bus.
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.bump_enabled = False
    self.bump_targets: dict[int, float] = {}
    # read_camera_params overwrites both on the first tick, before anything reads them
    self.camera_kinds: frozenset[int] = frozenset(CAMERA_KIND_PARAMS)
    self.camera_margin = 0

  def _db_mtimes(self) -> tuple[float | None, ...]:
    """When each database file was last written; None for one that is not there."""
    return tuple(mtime_or_none(p)
                 for p in (self.cameras_path, self.links_path, self.bumps_path))

  def _give_up(self) -> None:
    """Stop retrying, but remember which files we gave up on."""
    self.open_failed = True
    self._failed_mtimes = self._db_mtimes()

  def open_db(self) -> None:
    """Opened lazily: the process still runs, publishing nothing, until both files land.

    A file that is not there yet is not a failure -- the user is still copying it, so we
    keep looking every tick. A file that IS there and will not open (schema mismatch,
    corruption) never fixes itself, so we log once and stop: retrying at 1 Hz would do
    nothing but fill the log until the next restart.

    "Never fixes itself" stopped being true when map_download shipped: it installs a good
    database over the broken one with os.replace while this process runs. So the giving-up
    is held only against the files we actually failed on -- once one of them has been
    written again, the next tick tries the new bytes rather than waiting for a reboot.
    """
    if self.db is not None:
      return
    if self.open_failed:
      if self._db_mtimes() == self._failed_mtimes:
        return
      self.open_failed = False
    if not (os.path.exists(self.cameras_path) and os.path.exists(self.links_path)):
      return
    try:
      self.db = KoreaMapDB(self.cameras_path, self.links_path, self.bumps_path)
      cloudlog.info("korea_map: opened %s + %s (bumps: %s)", self.cameras_path, self.links_path,
                    "yes" if self.db.bmp is not None else "no")
    except Exception:
      self._give_up()
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

  def update_destination(self) -> None:
    """Copy a destination from the socket into the param the route thread reads.

    The socket's TTL is 5 s (external_source.py:35, checked at :124) and a destination is
    sent once, so it cannot live there. The param is the one place both writers -- this
    socket and athenad's setNavDestination RPC -- agree on, which is why the JSON shape is
    athenad's.

    Written when it changes (or when the param has been cleared out from under this object --
    see _last_written_destination above), and removed only on an explicit end-of-guidance
    signal -- this loop runs forever at 1 Hz and NavDestination lives on flash. nav.destination
    alone cannot drive that: ExternalNav.destination is None both for "this datagram carried no
    destination keys" and for "the phone sent 0,0 (or a point outside Korea)", and those need
    opposite responses. So key presence in nav.raw -- not the parsed value -- is what tells a
    repeated no-op apart from the phone saying guidance ended.
    """
    nav = self.nav()
    if nav is None:
      return
    if "destination_lat" not in nav.raw and "destination_lon" not in nav.raw:
      # Nothing about the destination in this datagram: leave the param exactly as it is,
      # whether that means untouched or already cleared.
      return
    if nav.destination is None:
      # Keys were present but parsed to None: 0/0 or a coordinate outside Korea, i.e. the
      # phone's explicit end-of-guidance signal, not "nothing new this tick". Clearing the
      # remembered value too means a later re-send of the same destination writes again.
      if self.params.get("NavDestination"):
        self.params.remove("NavDestination")
      self._last_written_destination = None
    elif nav.destination != self._last_written_destination or not self.params.get("NavDestination"):
      # The "or" covers a destination unchanged from this object's point of view but cleared
      # by someone else since -- the manager on an offroad transition, or RouteSource on
      # arrival -- which otherwise left the param empty for the rest of that drive (Fix 3).
      self.params.put("NavDestination", json.dumps({
        "latitude": nav.destination[0], "longitude": nav.destination[1],
        "place_name": nav.road_name or None, "place_details": None,
      }))
      self._last_written_destination = nav.destination

  def update_location(self) -> None:
    location = self.sm['liveLocationKalman']
    self.localizer_valid = (location.status == log.LiveLocationKalman.Status.valid) and location.positionGeodetic.valid

    if self.localizer_valid:
      self.last_bearing = math.degrees(location.calibratedOrientationNED.value[2]) % 360.
      self.last_position = Coordinate(location.positionGeodetic.value[0], location.positionGeodetic.value[1])

    self.link = None
    self.camera = None
    self.bump = None

    # Kept apart from open_db() below on purpose: the route thread must keep tracking our
    # position, and self.route must stay current, even while the camera/link database is
    # still being copied down or has just been dropped after a corrupt read -- neither is a
    # reason to feed the lookups (and next_camera/next_bump, gated on self.db below) a route
    # that stopped following the driver.
    self.route = self.route_source.latest() if self.route_source is not None else []
    if self.route_source is not None and self.localizer_valid:
      self.route_source.set_position(self.last_position.latitude, self.last_position.longitude)

    self.open_db()
    if self.db is None or self.last_position is None:
      return

    # Task 13 swaps the camera file underneath us with os.replace. This is the only place
    # that notices; drop it and a refresh never reaches the running process.
    self.db.reload_if_changed()

    lat, lon = self.last_position.latitude, self.last_position.longitude
    try:
      self.link = self.db.current_link(lat, lon, self.last_bearing)
      self.camera = self.db.next_camera(lat, lon, self.last_bearing, route=self.route, kinds=self.camera_kinds)
      self.bump = self.db.next_bump(lat, lon, self.last_bearing, route=self.route)
    except Exception:
      # Deliberately broad. A corrupt page raises sqlite3.DatabaseError, but a truncated
      # geometry blob raises struct.error from _unpack_geom -- not a sqlite exception at
      # all -- and anything that escapes here reaches mapd_manager's bare `while True`,
      # kills the process, and raises processNotRunning, which blocks engagement. Whatever
      # the corruption is, it raises on every query from here on, so retrying at 1 Hz would
      # only crash-loop. Drop the database and keep publishing zeros: no speed limit is a
      # safe answer, a dead mapd is a worse one.
      self._give_up()
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
    # here means publish_targets would fall through to "no target" if one ever arrived.
    self.bump_targets = {BUMP_ARCH: arch * CV.KPH_TO_MS, BUMP_TRAPEZOID: trapezoid * CV.KPH_TO_MS}

  def read_camera_params(self) -> None:
    """Same 1 Hz as read_bump_params, for the same reason."""
    self.camera_kinds = frozenset(kind for kind, key in CAMERA_KIND_PARAMS.items() if self.params.get_bool(key))
    self.camera_margin = get_sanitize_int_param("KoreaCameraMargin", *CAMERA_MARGIN_RANGE, self.params)

  def camera_point(self) -> tuple[float, float, float] | None:
    """The next camera as an SCC-Map target: its limit, camera_margin metres short of it.

    The point sits on the straight line from the car to the camera. SCC-Map measures
    straight-line distance too, so the car reaches the limit about camera_margin metres
    before the camera as the crow flies -- on a winding road that is a little early, never
    late. Inside the margin the point is the car's own position: SCC-Map keeps treating a
    point at distance zero as due, so the car stays near the limit until next_camera lets
    the camera go.

    No speed limit offset. SpeedLimitAssist adds one to road limits; a camera enforces its
    own number.
    """
    if self.camera is None or self.last_position is None:
      return None
    camera, car = self.camera, self.last_position
    share = max(0., camera.distance_m - self.camera_margin) / camera.distance_m if camera.distance_m > 0. else 0.
    return (car.latitude + (camera.lat - car.latitude) * share,
            car.longitude + (camera.lon - car.longitude) * share,
            camera.limit_kph * CV.KPH_TO_MS)

  def publish_targets(self) -> None:
    """Hand the next bump, the next camera AND the curves ahead to SmartCruiseControlMap.

    One writer, not two: SCC-Map reads the whole list from this one param, so a second
    writer would delete the first one's points every tick. The bump feature shipped first
    and owned the param alone -- it now shares it.

    Written on every tick, cleared when there is nothing ahead. SCC-Map has no staleness
    check of its own -- it trusts whatever is in the param -- so 'stop writing' is not a
    way to turn this off; only an empty list is.

    Also requires localizer_valid: last_position/last_bearing only update while the
    localizer is valid (see update_location), so a localizer that stops updating would
    otherwise freeze self.bump and self.camera (and republish self.curve_points against a
    stale car position) forever -- the car keeps moving, SCC-Map keeps seeing a constant
    distance, and the slowdown never releases.
    """
    points: list[tuple[float, float, float]] = []
    if self.localizer_valid and self.last_position is not None:
      if self.bump_enabled and self.bump is not None:
        target = self.bump_targets.get(self.bump.kind, 0.)
        if target > 0.:
          points.append((self.bump.lat, self.bump.lon, target))
      camera = self.camera_point()
      if camera is not None:
        points.append(camera)
      points.extend(self.curve_points)
      points.sort(key=lambda p: self.last_position.distance_to(Coordinate(p[0], p[1])))

    self.mem_params.put("MapTargetVelocities", json.dumps(
      [{"latitude": lat, "longitude": lon, "velocity": velocity} for lat, lon, velocity in points]))
    if self.last_position is not None:
      self.mem_params.put("LastGPSPosition", json.dumps(self.last_position.as_dict()))

  def tick(self) -> None:
    """Override rather than calling from update_location: update_location returns early on
    every tick before the database opens, and the clearing write has to happen anyway."""
    self.read_bump_params()
    self.read_camera_params()
    self.update_destination()
    super().tick()
    self.curve_points = []
    if self.route and self.last_position is not None and self.localizer_valid:
      # The road's own limit, not the set speed: mapd never sees the set speed, and
      # SmartCruiseControlMap already refuses to act on a target above it
      # (map_controller.py:161,239). A curve target above the posted limit is noise either way.
      v_max = self.get_current_speed_limit() or MAX_SPEED_LIMIT
      self.curve_points = curve_targets(self.route, self.last_position.latitude,
                                        self.last_position.longitude, v_max)
    self.publish_targets()
