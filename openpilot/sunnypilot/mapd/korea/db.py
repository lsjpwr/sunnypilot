"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

db: read-only lookups against the sqlite file build_db.py produces. Deliberately
free of openpilot imports so it runs under a bare Python interpreter.
"""
import logging
import os
import sqlite3
import struct
from dataclasses import dataclass

from openpilot.sunnypilot.mapd.korea.geo import bearing, bearing_delta, haversine, point_segment_distance

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

# ~3.3 km box, then filtered down to CAMERA_MAX_DISTANCE_M
CAMERA_SEARCH_DEG = 0.03
CAMERA_MAX_DISTANCE_M = 2000.
CAMERA_AHEAD_TOLERANCE = 60.

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


def _unpack_geom(blob: bytes) -> list[tuple[float, float]]:
  count = len(blob) // 8
  flat = struct.unpack(f"<{2 * count}f", blob)
  return [(flat[i], flat[i + 1]) for i in range(0, 2 * count, 2)]


class KoreaMapDB:
  """Read-only lookups against the camera and link databases.

  Not thread-safe: reload_if_changed() swaps self.cam, so the caller must call it from
  the same thread as the queries. check_same_thread=False below is there so the
  connection can be opened in one place and used from whichever thread ends up owning
  this object, not to license calling into it concurrently from multiple threads.
  """

  def __init__(self, cameras_path: str, links_path: str):
    self.cameras_path = cameras_path
    self._cameras_mtime = 0.
    self._last_link_id: int | None = None
    # Two connections, not one attached database: Task 13 replaces the camera file
    # underneath us with os.replace while this process keeps running, and reopening one
    # connection must not disturb the 220 MB link database that never changes.
    self.cam = self._open(cameras_path)
    self.lnk = self._open(links_path)
    self._cameras_mtime = os.path.getmtime(cameras_path)

  @staticmethod
  def _open(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
      con.close()
      raise ValueError(f"{path}: schema {row and row[0]!r} != {SCHEMA_VERSION!r}")
    return con

  def reload_if_changed(self) -> bool:
    """Reopen the camera database if it was replaced on disk. Returns True if it was.

    Task 13 refreshes cameras with os.replace, which leaves this process reading the old
    inode forever unless we notice. One stat per call is cheap at 1 Hz; a failed reopen
    keeps the old connection rather than leaving the caller with nothing.
    """
    try:
      mtime = os.path.getmtime(self.cameras_path)
    except OSError:
      return False
    if mtime == self._cameras_mtime:
      return False

    try:
      con = self._open(self.cameras_path)
    except (sqlite3.Error, ValueError):
      # stdlib logging on purpose: openpilot's cloudlog pulls in zmq, and this module has
      # to stay importable under a bare interpreter so its tests run without the device stack.
      logging.getLogger(__name__).exception("korea db: keeping the old camera database")
      self._cameras_mtime = mtime  # don't retry the same bad file every tick
      return False

    self.cam.close()
    self.cam = con
    self._cameras_mtime = mtime
    return True

  def close(self) -> None:
    try:
      self.cam.close()
    finally:
      self.lnk.close()

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

  def next_camera(self, lat: float, lon: float, heading_deg: float | None) -> Camera | None:
    """Nearest speed camera ahead of us, or None. Needs a heading to know what 'ahead' means."""
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
      if bearing_delta(heading_deg, bearing(lat, lon, clat, clon)) > CAMERA_AHEAD_TOLERANCE:
        continue
      best = Camera(limit_kph=limit_kph, distance_m=distance, section_m=section_m)

    return best


def _heading_matches(heading_deg: float, alat: float, alon: float, blat: float, blon: float) -> bool:
  """A two-way road may be digitised in either direction, so accept the reverse too."""
  segment = bearing(alat, alon, blat, blon)
  forward = bearing_delta(heading_deg, segment)
  backward = bearing_delta(heading_deg, (segment + 180.) % 360.)
  return min(forward, backward) <= LINK_BEARING_TOLERANCE
