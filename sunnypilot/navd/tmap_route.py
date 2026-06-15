"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

tmap_route: fetches a route from the TMAP open API (free tier: one call per
destination plus occasional re-routes), parses the GeoJSON response into a
polyline with maneuver points, and projects the vehicle position onto it.
"""
import math

import requests

ROUTE_URL = "https://apis.openapi.sk.com/tmap/routes?version=1"
REQUEST_TIMEOUT = 10.  # s

# officially documented TMAP turnType codes for sharp maneuvers
TURN_TYPES_SHARP = {12, 13, 14, 16, 17, 18, 19}  # left, right, u-turn, 8/10/2/4 o'clock
# ramp classification is keyword-based: turnType codes for highway entries/exits are
# not reliably documented, the guidance description is
RAMP_KEYWORDS = ("고속도로", "도시고속", "진입", "진출", "램프", " IC", " JC")

EARTH_R = 6371000.


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  p1, p2 = math.radians(lat1), math.radians(lat2)
  dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
  a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
  return 2 * EARTH_R * math.asin(math.sqrt(a))


def fetch_route(api_key: str, start_lat: float, start_lon: float,
                dest_lat: float, dest_lon: float, dest_name: str = "destination") -> dict:
  """Single TMAP car route API call. Raises on HTTP or API errors."""
  resp = requests.post(
    ROUTE_URL,
    headers={"appKey": api_key, "Content-Type": "application/json"},
    json={
      "startX": str(start_lon), "startY": str(start_lat),
      "endX": str(dest_lon), "endY": str(dest_lat),
      "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
      "startName": "origin", "endName": dest_name,
    },
    timeout=REQUEST_TIMEOUT,
  )
  resp.raise_for_status()
  return resp.json()


class RoutePlan:
  """Polyline with cumulative distances and maneuver points along it."""

  def __init__(self, geojson: dict):
    self.points: list[tuple[float, float]] = []  # (lat, lon)
    self.cumdist: list[float] = []
    self.maneuvers: list[tuple[float, str]] = []  # (distance along route, 'turn' | 'ramp')
    self.total_distance = 0.
    self._last_idx = 0
    self._parse(geojson)

  def _append_point(self, lat: float, lon: float) -> None:
    if self.points:
      last = self.points[-1]
      d = haversine(last[0], last[1], lat, lon)
      if d < 0.5:  # skip duplicates between consecutive linestrings
        return
      self.cumdist.append(self.cumdist[-1] + d)
    else:
      self.cumdist.append(0.)
    self.points.append((lat, lon))

  @staticmethod
  def _classify(props: dict) -> str | None:
    turn_type = props.get("turnType", 0)
    if turn_type in TURN_TYPES_SHARP:
      return "turn"
    desc = props.get("description", "") or ""
    if any(k in desc for k in RAMP_KEYWORDS):
      return "ramp"
    return None

  def _parse(self, geojson: dict) -> None:
    pending: list[tuple[float, float, str]] = []  # maneuver coords awaiting cumdist
    for feature in geojson.get("features", []):
      geom = feature.get("geometry", {})
      props = feature.get("properties", {})
      if geom.get("type") == "LineString":
        for lon, lat in geom.get("coordinates", []):
          self._append_point(lat, lon)
      elif geom.get("type") == "Point":
        kind = self._classify(props)
        if kind is not None:
          lon, lat = geom.get("coordinates", [0., 0.])
          pending.append((lat, lon, kind))
        if "totalDistance" in props:
          self.total_distance = float(props["totalDistance"])

    # locate each maneuver point on the assembled polyline
    for lat, lon, kind in pending:
      best_d, best_i = float('inf'), None
      for i, (plat, plon) in enumerate(self.points):
        d = haversine(plat, plon, lat, lon)
        if d < best_d:
          best_d, best_i = d, i
      if best_i is not None and best_d < 30.:
        self.maneuvers.append((self.cumdist[best_i], kind))
    self.maneuvers.sort()

  @staticmethod
  def _project_to_segment(lat: float, lon: float, p1: tuple[float, float],
                          p2: tuple[float, float]) -> tuple[float, float]:
    """Project onto the segment using a local equirectangular approximation.
    Returns (fraction along segment 0..1, perpendicular distance in m)."""
    coslat = math.cos(math.radians(lat))
    ax = (p1[1] - lon) * coslat * 111320.
    ay = (p1[0] - lat) * 111320.
    bx = (p2[1] - lon) * coslat * 111320.
    by = (p2[0] - lat) * 111320.
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-6:
      return 0., math.hypot(ax, ay)
    t = max(0., min(1., -(ax * dx + ay * dy) / seg_len_sq))
    px, py = ax + t * dx, ay + t * dy
    return t, math.hypot(px, py)

  def locate(self, lat: float, lon: float) -> tuple[float, float]:
    """Project the position onto the polyline near the last known index.
    Returns (distance along route, offset from route in m)."""
    if len(self.points) < 2:
      return 0., float('inf')

    def search(lo: int, hi: int) -> tuple[float, float, int]:
      best = (float('inf'), 0., lo)  # (offset, along, segment index)
      for i in range(lo, hi):
        t, off = self._project_to_segment(lat, lon, self.points[i], self.points[i + 1])
        if off < best[0]:
          along = self.cumdist[i] + t * (self.cumdist[i + 1] - self.cumdist[i])
          best = (off, along, i)
      return best

    lo = max(0, self._last_idx - 10)
    hi = min(len(self.points) - 1, self._last_idx + 200)
    off, along, idx = search(lo, hi)
    if off > 100.:  # widen to a full search after GPS gaps
      off, along, idx = search(0, len(self.points) - 1)
    self._last_idx = idx
    return along, off

  def upcoming_maneuvers(self, along: float, horizon: float) -> list[tuple[float, str]]:
    """Maneuvers ahead within the horizon, as (remaining distance, kind)."""
    return [(d - along, kind) for d, kind in self.maneuvers if 0. < d - along <= horizon]

  def remaining_distance(self, along: float) -> float:
    return max(0., (self.cumdist[-1] if self.cumdist else 0.) - along)
