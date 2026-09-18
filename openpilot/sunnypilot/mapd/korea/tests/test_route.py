"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import unittest

from openpilot.sunnypilot.mapd.korea import route
from openpilot.sunnypilot.mapd.korea.route import (DAILY_REQUEST_CAP, MAX_ROUTE_POINTS, RequestBudget,
                                                   build_request, fetch_route, in_korea, parse_route)


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
