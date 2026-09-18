"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

route: turns a destination into a polyline, and the polyline into slowdown targets.

Only the geometry is taken from the routing provider. Speed limits and cameras already
live in the local sqlite databases, so the route's single job is telling the lookups
which way we go at a fork. That keeps the parser thin enough to swap providers by
editing parse_route alone.

Deliberately free of openpilot imports so it runs under a bare Python interpreter --
same rule as db.py and geo.py.
"""
import json
import logging
import math
import threading
import time
import urllib.request

from openpilot.sunnypilot.mapd.korea.geo import haversine, M_PER_DEG_LAT, point_segment_distance

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


ROUTE_URL = "https://apis.openapi.sk.com/tmap/routes?version=1&format=json"
# Much shorter than camera_refresh's 30 s: that one runs offroad with all day to finish,
# this one is a driver waiting for a reroute. A request that has not answered in 10 s has
# already lost to the backoff that follows it.
HTTP_TIMEOUT_S = 10.
# A hard ceiling that does not depend on knowing the provider's free tier, which is not
# published. Real use is a handful of requests per drive, so this only ever fires on a bug.
DAILY_REQUEST_CAP = 200
_DAY_S = 24 * 3600


class RequestBudget:
  """A per-day request ceiling. Not thread-safe -- one route thread owns it."""

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


def build_request(api_key: str, start: tuple[float, float],
                  dest: tuple[float, float]) -> urllib.request.Request:
  """The key goes in a header, never the URL: urllib puts the URL in exception messages
  and those reach cloudlog. camera_refresh.py:105 makes the same promise."""
  body = {
    "startX": start[1], "startY": start[0],
    "endX": dest[1], "endY": dest[0],
    "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
    "searchOption": "0",
  }
  return urllib.request.Request(
    ROUTE_URL,
    data=json.dumps(body).encode(),
    headers={"appKey": api_key, "Content-Type": "application/json"},
    method="POST",
  )


def fetch_route(api_key: str, start: tuple[float, float], dest: tuple[float, float],
                opener=urllib.request.urlopen, budget: RequestBudget | None = None
                ) -> list[tuple[float, float]]:
  """One routing request. [] for anything that does not produce a route we trust.

  Every reason to not make the request is checked before the socket is touched, so a
  missing key or a bad destination costs nothing.
  """
  if not api_key:
    LOG.warning("route: no KoreaRouteApiKey set")
    return []
  if not in_korea(*start) or not in_korea(*dest):
    return []
  if budget is not None and not budget.allow():
    LOG.warning("route: daily request cap reached")
    return []

  try:
    if budget is not None:
      budget.spend()
    with opener(build_request(api_key, start, dest), timeout=HTTP_TIMEOUT_S) as response:
      payload = json.loads(response.read())
  except Exception:
    # Deliberately broad. urllib raises OSError/HTTPError, json raises ValueError, and a
    # truncated body can raise almost anything -- all of them mean the same thing here,
    # and this runs on a thread whose death would silently disable the feature.
    LOG.warning("route: request failed", exc_info=True)
    return []

  return parse_route(payload) if isinstance(payload, dict) else []


# Wider than the 30 m corridor the lookups use: the corridor decides whether a camera is
# on our road, this decides whether the route is wrong. Being strict here buys nothing and
# costs a request every time the localizer drifts in a tunnel.
OFF_ROUTE_M = 50.
# One tick of GPS jitter is not a wrong turn. Three seconds at 1 Hz is.
OFF_ROUTE_TICKS = 3
# The first step is the normal reroute latency -- 3 s to confirm plus 5 s is not felt. The
# rest exist only for the runaway case where the new route is immediately off-route too,
# which is the failure the cooldown alone used to be asked to cover and could not.
REROUTE_BACKOFF_S = (5., 15., 60., 300.)


def distance_to_route(route: list[tuple[float, float]], lat: float, lon: float) -> float:
  """Metres from (lat, lon) to the nearest point of the polyline."""
  if len(route) < MIN_ROUTE_POINTS:
    return float("inf")
  return min(point_segment_distance(lat, lon, a[0], a[1], b[0], b[1])
             for a, b in zip(route, route[1:], strict=False))


class RouteState:
  """Decides when to ask for a new route. Owns no I/O -- the caller does the fetching.

  Kept apart from the fetching on purpose: this is the part with the interesting states,
  and a pure object is the only way to test a backoff without waiting five minutes.
  """

  def __init__(self, clock=time.monotonic):
    self.route: list[tuple[float, float]] = []
    self._clock = clock
    self._off_ticks = 0
    self._failures = 0
    self._last_request = None  # None means 'never asked', which must not be a cooldown

  def set_route(self, route: list[tuple[float, float]]) -> None:
    self.route = route
    self._off_ticks = 0

  def note_request(self) -> None:
    """Called after a request goes out, whatever it answered. A request that produced a
    good route is still followed by a reset -- set_route's caller does that by getting
    back on the line, not by this method, so a bad answer keeps walking the backoff."""
    self._last_request = self._clock()
    self._failures += 1

  def reset_backoff(self) -> None:
    """A new destination starts a fresh backoff."""
    self._failures = 0
    self._last_request = None

  def _wait(self) -> float:
    return REROUTE_BACKOFF_S[max(0, min(self._failures - 1, len(REROUTE_BACKOFF_S) - 1))]

  def update(self, lat: float, lon: float) -> bool:
    if distance_to_route(self.route, lat, lon) <= OFF_ROUTE_M:
      self._off_ticks = 0
      self._failures = 0  # back on the line: whatever went wrong is over
      self._last_request = None  # ...and so is any cooldown that went with it
      return False

    self._off_ticks += 1
    if self._off_ticks < OFF_ROUTE_TICKS and self.route:
      return False
    if self._last_request is not None and self._clock() - self._last_request < self._wait():
      return False
    return True


# SCC-Map brakes with a jerk limit, so a point handed over at 300 m is already inside the
# comfortable window at highway speed and well outside it in town.
CURVE_HORIZON_M = 300.
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
  """(lat, lon, velocity) points for the curves inside CURVE_HORIZON_M ahead, nearest first.

  Only curves that actually ask for a slowdown are returned: a target at or above the set
  speed is not a target, it is noise SCC-Map would have to filter itself.
  """
  if len(route) < 3:
    return []

  # start at the segment we are on, so a curve already behind us is never reported
  start = min(range(len(route) - 1),
              key=lambda i: point_segment_distance(lat, lon, route[i][0], route[i][1],
                                                   route[i + 1][0], route[i + 1][1]))

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

    curvature = _menger_curvature(route[i - 1], route[i], route[i + 1])
    if curvature <= 0.:
      continue
    velocity = max(math.sqrt(A_LAT_MAX / curvature), MIN_V_MS)
    if velocity < v_max_ms:
      targets.append((route[i][0], route[i][1], velocity))

  return targets


# Close enough to be there. Generous on purpose -- the destination the phone sends is a
# POI centroid, not the kerb, and a 20 m threshold would leave the route live in a car park.
ARRIVED_M = 100.


def arrived(position: tuple[float, float] | None, destination: tuple[float, float] | None) -> bool:
  """Are we there? False when either end is unknown -- 'no position' is not 'arrived'."""
  if position is None or destination is None:
    return False
  return haversine(position[0], position[1], destination[0], destination[1]) <= ARRIVED_M


class RouteSource:
  """Background thread that keeps a polyline for the current destination.

  Runs off the control loop for one reason: a routing request takes seconds, and
  KoreaMapData.tick() is the 1 Hz path that decides whether to brake. Same split, and the
  same start/stop shape, as CameraRefresher.
  """

  def __init__(self, clock=time.monotonic):
    self.state = RouteState(clock=clock)
    self.budget = RequestBudget()
    self._lock = threading.Lock()
    self._position: tuple[float, float] | None = None
    # The destination the current self.state.route was fetched for (or is being fetched
    # for), so _loop can tell "still the same drive" from "a new one just got picked" --
    # RouteState.update alone only knows about position, never about the destination.
    self._route_destination: tuple[float, float] | None = None
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None:
      return
    self._thread = threading.Thread(target=self._loop, daemon=True)
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=2.)
      self._thread = None

  def set_position(self, lat: float, lon: float) -> None:
    with self._lock:
      self._position = (lat, lon)

  def latest(self) -> list[tuple[float, float]]:
    # RouteState is single-owner and documents no lock of its own -- this is the boundary
    # where the control loop's reads and the route thread's writes actually cross, so the
    # lock belongs here, not inside RouteState.
    with self._lock:
      return self.state.route

  def _destination(self, params) -> tuple[float, float] | None:
    """The destination athenad's setNavDestination RPC and the UDP socket both write."""
    raw = params.get("NavDestination")
    if not raw:
      return None
    try:
      dest = json.loads(raw)
      lat, lon = float(dest["latitude"]), float(dest["longitude"])
    except (ValueError, TypeError, KeyError):
      return None
    return (lat, lon) if in_korea(lat, lon) else None

  def _loop(self) -> None:
    # imported here so the module stays importable without the device stack, which is what
    # lets the tests run under a bare interpreter -- same as CameraRefresher._loop
    from openpilot.common.params import Params

    params = Params()
    while not self._stop.is_set():
      try:
        with self._lock:
          position = self._position
        destination = self._destination(params)

        if arrived(position, destination):
          # Arrived. Clearing here rather than waiting for the phone means a driver who
          # closes the nav app on the kerb does not keep a route that now points behind
          # them -- and the reroute machinery would otherwise fire on every tick.
          params.remove("NavDestination")
          destination = None

        if destination != self._route_destination:
          # The destination changed -- including to/from None. The old polyline can still
          # read as "on route" for a road we are no longer headed down (that is the whole
          # bug: the car is physically on the new road before RouteState notices anything
          # is wrong), so drop it rather than let the off-route/backoff machinery hold it.
          # And the old destination's failure history says nothing about this one, so a
          # fresh destination gets a fresh backoff rather than inheriting a stale cooldown.
          with self._lock:
            self.state.set_route([])
          self.state.reset_backoff()
          self._route_destination = destination

        if destination is None or position is None:
          with self._lock:
            self.state.set_route([])
        elif self.state.update(*position):
          api_key = params.get("KoreaRouteApiKey", return_default=True) or ""
          route = fetch_route(api_key, position, destination, budget=self.budget)
          self.state.note_request()
          if route:
            with self._lock:
              self.state.set_route(route)
      except Exception:
        # This thread dying disables the feature silently until the next reboot, and
        # nothing it does is worth that. Keep going and try again next second.
        LOG.exception("route: source loop error")

      self._stop.wait(1.)
