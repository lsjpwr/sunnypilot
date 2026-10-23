"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

route: receives the phone's geometry and turns it into slowdown targets.
The REST parsers remain for offline replay. No routing requests run on the comma.

Deliberately free of openpilot imports so it runs under a bare Python interpreter --
same rule as db.py and geo.py.
"""
import bisect
from collections import Counter
import glob
import json
import logging
import math
import os
import threading
import time

from openpilot.sunnypilot.mapd.korea.geo import closest_point_on_segment, haversine, M_PER_DEG_LAT, point_segment_distance

# Everything this feature serves is inside South Korea, so a coordinate outside it is a
# provider bug or a hostile answer either way. Generous on purpose: the box covers Jeju
# and Ulleungdo, and it is a sanity check, not a service area.
KOREA_BBOX = (33., 39., 124., 132.)  # min_lat, max_lat, min_lon, max_lon

# A 300 km route at TMAP's resolution is a few thousand points. 5000 is a runaway guard,
# not a real limit -- a bigger answer means the request was not what we think it was.
MAX_ROUTE_POINTS = 5000
MIN_ROUTE_POINTS = 2

LOG = logging.getLogger(__name__)


def in_korea(lat: float, lon: float) -> bool:
  min_lat, max_lat, min_lon, max_lon = KOREA_BBOX
  return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def parse_route(payload: dict) -> list[tuple[float, float]]:
  """Concatenated LineString geometry as (lat, lon), or [] for anything we will not trust.

  [] rather than an exception: every caller is on a path that must keep running with no
  route, and 'no route' is already a state the rest of the feature handles.
  """
  points: list[tuple[float, float]] = []
  try:
    for feat in payload.get("features") or []:
      geometry = feat.get("geometry") or {}
      if geometry.get("type") != "LineString":
        continue
      for lon, lat in geometry.get("coordinates") or []:
        lat, lon = float(lat), float(lon)
        if not in_korea(lat, lon):
          LOG.warning("route: coordinate outside Korea, discarding the route")
          return []
        if points and points[-1] == (lat, lon):
          # TMAP emits one LineString per road segment; adjacent segments repeat their
          # shared joint coordinate. Collapse it so the join isn't counted twice.
          continue
        points.append((lat, lon))
        if len(points) > MAX_ROUTE_POINTS:
          LOG.warning("route: over %d points, discarding the route", MAX_ROUTE_POINTS)
          return []
  except (AttributeError, TypeError, ValueError):
    # A payload shaped differently from what we expect is the same as no route. Deliberately
    # broad over the shape errors only -- a real bug in this function should still raise.
    LOG.warning("route: malformed payload")
    return []

  if len(points) < MIN_ROUTE_POINTS:
    # A 200 with no usable geometry: the request worked and the budget was spent, but there
    # is nothing to show for it. This is the one shape of this response TMAP has never been
    # checked against live, so it is the likeliest place a wrong assumption shows up.
    LOG.warning("route: parsed %d point(s), below the %d needed for a usable route",
               len(points), MIN_ROUTE_POINTS)
    return []

  LOG.info("route: parsed %d points", len(points))
  return points


# TMAP's LineString facilityType for a road on a level of its own: 2 터널, 3 고가도로, 4 지하도로
# (tmapRouteDoc; ints in the live answers). 1 교량 is left out: a bridge has junctions at both ends.
GRADE_SEPARATED_FACILITIES = frozenset({2, 3, 4})


def parse_grade_separated(payload: dict) -> list[list[tuple[float, float]]]:
  """The route's tunnels, overpasses and underpasses: each LineString TMAP marks with one of
  GRADE_SEPARATED_FACILITIES, as (lat, lon), in route order. [] for anything unreadable, which
  is what an answer without them gives too. Read only from an answer parse_route accepted."""
  try:
    return [[(float(lat), float(lon)) for lon, lat in feat["geometry"]["coordinates"]]
            for feat in payload.get("features") or []
            if (feat.get("properties") or {}).get("facilityType") in GRADE_SEPARATED_FACILITIES
            and (feat.get("geometry") or {}).get("type") == "LineString"]
  except (AttributeError, KeyError, TypeError, ValueError):
    LOG.warning("route: unreadable facility spans, using none")
    return []


# Shared with Tesla's read and command budgets; their callers supply their own caps.
DAILY_REQUEST_CAP = 200
_DAY_S = 24 * 3600


class RequestBudget:
  """A per-day request ceiling, owned by one caller thread."""

  def __init__(self, cap: int = DAILY_REQUEST_CAP, clock=time.time):  # noqa: TID251
    # Wall clock on purpose: 'per day' is a calendar notion, and a monotonic clock has an
    # arbitrary epoch. Same reasoning as CameraRefresher._due.
    self.cap = cap
    self._clock = clock
    self._day = int(clock() // _DAY_S)
    self._spent = 0

  def _roll(self) -> None:
    day = int(self._clock() // _DAY_S)
    if day != self._day:
      self._day, self._spent = day, 0

  def allow(self) -> bool:
    self._roll()
    return self._spent < self.cap

  def spend(self) -> None:
    self._roll()
    self._spent += 1


# What a camera or enforcement point would say in a TMAP text field (description, name)
CAMERA_WORDS = ("카메라", "단속", "과속")


def summarize_route(payload: dict) -> str:
  """What a routing answer carries besides its geometry: the property keys of each feature kind, the counts
  of every *Type code, and which text fields name a camera. Logged to find out whether TMAP sends the cameras
  along the route (2026-10-09). No coordinates and no text values -- those place the car."""
  counts: Counter = Counter()
  keys: dict[str, set[str]] = {}
  codes: dict[str, dict[str, Counter]] = {}
  words: Counter = Counter()
  try:
    for feat in payload.get("features") or []:
      props = feat.get("properties") or {}
      kind = str((feat.get("geometry") or {}).get("type"))
      counts[kind] += 1
      keys.setdefault(kind, set()).update(props)
      for key, value in props.items():
        if key.endswith("Type") and (isinstance(value, int) or (isinstance(value, str) and len(value) <= 4)):
          codes.setdefault(kind, {}).setdefault(key, Counter())[value] += 1
        if isinstance(value, str) and any(word in value for word in CAMERA_WORDS):
          words[key] += 1
  except (AttributeError, TypeError):
    return f"keys {sorted(payload)}, features unreadable"
  parts = [f"keys {sorted(payload)}"]
  for kind in sorted(counts):
    parts.append(" ".join([f"{kind} x{counts[kind]} keys {sorted(keys[kind])}",
                           *(f"{key} {dict(sorted(c.items(), key=str))}" for key, c in sorted(codes.get(kind, {}).items()))]))
  parts.append(f"camera words {dict(words)}")
  return "; ".join(parts)


def distance_to_route(route: list[tuple[float, float]], lat: float, lon: float) -> float:
  """Metres from (lat, lon) to the nearest point of the polyline."""
  if len(route) < MIN_ROUTE_POINTS:
    return float("inf")
  return min(point_segment_distance(lat, lon, a[0], a[1], b[0], b[1])
             for a, b in zip(route, route[1:], strict=False))


def route_progress(route: list[tuple[float, float]], lat: float, lon: float) -> float:
  """Metres along the polyline to its point nearest (lat, lon); inf without a route."""
  if len(route) < MIN_ROUTE_POINTS:
    return float("inf")
  i = min(range(len(route) - 1),
          key=lambda k: point_segment_distance(lat, lon, route[k][0], route[k][1], route[k + 1][0], route[k + 1][1]))
  along = sum(haversine(route[k][0], route[k][1], route[k + 1][0], route[k + 1][1]) for k in range(i))
  plat, plon = closest_point_on_segment(lat, lon, route[i][0], route[i][1], route[i + 1][0], route[i + 1][1])
  return along + haversine(route[i][0], route[i][1], plat, plon)


# SCC-Map ramps its target down at MapSlowdownDecel (0.5-1.2 m/s^2), which at the gentlest 0.5 takes
# about 1.2 km to bring 125 km/h down to the 20 km/h floor; a curve seen later than its ramp needs
# steps the target down and the planner brakes at its 1.2 cap. A far curve costs nothing: its ramp
# speed sits far above the set speed until the car is close.
CURVE_HORIZON_M = 2000.
# SCC-Map ramps on the straight-line distance to a point, but the horizon above is along the route. A
# vertex the route reaches only after turning back toward the car (the return leg of a U-turn or P-turn,
# a loop ramp) is far along the route yet close in a straight line, and would slow the car long before
# it gets there. It is left out while its straight-line distance is under this share of the distance
# along the route, and comes back once the car is on the leg heading at it. The far end of a plain
# 180 degree bend still reads 0.54 or more, the least with a straight lead-in of 4/pi radii.
MIN_STRAIGHT_LINE_RATIO = 0.5
# m. Curvature is measured through the vertices at least this far along the route either side of a
# vertex, not through its raw neighbours: a TMAP joint can put a vertex 1-3 m past the last one and
# 20-30 cm off the line, which three raw points read as a ~230 m radius (2026-10-03: 33 of 34 route
# curve slowdowns had no curve under them). Real vertices keep a sampled arc exact, and a curve worth
# slowing for is far longer than this.
CURVE_BASELINE_M = 25.
# Matches _A_LAT_REG_MAX in smart_cruise_control/vision_controller.py:31. The two
# controllers feed the same longitudinal planner, so disagreeing here would show up as one
# of them fighting the other through a bend.
A_LAT_MAX = 2.
# smart_cruise_control/__init__.py: MIN_V = 20 * CV.KPH_TO_MS. Repeated rather than
# imported because korea/ must stay importable without the openpilot stack.
MIN_V_MS = 20. / 3.6
# Menger curvature goes as 1/spacing, so a triple whose points are centimetres apart turns
# coordinate noise into a hairpin reading. No routing polyline carries real road shape below
# a metre, and a genuine hairpin still registers on the wider triples along it, so a triple
# this short is treated as straight rather than trusted.
MIN_SIDE_M = 1.


def _menger_curvature(a: tuple[float, float], b: tuple[float, float],
                      c: tuple[float, float]) -> float:
  """Curvature in 1/m of the circle through three points, 0 when they are collinear.

  Menger's formula rather than a derivative: the polyline's point spacing is uneven, and
  three points with a circumscribed circle need no differentiation to be stable.
  """
  ab = haversine(a[0], a[1], b[0], b[1])
  bc = haversine(b[0], b[1], c[0], c[1])
  ca = haversine(c[0], c[1], a[0], a[1])
  if ab < MIN_SIDE_M or bc < MIN_SIDE_M or ca < MIN_SIDE_M:
    return 0.

  # twice the triangle area, by the cross product in a local flat frame
  m_lon = M_PER_DEG_LAT * math.cos(math.radians(b[0]))
  ax, ay = (a[1] - b[1]) * m_lon, (a[0] - b[0]) * M_PER_DEG_LAT
  cx, cy = (c[1] - b[1]) * m_lon, (c[0] - b[0]) * M_PER_DEG_LAT
  area2 = abs(ax * cy - ay * cx)
  return 2. * area2 / (ab * bc * ca) if area2 > 0. else 0.


def curve_targets(route: list[tuple[float, float]], lat: float, lon: float,
                  v_max_ms: float) -> list[tuple[float, float, float]]:
  """(lat, lon, velocity) points for the curves inside CURVE_HORIZON_M ahead, in route order.

  Only curves that actually ask for a slowdown are returned: a target at or above the set
  speed is not a target, it is noise SCC-Map would have to filter itself.
  """
  if len(route) < 3:
    return []

  # start at the segment we are on, so a curve already behind us is never reported
  start = min(range(len(route) - 1),
              key=lambda i: point_segment_distance(lat, lon, route[i][0], route[i][1],
                                                   route[i + 1][0], route[i + 1][1]))

  cum = [0.]
  for a, b in zip(route, route[1:], strict=False):
    cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))

  targets: list[tuple[float, float, float]] = []
  # The walk starts at the car, not the segment's start vertex: seeding from route[start]
  # counted the distance back to that vertex as forward travel, which could shrink the
  # horizon enough to miss a curve just ahead.
  travelled = 0.
  prev = (lat, lon)
  for i in range(start + 1, len(route) - 1):
    travelled += haversine(prev[0], prev[1], route[i][0], route[i][1])
    prev = route[i]
    if travelled > CURVE_HORIZON_M:
      break
    if haversine(lat, lon, route[i][0], route[i][1]) < MIN_STRAIGHT_LINE_RATIO * travelled:
      continue

    # the vertices at least CURVE_BASELINE_M back and ahead along the route. Within that of either end
    # there is none, and the short side left would read joint noise as a curve again.
    back = bisect.bisect_right(cum, cum[i] - CURVE_BASELINE_M) - 1
    ahead = bisect.bisect_left(cum, cum[i] + CURVE_BASELINE_M)
    if back < 0 or ahead >= len(route):
      continue
    curvature = _menger_curvature(route[back], route[i], route[ahead])
    if curvature <= 0.:
      continue
    velocity = max(math.sqrt(A_LAT_MAX / curvature), MIN_V_MS)
    if velocity < v_max_ms:
      targets.append((route[i][0], route[i][1], velocity))

  return targets


# Routes kept on the device, so a drive's curve targets can be replayed against its logs later.
ROUTES_KEPT = 20


def save_route(directory: str, points: list[tuple[float, float]], keep: int = ROUTES_KEPT) -> None:
  """Write a fetched route to directory as route-<ms>.json and keep only the newest `keep`. Local only:
  a route is where the car went, and nothing reads this directory but a person pulling it by hand."""
  os.makedirs(directory, exist_ok=True)
  # The wall clock on purpose: the file is matched to a drive's logs by time.
  now = time.time()  # noqa: TID251
  path = os.path.join(directory, f"route-{int(now * 1000)}.json")
  with open(path + ".tmp", "w") as f:
    json.dump({"saved": now, "points": points}, f)
  os.replace(path + ".tmp", path)
  for old in sorted(glob.glob(os.path.join(directory, "route-*.json")))[:-keep]:
    os.remove(old)


# Close enough to be there. Generous on purpose -- the destination the phone sends is a
# POI centroid, not the kerb, and a 20 m threshold would leave the route live in a car park.
ARRIVED_M = 100.


def arrived(position: tuple[float, float] | None, destination: tuple[float, float] | None) -> bool:
  """Are we there? False when either end is unknown -- 'no position' is not 'arrived'."""
  if position is None or destination is None:
    return False
  return haversine(position[0], position[1], destination[0], destination[1]) <= ARRIVED_M


def _wire_int(value, minimum=0):
  return type(value) is int and minimum <= value < 2 ** 53


class RouteSource:
  """One phone trip, one bounded assembly, and its last fully validated route. No I/O."""

  def __init__(self):
    self._lock = threading.Lock()
    self._session = self._trip = self._sequence = self._wanted = self._applied = 0
    self._closed = False
    self._started = None
    self._destination = None
    self._route = []
    self._layers = []
    self._pending = {}
    self._count = 0

  def _clear(self):
    self._route, self._layers, self._pending = [], [], {}
    self._applied = self._count = 0

  def latest(self):
    with self._lock:
      return self._route, self._layers

  def applied(self):
    with self._lock:
      return [self._session, self._trip, self._applied] if self._applied else None

  def closed(self):
    with self._lock:
      return self._closed

  def set_started(self, started):
    with self._lock:
      if self._started is True and not started:
        self._closed = True
        self._clear()
      self._started = started

  def set_position(self, lat, lon):
    with self._lock:
      if arrived((lat, lon), self._destination):
        self._closed = True
        self._clear()

  def receive(self, payload):
    session, trip, route_id = (payload.get(k) for k in ("session", "trip", "route_id"))
    if not any(k in payload for k in ("session", "trip", "route_id", "route")):
      return True  # legacy item/trip packets still work, but cannot install a route
    if not _wire_int(session, 1) or not _wire_int(trip) or not _wire_int(route_id):
      return False
    with self._lock:
      if "route" in payload:
        if ((session, trip, route_id) == (self._session, self._trip, self._wanted)
            and not self._closed and route_id and route_id != self._applied):
          self._piece(payload["route"])
        return False  # route pieces never replace the 2 Hz safety snapshot
      sequence = payload.get("seq")
      if not _wire_int(sequence, 1) or session < self._session:
        return False
      if session == self._session and (sequence <= self._sequence or trip < self._trip):
        return False
      new_trip = (session, trip) != (self._session, self._trip)
      if not new_trip and self._closed:
        self._sequence = sequence
        return True  # safety updates continue; this trip's route stays closed
      lat, lon = payload.get("destination_lat"), payload.get("destination_lon")
      ended = type(lat) in (int, float) and type(lon) in (int, float) and lat == lon == 0
      valid_destination = type(lat) in (int, float) and type(lon) in (int, float) and in_korea(lat, lon)
      if not new_trip and not ended and route_id < self._wanted:
        return False
      if not new_trip and valid_destination and self._destination != (lat, lon):
        return False
      if not ended and not valid_destination and trip:
        return False
      if new_trip:
        self._clear()
        self._closed = False
        self._wanted = 0
      self._session, self._trip, self._sequence = session, trip, sequence
      if ended or not trip:
        self._closed = True
        self._clear()
      else:
        self._destination = (float(lat), float(lon))
        if route_id != self._wanted:
          self._pending = {}
          self._count = 0
        self._wanted = route_id
      return True

  def _piece(self, piece):
    if not isinstance(piece, dict):
      return
    part, parts, count = (piece.get(k) for k in ("part", "parts", "count"))
    if not all(_wire_int(v) for v in (part, parts, count)) or not MIN_ROUTE_POINTS <= count <= MAX_ROUTE_POINTS:
      return
    if parts != (count + 127) // 128 or not 0 <= part < parts or self._count not in (0, count):
      return
    points, layers = piece.get("points"), piece.get("layers", [])
    if not isinstance(points, list) or len(points) != min(128, count - part * 128):
      return
    if not isinstance(layers, list) or len(layers) > 128:
      return
    for p in points:
      if not isinstance(p, list) or len(p) != 2 or any(type(v) not in (int, float) for v in p) or not in_korea(p[1], p[0]):
        return
    for span in layers:
      if not isinstance(span, list) or len(span) != 2 or not all(_wire_int(v) for v in span) or not 0 <= span[0] < span[1] < count:
        return
    if part in self._pending:
      return
    self._count = count
    self._pending[part] = ([(float(p[1]), float(p[0])) for p in points], layers)
    if len(self._pending) != parts:
      return
    route = [p for i in range(parts) for p in self._pending[i][0]]
    spans = [span for i in range(parts) for span in self._pending[i][1]]
    self._pending = {}
    self._count = 0
    if any(b[0] <= a[1] for a, b in zip(spans, spans[1:], strict=False)):
      return
    self._route = route
    self._layers = [route[start:end + 1] for start, end in spans]
    self._applied = self._wanted
