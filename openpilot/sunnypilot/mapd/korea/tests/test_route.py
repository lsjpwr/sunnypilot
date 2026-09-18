"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import unittest

from openpilot.sunnypilot.mapd.korea import route
from openpilot.sunnypilot.mapd.korea.route import (DAILY_REQUEST_CAP, MAX_ROUTE_POINTS, OFF_ROUTE_TICKS,
                                                   REROUTE_BACKOFF_S, RequestBudget, RouteState, build_request,
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
    self.assertEqual(parse_route(SAMPLE), [
      (37.5665, 126.9780), (37.5600, 126.9800), (37.5500, 126.9850), (37.4979, 127.0276),
    ])

  def test_point_features_are_ignored(self):
    only_points = {"features": [feature([126.9780, 37.5665], kind="Point")]}
    self.assertEqual(parse_route(only_points), [])

  def test_a_coordinate_outside_korea_discards_the_whole_route(self):
    payload = {"features": [feature([[126.9780, 37.5665], [139.6503, 35.6762]])]}
    self.assertEqual(parse_route(payload), [])

  def test_too_many_points_are_discarded(self):
    coords = [[126.9780 + i * 1e-5, 37.5665] for i in range(MAX_ROUTE_POINTS + 1)]
    self.assertEqual(parse_route({"features": [feature(coords)]}), [])

  def test_a_single_point_is_not_a_route(self):
    self.assertEqual(parse_route({"features": [feature([[126.9780, 37.5665]])]}), [])

  def test_malformed_payload_is_empty_not_an_exception(self):
    for bad in ({}, {"features": None}, {"features": [{}]}, {"features": [{"geometry": 7}]},
                {"features": [feature([["nope", "nope"]])]}):
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
    route = fetch_route("K", (37.5665, 126.9780), (37.4979, 127.0276), opener=fake_opener(SAMPLE))
    self.assertEqual(len(route), 4)

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
    route = fetch_route("", (37.5665, 126.9780), (37.4979, 127.0276),
                        opener=fake_opener(SAMPLE, capture))
    self.assertEqual(route, [])
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
