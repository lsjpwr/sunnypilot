"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from openpilot.sunnypilot.mapd.korea import route
from openpilot.sunnypilot.mapd.korea.geo import haversine
from openpilot.sunnypilot.mapd.korea.route import (A_LAT_MAX, DAILY_REQUEST_CAP, MAX_ROUTE_POINTS, MIN_V_MS,
                                                   OFF_ROUTE_TICKS, REROUTE_BACKOFF_S, RequestBudget, RouteSource,
                                                   RouteState, arrived, build_request, curve_targets,
                                                   distance_to_route, fetch_route, in_korea, parse_route)


def feature(coords, kind="LineString"):
  return {"type": "Feature", "geometry": {"type": kind, "coordinates": coords}}


# TMAP answers GeoJSON: coordinates are [lon, lat], LineString features carry the road
# geometry and Point features carry the turn markers. Seoul city hall -> Gangnam, trimmed.
SAMPLE = {
  "type": "FeatureCollection",
  "features": [
    feature([126.9780, 37.5665], kind="Point"),
    feature([[126.9780, 37.5665], [126.9800, 37.5600], [126.9850, 37.5500]]),
    feature([[126.9850, 37.5500], [127.0276, 37.4979]]),
  ],
}


class TestInKorea(unittest.TestCase):
  def test_seoul_is_inside(self):
    self.assertTrue(in_korea(37.5665, 126.9780))

  def test_tokyo_is_outside(self):
    self.assertFalse(in_korea(35.6762, 139.6503))

  def test_null_island_is_outside(self):
    self.assertFalse(in_korea(0., 0.))


class TestParseRoute(unittest.TestCase):
  def test_linestrings_concatenate_in_order(self):
    with self.assertLogs(route.LOG, level="DEBUG"):
      result = parse_route(SAMPLE)
    self.assertEqual(result, [
      (37.5665, 126.9780), (37.5600, 126.9800), (37.5500, 126.9850), (37.4979, 127.0276),
    ])

  def test_point_features_are_ignored(self):
    only_points = {"features": [feature([126.9780, 37.5665], kind="Point")]}
    with self.assertLogs(route.LOG, level="DEBUG"):
      self.assertEqual(parse_route(only_points), [])

  def test_a_coordinate_outside_korea_discards_the_whole_route(self):
    payload = {"features": [feature([[126.9780, 37.5665], [139.6503, 35.6762]])]}
    with self.assertLogs(route.LOG, level="DEBUG"):
      self.assertEqual(parse_route(payload), [])

  def test_too_many_points_are_discarded(self):
    coords = [[126.9780 + i * 1e-5, 37.5665] for i in range(MAX_ROUTE_POINTS + 1)]
    with self.assertLogs(route.LOG, level="DEBUG"):
      self.assertEqual(parse_route({"features": [feature(coords)]}), [])

  def test_a_single_point_is_not_a_route(self):
    with self.assertLogs(route.LOG, level="DEBUG"):
      self.assertEqual(parse_route({"features": [feature([[126.9780, 37.5665]])]}), [])

  def test_malformed_payload_is_empty_not_an_exception(self):
    for bad in ({}, {"features": None}, {"features": [{}]}, {"features": [{"geometry": 7}]},
                {"features": [feature([["nope", "nope"]])]}):
      with self.assertLogs(route.LOG, level="DEBUG"):
        self.assertEqual(parse_route(bad), [])


class FakeResponse:
  def __init__(self, payload):
    self._body = json.dumps(payload).encode()

  def read(self):
    return self._body

  def __enter__(self):
    return self

  def __exit__(self, *args):
    return False


def fake_opener(payload, capture=None):
  def opener(request, timeout=None):
    if capture is not None:
      capture.append(request)
    return FakeResponse(payload)
  return opener


class FakeClock:
  def __init__(self, now=0.):
    self.now = now

  def __call__(self):
    return self.now


class TestArrived(unittest.TestCase):
  def test_the_destination_itself_counts(self):
    self.assertTrue(arrived((37.4979, 127.0276), (37.4979, 127.0276)))

  def test_inside_the_threshold_counts(self):
    self.assertTrue(arrived((37.4979 + 0.0005, 127.0276), (37.4979, 127.0276)))  # ~55 m

  def test_outside_the_threshold_does_not(self):
    self.assertFalse(arrived((37.4979 + 0.002, 127.0276), (37.4979, 127.0276)))  # ~220 m

  def test_an_unknown_position_is_not_arrival(self):
    self.assertFalse(arrived(None, (37.4979, 127.0276)))
    self.assertFalse(arrived((37.4979, 127.0276), None))


class TestBuildRequest(unittest.TestCase):
  def test_the_key_travels_in_the_header_not_the_url(self):
    request = build_request("SECRET", (37.5665, 126.9780), (37.4979, 127.0276))
    self.assertNotIn("SECRET", request.full_url)
    self.assertEqual(request.get_header("Appkey"), "SECRET")

  def test_the_body_carries_lon_lat_in_that_order(self):
    request = build_request("K", (37.5665, 126.9780), (37.4979, 127.0276))
    body = json.loads(request.data)
    self.assertEqual((body["startX"], body["startY"]), (126.9780, 37.5665))
    self.assertEqual((body["endX"], body["endY"]), (127.0276, 37.4979))
    self.assertEqual(body["reqCoordType"], "WGS84GEO")
    self.assertEqual(body["resCoordType"], "WGS84GEO")


class TestRequestBudget(unittest.TestCase):
  def test_spending_up_to_the_cap_is_allowed(self):
    budget = RequestBudget(cap=3, clock=FakeClock())
    for _ in range(3):
      self.assertTrue(budget.allow())
      budget.spend()
    self.assertFalse(budget.allow())

  def test_the_budget_resets_on_the_next_day(self):
    clock = FakeClock()
    budget = RequestBudget(cap=1, clock=clock)
    budget.spend()
    self.assertFalse(budget.allow())
    clock.now += 24 * 3600
    self.assertTrue(budget.allow())

  def test_the_default_cap_is_the_documented_one(self):
    self.assertEqual(RequestBudget().cap, DAILY_REQUEST_CAP)


class TestFetchRoute(unittest.TestCase):
  def test_a_good_answer_becomes_a_polyline(self):
    with self.assertLogs(route.LOG, level="DEBUG"):
      result = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276), opener=fake_opener(SAMPLE))
    self.assertEqual(len(result), 4)

  def test_an_http_failure_is_an_empty_route(self):
    def boom(request, timeout=None):
      raise OSError("no network")
    with self.assertLogs(route.LOG, level="DEBUG"):
      self.assertEqual(fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276), opener=boom), [])

  def test_a_destination_outside_korea_is_never_requested(self):
    capture = []
    route = fetch_route("K", (37.5665, 126.9780), (35.6762, 139.6503),
                        opener=fake_opener(SAMPLE, capture))
    self.assertEqual(route, [])
    self.assertEqual(capture, [])

  def test_an_empty_key_is_never_requested(self):
    capture = []
    with self.assertLogs(route.LOG, level="DEBUG"):
      result = fetch_route("", (37.5665, 126.9780), (37.4979, 127.0276),
                           opener=fake_opener(SAMPLE, capture))
    self.assertEqual(result, [])
    self.assertEqual(capture, [])

  def test_the_budget_stops_the_request_before_the_socket(self):
    capture = []
    budget = RequestBudget(cap=0, clock=FakeClock())
    with self.assertLogs(route.LOG, level="DEBUG"):
      result = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276),
                           opener=fake_opener(SAMPLE, capture), budget=budget)
    self.assertEqual(result, [])
    self.assertEqual(capture, [])

  def test_budget_spend_is_enforced_on_subsequent_requests(self):
    capture = []
    budget = RequestBudget(cap=1, clock=FakeClock())
    # First call should succeed and spend the budget
    with self.assertLogs(route.LOG, level="DEBUG"):
      result1 = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276),
                            opener=fake_opener(SAMPLE, capture), budget=budget)
    self.assertEqual(len(result1), 4)
    self.assertEqual(len(capture), 1)
    # Second call should be blocked by the spent budget
    capture.clear()
    with self.assertLogs(route.LOG, level="DEBUG"):
      result2 = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276),
                            opener=fake_opener(SAMPLE, capture), budget=budget)
    self.assertEqual(result2, [])
    self.assertEqual(capture, [])


# A 1.1 km straight leg north-east of Seoul city hall. ~0.001 deg lat is ~111 m.
STRAIGHT = [(37.5665 + i * 0.001, 126.9780) for i in range(11)]


class TestDistanceToRoute(unittest.TestCase):
  def test_a_point_on_the_line_is_zero(self):
    self.assertLess(distance_to_route(STRAIGHT, 37.5675, 126.9780), 1.)

  def test_an_empty_route_is_infinitely_far(self):
    self.assertEqual(distance_to_route([], 37.5665, 126.9780), float("inf"))

  def test_the_offset_is_measured_perpendicular(self):
    # 0.001 deg of longitude at this latitude is about 88 m
    offset = distance_to_route(STRAIGHT, 37.5675, 126.9790)
    self.assertGreater(offset, 80.)
    self.assertLess(offset, 95.)


class TestRouteState(unittest.TestCase):
  def setUp(self):
    self.clock = FakeClock()
    self.state = RouteState(clock=self.clock)
    self.state.set_route(STRAIGHT)

  def drive(self, ticks, lat=37.5675, lon=126.9780):
    return [self.state.update(lat, lon) for _ in range(ticks)]

  def test_staying_on_route_never_reroutes(self):
    self.assertEqual(self.drive(10), [False] * 10)

  def test_three_consecutive_off_route_ticks_reroute(self):
    self.assertEqual(self.drive(3, lon=126.9800), [False, False, True])

  def test_two_off_route_ticks_then_back_on_does_not_reroute(self):
    self.drive(2, lon=126.9800)
    self.assertEqual(self.drive(3), [False] * 3)

  def test_an_empty_route_reroutes_immediately(self):
    self.state.set_route([])
    self.assertEqual(self.state.update(37.5675, 126.9780), True)

  def test_the_cooldown_holds_the_second_request(self):
    self.drive(3, lon=126.9800)
    self.state.note_request()
    self.assertEqual(self.drive(3, lon=126.9800), [False] * 3)
    self.clock.now += REROUTE_BACKOFF_S[0]
    self.assertEqual(self.state.update(37.5675, 126.9800), True)

  def test_consecutive_failures_walk_the_backoff(self):
    for expected_wait in REROUTE_BACKOFF_S:
      self.drive(OFF_ROUTE_TICKS, lon=126.9800)
      self.state.note_request()
      self.assertEqual(self.state.update(37.5675, 126.9800), False)
      self.clock.now += expected_wait

  def test_the_last_backoff_step_repeats_rather_than_overflowing(self):
    for _ in range(len(REROUTE_BACKOFF_S) + 3):
      self.drive(OFF_ROUTE_TICKS, lon=126.9800)
      self.state.note_request()
      self.clock.now += REROUTE_BACKOFF_S[-1]
    self.assertEqual(self.state.update(37.5675, 126.9800), True)

  def test_getting_back_on_route_resets_the_backoff(self):
    for _ in range(3):
      self.drive(OFF_ROUTE_TICKS, lon=126.9800)
      self.state.note_request()
      self.clock.now += REROUTE_BACKOFF_S[-1]
    self.drive(1)  # back on the line
    self.drive(OFF_ROUTE_TICKS, lon=126.9800)
    self.state.note_request()
    self.clock.now += REROUTE_BACKOFF_S[0]
    self.assertEqual(self.state.update(37.5675, 126.9800), True)

  def test_getting_back_on_route_clears_the_stale_cooldown(self):
    # Regression test for a bug in the plan's sample RouteState: getting back on route
    # reset _failures but left a stale _last_request timestamp behind. With _failures at 0,
    # REROUTE_BACKOFF_S[min(-1, 3)] silently wrapped around to the LARGEST backoff (via
    # Python negative indexing), so a fresh excursion right after a reset could be held
    # back for up to 300 s instead of rerouting after OFF_ROUTE_TICKS like a first excursion.
    self.drive(OFF_ROUTE_TICKS, lon=126.9800)
    self.state.note_request()
    self.drive(1)  # back on the line resets the backoff
    self.assertEqual(self.drive(OFF_ROUTE_TICKS, lon=126.9800), [False, False, True])

  def test_escalation_means_second_backoff_differs_from_first(self):
    # This test pins the backoff escalation: with a flattened constant backoff tuple like
    # (5., 5., 5., 5.), the second and first failures would have the same cooldown, and
    # this test would fail (update at t=10 would return True instead of False). The real
    # backoff escalates the second failure from 5 s to 15 s, so the assertion at step 5
    # discriminates the actual escalation from any flattened version.
    clock = FakeClock()
    state = RouteState(clock=clock)
    # Leave route empty so the off-route gate is skipped entirely.

    # First failure at t=0, cooldown is REROUTE_BACKOFF_S[0] = 5 s.
    state.note_request()

    # At t=5 the cooldown expires.
    clock.now = 5.
    self.assertTrue(state.update(37.5675, 126.9780))

    # Second failure at t=5, cooldown is REROUTE_BACKOFF_S[1] = 15 s.
    state.note_request()

    # At t=10, only 5 s have passed, so cooldown still holds (5 < 15).
    # Under a flattened (5., 5., 5., 5.) tuple this would return True instead.
    clock.now = 10.
    self.assertFalse(state.update(37.5675, 126.9780))

  def test_reset_backoff_clears_failures_and_the_cooldown(self):
    # A new destination is not a continuation of the old one's failures (Fix 2): three
    # failed requests leave the next update() inside the cooldown...
    clock = FakeClock()
    state = RouteState(clock=clock)
    for _ in range(3):
      state.note_request()
    self.assertFalse(state.update(37.5675, 126.9780))

    # ...but reset_backoff() (what a changed destination calls) clears that cooldown just
    # like arriving back on the route does, with the clock never having moved.
    state.reset_backoff()
    self.assertTrue(state.update(37.5675, 126.9780))


class TestRouteSourceDestination(unittest.TestCase):
  """RouteSource._destination reads a value written by athenad's setNavDestination RPC or by
  our own external_source.py socket -- either way, untrusted input from a remote party. It
  must degrade to "no destination" rather than raise on anything malformed, since it runs on
  a background thread whose death would silently disable the whole feature."""

  def setUp(self):
    self.source = RouteSource()

  def destination(self, raw):
    return self.source._destination(SimpleNamespace(get=lambda key: raw))

  def test_the_key_is_absent_entirely(self):
    self.assertIsNone(self.destination(None))

  def test_an_empty_string(self):
    self.assertIsNone(self.destination(""))

  def test_an_empty_json_object(self):
    self.assertIsNone(self.destination("{}"))

  def test_malformed_json(self):
    self.assertIsNone(self.destination("{not valid json"))

  def test_the_json_literal_null(self):
    self.assertIsNone(self.destination("null"))

  def test_a_json_array_instead_of_an_object(self):
    self.assertIsNone(self.destination("[]"))

  def test_latitude_present_but_non_numeric(self):
    raw = json.dumps({"latitude": "not-a-number", "longitude": 127.0276})
    self.assertIsNone(self.destination(raw))

  def test_latitude_present_as_a_nested_object(self):
    raw = json.dumps({"latitude": {"deg": 37, "min": 30}, "longitude": 127.0276})
    self.assertIsNone(self.destination(raw))

  def test_a_coordinate_outside_korea(self):
    raw = json.dumps({"latitude": 35.6762, "longitude": 139.6503})  # Tokyo
    self.assertIsNone(self.destination(raw))

  def test_a_valid_destination_inside_korea(self):
    raw = json.dumps({"latitude": 37.4979, "longitude": 127.0276})
    self.assertEqual(self.destination(raw), (37.4979, 127.0276))


def arc(center_lat, center_lon, radius_m, start_deg, end_deg, step_deg=5.):
  """A circular arc as (lat, lon) points -- a curve with a known radius to check against."""
  m_per_deg_lat = 111195.
  m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(center_lat))
  points = []
  angle = start_deg
  while angle <= end_deg:
    rad = math.radians(angle)
    points.append((center_lat + radius_m * math.sin(rad) / m_per_deg_lat,
                   center_lon + radius_m * math.cos(rad) / m_per_deg_lon))
    angle += step_deg
  return points


class TestCurveTargets(unittest.TestCase):
  def test_a_straight_road_has_no_targets(self):
    self.assertEqual(curve_targets(STRAIGHT, 37.5665, 126.9780, 30.), [])

  def test_an_empty_route_has_no_targets(self):
    self.assertEqual(curve_targets([], 37.5665, 126.9780, 30.), [])

  def test_a_200_m_radius_curve_targets_the_physics_speed(self):
    route = arc(37.5665, 126.9780, 200., 0., 90.)
    targets = curve_targets(route, route[0][0], route[0][1], 30.)
    self.assertTrue(targets)
    expected = math.sqrt(A_LAT_MAX * 200.)  # ~20 m/s
    self.assertAlmostEqual(targets[0][2], expected, delta=3.)

  def test_a_tight_curve_is_clamped_to_the_floor(self):
    route = arc(37.5665, 126.9780, 15., 0., 180.)
    targets = curve_targets(route, route[0][0], route[0][1], 30.)
    self.assertTrue(targets)
    self.assertGreaterEqual(min(t[2] for t in targets), MIN_V_MS)

  def test_a_gentle_curve_is_not_reported_above_the_set_speed(self):
    route = arc(37.5665, 126.9780, 2000., 0., 90.)
    self.assertEqual(curve_targets(route, route[0][0], route[0][1], 10.), [])

  def test_targets_come_back_nearest_first(self):
    # The lead-in must reach the curve's own first point with no gap. An earlier version of
    # this test used a nearby point as the arc's *center* (arc(37.5685, 126.9780, 100., ...)
    # with 37.5685 taken from STRAIGHT), but center_lat/center_lon is the circle's centre,
    # not a point on it -- that put a 100 m teleport between the straight lead-in and the
    # curve, which alone blew the 300 m CURVE_HORIZON_M budget and left only the single
    # degenerate joint triplet to report (len(targets) == 1, not > 1).
    curve = arc(37.5685, 126.9780, 100., 0., 180.)
    lead_in = [(curve[0][0] - 0.001, curve[0][1]), (curve[0][0] - 0.0005, curve[0][1])]
    route = lead_in + curve
    targets = curve_targets(route, lead_in[0][0], lead_in[0][1], 30.)
    self.assertGreater(len(targets), 1)
    distances = [haversine(lead_in[0][0], lead_in[0][1], lat, lon) for lat, lon, _ in targets]
    self.assertEqual(distances, sorted(distances))

  def test_a_curve_behind_us_is_ignored(self):
    route = arc(37.5665, 126.9780, 100., 0., 90.)
    beyond = route[-1]
    self.assertEqual(curve_targets(route, beyond[0], beyond[1], 30.), [])

  def test_a_two_point_route_has_no_targets(self):
    # Menger curvature needs three points; two can never form a triangle.
    self.assertEqual(curve_targets(STRAIGHT[:2], 37.5665, 126.9780, 30.), [])

  def test_duplicate_consecutive_points_do_not_raise(self):
    # A repeated point makes one side of the Menger triangle zero. That must be guarded
    # before the division, not just happen to avoid a ZeroDivisionError on this input --
    # the real curve on either side of the duplicate must still come through.
    curve = arc(37.5665, 126.9780, 200., 0., 90.)
    route = [curve[0], curve[1], curve[1]] + curve[2:]
    targets = curve_targets(route, curve[0][0], curve[0][1], 30.)
    self.assertTrue(targets)
    self.assertTrue(all(math.isfinite(v) for _, _, v in targets))

  def test_a_car_mid_segment_still_sees_the_curve_ahead(self):
    # Pins the travelled-distance seeding fix: the old code seeded `travelled` with the
    # *backward* distance to the segment's start vertex, then re-added that whole segment
    # on the first loop iteration -- a car 95% along a 200 m lead-in (10 m short of a real
    # curve) overshot the 300 m CURVE_HORIZON_M budget before the curve was ever examined,
    # and got zero targets. This assertion fails under that seeding.
    m_per_deg_lat = 111195.
    curve = arc(37.5885, 126.9780, 50., 0., 90.)
    lead_in_start = (curve[0][0] - 200. / m_per_deg_lat, curve[0][1])
    route = [lead_in_start, curve[0]] + curve[1:]
    car = (curve[0][0] - 10. / m_per_deg_lat, curve[0][1])  # 10 m short of the curve
    self.assertTrue(curve_targets(route, car[0], car[1], 30.))

  def test_a_centimetre_offset_on_a_straight_road_has_no_targets(self):
    # Coordinate noise, not road shape: adjacent GeoJSON LineString features are
    # concatenated and typically round to 7 decimals (~1 cm), so two points that should be
    # the same joint can end up a centimetre apart. Menger curvature scales as 1/spacing, so
    # that gap alone used to read as a phantom hairpin on a dead-straight road.
    m_per_deg_lon = 111195. * math.cos(math.radians(STRAIGHT[1][0]))
    route = list(STRAIGHT)
    route[2] = (route[1][0], route[1][1] + 0.01 / m_per_deg_lon)  # ~1 cm from route[1]
    self.assertEqual(curve_targets(route, STRAIGHT[0][0], STRAIGHT[0][1], 30.), [])


class TwoTickStop(threading.Event):
  """Lets RouteSource._loop run for exactly two iterations, switching what NavDestination
  reads back to right after the first one -- same one-shot-per-tick idea as
  test_camera_refresh.OneShotStop, extended to a scripted second tick."""

  def __init__(self, params_values, second_destination_raw):
    super().__init__()
    self._params_values = params_values
    self._second_destination_raw = second_destination_raw
    self._ticks = 0

  def wait(self, timeout=None):
    self._ticks += 1
    if self._ticks == 1:
      self._params_values["NavDestination"] = self._second_destination_raw
    else:
      self.set()
    return True


class FakeRouteParams:
  """Params stand-in for _loop: NavDestination/KoreaRouteApiKey read from a plain dict a
  test can mutate between ticks, the way TwoTickStop does."""

  def __init__(self, values):
    self._values = values

  def get(self, key, return_default=False):
    return self._values.get(key)

  def remove(self, key):
    self._values.pop(key, None)


class TestRouteSourceLoopDestinationChange(unittest.TestCase):
  """RouteState alone cannot exhibit either bug fixed here -- it has no notion of a
  destination at all. _loop is the only place a destination change actually does anything,
  so it is the only place that can prove Fix 1 (drop the stale route) and Fix 2 (give the
  new destination a fresh backoff) actually run. cereal is not imported by this loop, so
  only openpilot.common.params needs faking through sys.modules -- same technique
  test_camera_refresh.TestRefresherLoop uses for CameraRefresher._loop.
  """

  def test_a_changed_destination_drops_the_route_and_its_backoff(self):
    dest1, dest2 = (37.4979, 127.0276), (37.5000, 127.1000)  # both well clear of ARRIVED_M
    params_values = {
      "NavDestination": json.dumps({"latitude": dest1[0], "longitude": dest1[1]}),
      "KoreaRouteApiKey": "K",
    }
    dest2_raw = json.dumps({"latitude": dest2[0], "longitude": dest2[1]})

    fetch_calls = []

    def fake_fetch_route(api_key, start, dest, budget=None):
      fetch_calls.append(dest)
      return STRAIGHT  # a route with the car (STRAIGHT[0]) already sitting on it

    source = RouteSource(clock=FakeClock())
    source.set_position(*STRAIGHT[0])
    source._stop = TwoTickStop(params_values, dest2_raw)

    params_mod = types.ModuleType("openpilot.common.params")
    params_mod.Params = lambda: FakeRouteParams(params_values)

    with mock.patch.object(route, "fetch_route", fake_fetch_route):
      with mock.patch.dict(sys.modules, {"openpilot.common.params": params_mod}):
        source._loop()

    # Tick 1 fetches dest1's route and the car ends up sitting right on it. Without Fix 1,
    # tick 2 would read as "still on route" against that stale polyline and never call
    # fetch_route again -- len(fetch_calls) would stop at 1. Without Fix 2, Fix 1 dropping
    # the route would still leave dest1's backoff in place, and the frozen clock would hold
    # dest2's first request behind it -- also 1, not 2. Only with both does dest2 get asked
    # for on the very next tick.
    self.assertEqual(fetch_calls, [dest1, dest2])
