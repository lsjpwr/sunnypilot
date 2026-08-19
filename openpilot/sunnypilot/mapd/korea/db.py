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
  limit_kph: int
  distance_m: float
  section_m: int  # 0 for a point camera, >0 for 구간단속


def _unpack_geom(blob: bytes) -> list[tuple[float, float]]:
  count = len(blob) // 8
  flat = struct.unpack(f"<{2 * count}f", blob)
  return [(flat[i], flat[i + 1]) for i in range(0, 2 * count, 2)]


class KoreaMapDB:
  def __init__(self, cameras_path: str, links_path: str):
    self.cameras_path = cameras_path
    self._cameras_mtime = 0.
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
    self.cam.close()
    self.lnk.close()

  def current_link(self, lat: float, lon: float, heading_deg: float | None = None) -> Link | None:
    """Nearest road segment we are plausibly driving on, or None when off the network."""
    rows = self.lnk.execute(
      "SELECT l.max_spd, l.name, l.geom FROM links_idx i JOIN links l ON l.id = i.id " + _RTREE_OVERLAP,
      (lat - LINK_SEARCH_DEG, lat + LINK_SEARCH_DEG, lon - LINK_SEARCH_DEG, lon + LINK_SEARCH_DEG),
    ).fetchall()

    best: Link | None = None
    best_distance = LINK_MAX_DISTANCE_M

    for max_spd, name, blob in rows:
      points = _unpack_geom(blob)
      for (alat, alon), (blat, blon) in zip(points, points[1:], strict=False):
        distance = point_segment_distance(lat, lon, alat, alon, blat, blon)
        if distance >= best_distance:
          continue
        if heading_deg is not None and not _heading_matches(heading_deg, alat, alon, blat, blon):
          continue
        best_distance = distance
        best = Link(max_spd=max_spd, name=name)

    return best

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
