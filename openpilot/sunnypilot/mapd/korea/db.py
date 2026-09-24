"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

db: read-only lookups against the sqlite file build_db.py produces. Deliberately
free of openpilot imports so it runs under a bare Python interpreter.
"""
import logging
import math
import os
import sqlite3
import struct
from dataclasses import dataclass

from openpilot.sunnypilot.mapd.korea.geo import bearing, bearing_delta, haversine, point_segment_distance
from openpilot.sunnypilot.mapd.korea.route import distance_to_route

# Shared by all three databases (cameras, links, bumps) -- there is no independent version
# per kind. Bumping this to add a camera/link schema change also invalidates every existing
# bumps file; that mismatch is now tolerated (see KoreaMapDB.__init__), not fatal.
SCHEMA_VERSION = "1"

# ~78 m box; wide enough for GPS error, narrow enough to keep the candidate list short
LINK_SEARCH_DEG = 0.0007
LINK_MAX_DISTANCE_M = 40.
# a link running more than this far off our heading is a crossing road, not ours
LINK_BEARING_TOLERANCE = 60.

# Geometry is stored as float32, which quantises to about 0.3 m, so anything inside this
# is the same place as far as this database can tell. Measured: 0.1 m captures the
# genuinely coincident cases (overpasses, shared nodes) without merging roads that are
# actually a few metres apart.
TIE_DISTANCE_M = 0.1

# 과속방지턱형태구분, as build_db.classify_kind maps it. Defined here rather than in
# build_db because the dependency has to run builder -> runtime: db.py and
# korea_map_data.py read these at runtime, and importing build_db to get them would drag
# PC-only build tooling onto the device.
BUMP_ARCH = 0       # 원호형 -- a rounded hump, the harshest of the three
BUMP_TRAPEZOID = 1  # 사다리꼴형 -- flat-topped, gentler at the same speed
BUMP_VIRTUAL = 2    # 가상방지턱 -- road markings only, no physical rise

# 단속 종류, as build_db.classify_camera maps it. Here rather than in build_db for the same
# builder -> runtime reason as BUMP_* above. A camera has exactly one kind; classify_camera
# says which one wins when it is several things at once.
CAMERA_SPEED = 0    # 과속 only -- and any code this build does not recognise
CAMERA_SIGNAL = 1   # 신호·과속, the multi-function intersection cameras
CAMERA_SECTION = 2  # the start or end camera of a 구간단속 section
CAMERA_ZONE = 3     # inside a 노인/어린이 보호구역, whatever else it enforces

# ~3.3 km box, then filtered down to CAMERA_MAX_DISTANCE_M
CAMERA_SEARCH_DEG = 0.03
CAMERA_MAX_DISTANCE_M = 2000.
CAMERA_AHEAD_TOLERANCE = 60.

# ~660 m box, then filtered down to BUMP_MAX_DISTANCE_M. Much tighter than the camera
# horizon on purpose: a bump 2 km out is noise, and SCC-Map only needs the point once it
# is inside braking range.
BUMP_SEARCH_DEG = 0.006
BUMP_MAX_DISTANCE_M = 400.
# Tighter than CAMERA_AHEAD_TOLERANCE because the same lateral offset subtends a much
# larger angle at 400 m than at 2000 m -- 60 degrees here would pull in bumps on a side
# street. The cost is that a bump just past a sharp bend is picked up later, which is
# where the driver already is without this feature.
BUMP_AHEAD_TOLERANCE = 45.
# A +-45 deg cone spans +-71 m at 100 m -- wider than a city block, so the cone alone
# returns bumps on parallel side streets. The corridor is what keeps the lookup on the
# road the car is actually on. Measured on 강남대로: 13 of 31 sample points would brake
# without it, 6 with it. 20 m rather than 10 m because a 10 m corridor rejects every bump
# once the localizer's lateral error passes 10 m, which is ordinary in an urban canyon.
BUMP_CORRIDOR_M = 20.

# Tighter than OFF_ROUTE_M (50 m, route.py): that one asks 'is the route still right', this
# asks 'is this camera on our road'. A parallel road is typically 20-40 m away, so the
# corridor has to be narrower than the gap it is meant to reject. 30 m still clears the
# localizer's lateral error in an urban canyon, which is what forced BUMP_CORRIDOR_M to 20
# rather than 10.
ROUTE_CORRIDOR_M = 30.

_RTREE_OVERLAP = "WHERE i.maxlat >= ? AND i.minlat <= ? AND i.maxlon >= ? AND i.minlon <= ?"


@dataclass(frozen=True)
class Link:
  max_spd: int  # km/h
  name: str


@dataclass(frozen=True)
class Camera:
  """A speed camera ahead of us.

  section_m is raw provenance carried through from 과속단속구간길이, not a usable
  distance: see the cameras table's schema comment in build_db.py (only ~2.6% of rows
  populate it, units are inconsistent between submitting agencies, and 99999 is a
  sentinel, not a real value). Never feed it to a distance calculation.
  """
  limit_kph: int
  distance_m: float
  section_m: int  # 0 for a point camera, >0 for 구간단속 -- see docstring, not metres


@dataclass(frozen=True)
class Bump:
  """A speed bump ahead of us.

  Carries the coordinate, not just the distance: SmartCruiseControlMap consumes a
  (latitude, longitude, velocity) point and measures the distance itself.

  kind is 과속방지턱형태구분 as build_db.classify_kind mapped it -- 0 arch, 1 trapezoid,
  2 virtual. Virtual bumps are stored but never returned by next_bump; they are paint on
  the road, so there is nothing to slow down for.
  """
  lat: float
  lon: float
  kind: int
  distance_m: float


def mtime_or_none(path: str | None) -> float | None:
  """When `path` was last written, or None for a file that is not there.

  None is distinct from any real mtime, so a file that appears later reads as changed.
  Never raises: every caller is on a 1 Hz path that must not take the process down over a
  file that vanished between two stat calls.
  """
  try:
    return os.path.getmtime(path) if path else None
  except OSError:
    return None


def _unpack_geom(blob: bytes) -> list[tuple[float, float]]:
  count = len(blob) // 8
  flat = struct.unpack(f"<{2 * count}f", blob)
  return [(flat[i], flat[i + 1]) for i in range(0, 2 * count, 2)]


def _check_schema_version(con: sqlite3.Connection, path: str) -> None:
  row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
  if row is None or row[0] != SCHEMA_VERSION:
    raise ValueError(f"{path}: schema {row and row[0]!r} != {SCHEMA_VERSION!r}")


def verify(path: str, table: str, min_rows: int) -> int:
  """Open the database the way the device will and confirm it is worth using.

  Raises sqlite3.DatabaseError if the file is not a database, ValueError if the schema
  version is wrong or the table is too short.

  Lives here rather than in deploy.py because both the PC-side deploy check and the
  on-device downloader need it, and the dependency has to run builder -> runtime: a
  runtime module importing deploy.py would drag PC-only tooling onto the device.
  """
  con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
  try:
    _check_schema_version(con, path)
    count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if count < min_rows:
      raise ValueError(f"{path}: {count} rows in {table}, expected at least {min_rows}")
    return count
  finally:
    con.close()


class KoreaMapDB:
  """Read-only lookups against the camera, link and (optional) bump databases.

  Not thread-safe: reload_if_changed() swaps self.cam, self.lnk or self.bmp, so the caller
  must call it from the same thread as the queries. check_same_thread=False below is there
  so the connection can be opened in one place and used from whichever thread ends up
  owning this object, not to license calling into it concurrently from multiple threads.
  """

  def __init__(self, cameras_path: str, links_path: str, bumps_path: str | None = None):
    self.cameras_path = cameras_path
    self.links_path = links_path
    self.bumps_path = bumps_path
    self._last_link_id: int | None = None
    # Two connections, not one attached database: Task 13 replaces the camera file
    # underneath us with os.replace while this process keeps running, and reopening one
    # connection must not disturb the 220 MB link database that never changes.
    # Sample every mtime BEFORE opening anything, not after: opening the 220 MB link
    # database takes long enough that a swap can land in between, and recording a mtime
    # against the old inode afterwards would leave reload_if_changed permanently satisfied
    # -- stale reads for the life of the process. Sampling early can only cause one
    # redundant reload.
    self._cameras_mtime = mtime_or_none(cameras_path)
    self._links_mtime = mtime_or_none(links_path)
    self._bumps_mtime = mtime_or_none(bumps_path)
    self.cam = self._open(cameras_path)
    self.lnk = self._open(links_path)
    # Optional third file. A device deployed before speed bumps shipped has cameras and
    # links but no korea_bumps.sqlite, and losing speed limits over a missing comfort
    # feature would be the wrong trade -- so an absent file means "no bumps", not an error.
    # A file that IS present but unusable (schema mismatch, an scp interrupted mid-copy)
    # must not be an error either: _open raising here would propagate out of __init__ and
    # take cameras and links down with it over a database this feature calls optional.
    self.bmp = None
    if bumps_path and os.path.exists(bumps_path):
      try:
        self.bmp = self._open(bumps_path)
      except (sqlite3.Error, ValueError):
        logging.getLogger(__name__).exception("korea db: ignoring an unusable bump database")

  @staticmethod
  def _open(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    try:
      _check_schema_version(con, path)
    except Exception:
      con.close()
      raise
    return con

  def reload_if_changed(self) -> bool:
    """Reopen any of the three databases that was replaced on disk. True if any was.

    camera_refresh rewrites the camera file weekly and map_download rewrites the link and
    bump files, both with os.replace -- which leaves this process reading the old inode
    forever unless we notice. Three stats per call is cheap at 1 Hz; a failed reopen keeps
    the old connection rather than leaving the caller with nothing.
    """
    reloaded = False
    for path, mtime_attr, con_attr in (
      (self.cameras_path, "_cameras_mtime", "cam"),
      (self.links_path, "_links_mtime", "lnk"),
      (self.bumps_path, "_bumps_mtime", "bmp"),
    ):
      if self._reload_one(path, mtime_attr, con_attr):
        reloaded = True
    return reloaded

  def _reload_one(self, path: str, mtime_attr: str, con_attr: str) -> bool:
    if not path:
      return False
    try:
      mtime = os.path.getmtime(path)
    except OSError:
      return False
    if mtime == getattr(self, mtime_attr):
      return False

    try:
      con = self._open(path)
    except (sqlite3.Error, ValueError):
      # stdlib logging on purpose: openpilot's cloudlog pulls in zmq, and this module has
      # to stay importable under a bare interpreter so its tests run without the device stack.
      logging.getLogger(__name__).exception("korea db: keeping the old %s database", con_attr)
      setattr(self, mtime_attr, mtime)  # don't retry the same bad file every tick
      return False

    old = getattr(self, con_attr)
    if old is not None:
      old.close()
    setattr(self, con_attr, con)
    setattr(self, mtime_attr, mtime)
    if con_attr == "lnk":
      # The builder assigns link ids by insertion order, so the same id names a different
      # road after a rebuild. Inside the tie band a surviving id would pick that road.
      self._last_link_id = None
    return True

  def close(self) -> None:
    # Every connection gets its close() attempted even if an earlier one raises: the link
    # database is 220 MB of mapped sqlite, and leaking it because the camera handle
    # objected would be the expensive half of the failure.
    for con in (self.cam, self.lnk, self.bmp):
      if con is None:
        continue
      try:
        con.close()
      except sqlite3.Error:
        logging.getLogger(__name__).exception("korea db: a connection failed to close")

  def current_link(self, lat: float, lon: float, heading_deg: float | None = None) -> Link | None:
    """Nearest road segment we are plausibly driving on, or None when off the network.

    Ties (an overpass, a ramp, two links meeting at a node -- all within TIE_DISTANCE_M
    of each other) are broken toward the higher max_spd: guessing low makes speed limit
    assist brake for a limit that does not apply, guessing high only means it assists
    less, which is where the driver already is without this feature. The previous match
    is held only while it stays tied with the nearest candidate, so the pick does not
    flap between tied links frame to frame; it releases as soon as a clearly nearer link
    appears, not merely when it falls out of LINK_MAX_DISTANCE_M or fails the heading check.
    """
    rows = self.lnk.execute(
      "SELECT l.id, l.max_spd, l.name, l.geom FROM links_idx i JOIN links l ON l.id = i.id " + _RTREE_OVERLAP,
      (lat - LINK_SEARCH_DEG, lat + LINK_SEARCH_DEG, lon - LINK_SEARCH_DEG, lon + LINK_SEARCH_DEG),
    ).fetchall()

    # Collect every link in range with its own closest distance BEFORE deciding anything.
    # A running minimum made the outcome depend on the order sqlite returned rows in,
    # which let a stale sticky link outrank a clearly nearer road.
    candidates: dict[int, tuple[float, int, str]] = {}
    for link_id, max_spd, name, blob in rows:
      points = _unpack_geom(blob)
      for (alat, alon), (blat, blon) in zip(points, points[1:], strict=False):
        if heading_deg is not None and not _heading_matches(heading_deg, alat, alon, blat, blon):
          continue
        distance = point_segment_distance(lat, lon, alat, alon, blat, blon)
        if distance >= LINK_MAX_DISTANCE_M:
          continue
        previous = candidates.get(link_id)
        if previous is None or distance < previous[0]:
          candidates[link_id] = (distance, max_spd, name)

    if not candidates:
      self._last_link_id = None
      return None

    nearest = min(distance for distance, _, _ in candidates.values())
    # Everything inside the tie band is the same place as far as float32 geometry can tell.
    tied = {link_id: value for link_id, value in candidates.items()
            if value[0] <= nearest + TIE_DISTANCE_M}

    if self._last_link_id in tied:
      # Hold the previous match, but only against links it is genuinely tied with.
      # Anything beyond the tie band is a different road and must outrank stickiness.
      best_id = self._last_link_id
    else:
      # Prefer the higher limit among tied candidates, nearest first as a deterministic
      # tiebreak so the result never depends on row order.
      best_id = max(tied, key=lambda link_id: (tied[link_id][1], -tied[link_id][0]))

    self._last_link_id = best_id
    _, max_spd, name = candidates[best_id]
    return Link(max_spd=max_spd, name=name)

  def next_camera(self, lat: float, lon: float, heading_deg: float | None,
                  route: list[tuple[float, float]] | None = None) -> Camera | None:
    """Nearest speed camera ahead of us, or None. Needs a heading to know what 'ahead' means.

    With a route, 'ahead' stops being a bearing cone and becomes the road we will actually
    drive: the cone alone accepts a camera on the far side of a fork, which is the single
    most visible wrong slowdown this database produces.
    """
    if heading_deg is None:
      return None

    rows = self.cam.execute(
      "SELECT c.lat, c.lon, c.limit_kph, c.section_m FROM cameras_idx i JOIN cameras c ON c.id = i.id " + _RTREE_OVERLAP,
      (lat - CAMERA_SEARCH_DEG, lat + CAMERA_SEARCH_DEG, lon - CAMERA_SEARCH_DEG, lon + CAMERA_SEARCH_DEG),
    ).fetchall()

    best: Camera | None = None

    for clat, clon, limit_kph, section_m in rows:
      distance = haversine(lat, lon, clat, clon)
      if distance > CAMERA_MAX_DISTANCE_M or (best is not None and distance >= best.distance_m):
        continue
      if not _on_path(lat, lon, clat, clon, heading_deg, route, CAMERA_AHEAD_TOLERANCE):
        continue
      best = Camera(limit_kph=limit_kph, distance_m=distance, section_m=section_m)

    return best

  def next_bump(self, lat: float, lon: float, heading_deg: float | None,
                route: list[tuple[float, float]] | None = None) -> Bump | None:
    """Nearest physical speed bump ahead of us, or None. Same route rule as next_camera."""
    if self.bmp is None or heading_deg is None:
      return None

    rows = self.bmp.execute(
      "SELECT b.lat, b.lon, b.kind FROM bumps_idx i JOIN bumps b ON b.id = i.id " + _RTREE_OVERLAP,
      (lat - BUMP_SEARCH_DEG, lat + BUMP_SEARCH_DEG, lon - BUMP_SEARCH_DEG, lon + BUMP_SEARCH_DEG),
    ).fetchall()

    best: Bump | None = None

    for blat, blon, kind in rows:
      if kind == BUMP_VIRTUAL:
        continue
      distance = haversine(lat, lon, blat, blon)
      if distance > BUMP_MAX_DISTANCE_M or (best is not None and distance >= best.distance_m):
        continue
      if not _on_path(lat, lon, blat, blon, heading_deg, route, BUMP_AHEAD_TOLERANCE,
                      corridor_m=BUMP_CORRIDOR_M):
        continue
      best = Bump(lat=blat, lon=blon, kind=kind, distance_m=distance)

    return best


def _heading_matches(heading_deg: float, alat: float, alon: float, blat: float, blon: float) -> bool:
  """A two-way road may be digitised in either direction, so accept the reverse too."""
  segment = bearing(alat, alon, blat, blon)
  forward = bearing_delta(heading_deg, segment)
  backward = bearing_delta(heading_deg, (segment + 180.) % 360.)
  return min(forward, backward) <= LINK_BEARING_TOLERANCE


def _on_path(lat: float, lon: float, tlat: float, tlon: float, heading_deg: float,
             route: list[tuple[float, float]] | None, tolerance_deg: float,
             corridor_m: float | None = None) -> bool:
  """Is the target ahead of us on the road we are driving?

  The route NARROWS the existing test, never widens it. Every check that runs without a
  route still runs with one, and the corridor is added on top. Ordered that way on purpose:
  ROUTE_CORRIDOR_M (30 m) is looser than BUMP_CORRIDOR_M (20 m), so letting the route
  replace the corridor would start accepting bumps on parallel streets that are rejected
  today -- a regression dressed up as a feature.

  The bearing cone stays in the with-route case too, because a route that doubles back (a
  U-turn, a loop ramp) passes within the corridor of a point we already drove past.
  """
  delta = bearing_delta(heading_deg, bearing(lat, lon, tlat, tlon))
  if delta > tolerance_deg:
    return False

  if corridor_m is not None and \
     haversine(lat, lon, tlat, tlon) * math.sin(math.radians(delta)) > corridor_m:
    return False

  if route:
    return distance_to_route(route, tlat, tlon) <= ROUTE_CORRIDOR_M

  return True
