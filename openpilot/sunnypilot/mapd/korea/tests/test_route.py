"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import unittest

from openpilot.sunnypilot.mapd.korea.geo import haversine
from openpilot.sunnypilot.mapd.korea.route import (A_LAT_MAX, DAILY_REQUEST_CAP, MIN_V_MS, RequestBudget, arrived,
                                                   curve_targets, distance_to_route, in_korea, route_progress)


class TestInKorea(unittest.TestCase):
  def test_seoul_is_inside(self):
    self.assertTrue(in_korea(37.5665, 126.9780))

  def test_tokyo_is_outside(self):
    self.assertFalse(in_korea(35.6762, 139.6503))

  def test_null_island_is_outside(self):
    self.assertFalse(in_korea(0., 0.))


class FakeResponse:
  def __init__(self, payload):
    self._body = json.dumps(payload).encode()

  def read(self):
    return self._body

  def __enter__(self):
    return self

  def __exit__(self, *args):
    return False


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
    # 270 degrees of a 15 m radius is 71 m: the middle vertices have CURVE_BASELINE_M of arc either side and
    # read the radius exactly, whose physics speed (19.7 km/h) is under the floor
    route = arc(37.5665, 126.9780, 15., 0., 270.)
    targets = curve_targets(route, route[0][0], route[0][1], 30.)
    self.assertAlmostEqual(min(t[2] for t in targets), MIN_V_MS)

  def test_a_gentle_curve_is_not_reported_above_the_set_speed(self):
    route = arc(37.5665, 126.9780, 2000., 0., 90.)
    self.assertEqual(curve_targets(route, route[0][0], route[0][1], 10.), [])

  def test_targets_come_back_in_route_order(self):
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
    # route order, not straight-line distance: the horizon takes in this whole semicircle, whose far
    # side comes back toward the car
    indices = [route.index((lat, lon)) for lat, lon, _ in targets]
    self.assertEqual(indices, sorted(indices))

  def test_a_curve_the_route_reaches_only_after_turning_back_is_left_out(self):
    # a P-turn: straight over the junction, a U-turn 900 m on, back down the other carriageway and right at the
    # same junction. SCC-Map ramps on the straight-line distance, and that last turn is about 100 m from the car
    # in a straight line but nearly 2 km along the route: it would slow the car as it crosses the junction.
    m_per_deg_lat = 111195.
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(37.5665))

    def at(x, y):
      return (37.5665 + y / m_per_deg_lat, 126.9780 + x / m_per_deg_lon)

    out_leg = [at(0., y) for y in range(-100, 901, 10)]
    u_turn = [at(-15. + 15. * math.cos(math.radians(a)), 900. + 15. * math.sin(math.radians(a))) for a in range(15, 180, 15)]
    back_leg = [at(-30., y) for y in range(900, -1, -10)]
    right_turn = [at(x, 0.) for x in range(-40, -201, -10)]
    route = out_leg + u_turn + back_leg + right_turn
    car = route[0]
    targets = curve_targets(route, car[0], car[1], 60. / 3.6)
    self.assertTrue(targets)  # the U-turn
    self.assertTrue(all(haversine(car[0], car[1], lat, lon) > 800. for lat, lon, _ in targets))

  def test_the_far_end_of_a_half_turn_is_kept(self):
    # with a straight lead-in of 4/pi radii the far end of a 180 degree bend is as close as it gets in a straight
    # line for its distance along the route, sqrt(x^2 + 4) / (x + pi) = 0.54: MIN_STRAIGHT_LINE_RATIO must stay
    # under that, or a plain bend loses its far end
    m_per_deg_lat = 111195.
    curve = arc(37.5665, 126.9780, 100., 0., 180.)
    lead_in = (curve[0][0] - 127. / m_per_deg_lat, curve[0][1])
    route = [lead_in] + curve + [(curve[-1][0] - 50. / m_per_deg_lat, curve[-1][1])]
    targets = curve_targets(route, lead_in[0], lead_in[1], 30.)
    self.assertIn(curve[-1], [(lat, lon) for lat, lon, _ in targets])

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

  def test_a_wiggle_beside_a_short_step_has_no_targets(self):
    # 2026-10-03: a vertex 3 m past a joint and 30 cm off the line read as a ~230 m radius (78 km/h)
    # through three raw points. Over the 25 m baseline it is a straight road again.
    m_per_deg_lat = 111195.
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(37.5665))
    route = [(37.5665 + i * 50. / m_per_deg_lat, 126.9780) for i in range(8)]
    route.insert(4, (route[3][0] + 3. / m_per_deg_lat, route[3][1] + 0.3 / m_per_deg_lon))
    self.assertEqual(curve_targets(route, route[0][0], route[0][1], 30.), [])

  def test_a_wiggle_near_the_route_end_has_no_targets(self):
    # within CURVE_BASELINE_M of a route end one side of the triangle would be the short step itself
    m_per_deg_lat = 111195.
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(37.5665))
    route = [(37.5665 + i * 50. / m_per_deg_lat, 126.9780) for i in range(8)]
    route.insert(7, (route[7][0] - 3. / m_per_deg_lat, route[7][1] + 0.3 / m_per_deg_lon))
    self.assertEqual(curve_targets(route, route[0][0], route[0][1], 30.), [])

  def test_an_unevenly_sampled_curve_still_targets_the_physics_speed(self):
    # real polylines mix 1-3 m steps at joints with 35 m runs; the baseline must read the same radius
    main = arc(37.5665, 126.9780, 200., 0., 90., step_deg=10.)
    near = arc(37.5665, 126.9780, 200., 0.5, 80.5, step_deg=10.)  # 1.7 m past each vertex
    route = [p for pair in zip(main, near, strict=False) for p in pair] + [main[-1]]
    targets = curve_targets(route, route[0][0], route[0][1], 30.)
    self.assertTrue(targets)
    for _, _, v in targets:
      self.assertAlmostEqual(v, math.sqrt(A_LAT_MAX * 200.), delta=3.)

  def test_a_centimetre_offset_on_a_straight_road_has_no_targets(self):
    # Coordinate noise, not road shape: adjacent GeoJSON LineString features are
    # concatenated and typically round to 7 decimals (~1 cm), so two points that should be
    # the same joint can end up a centimetre apart. Menger curvature scales as 1/spacing, so
    # that gap alone used to read as a phantom hairpin on a dead-straight road.
    m_per_deg_lon = 111195. * math.cos(math.radians(STRAIGHT[1][0]))
    route = list(STRAIGHT)
    route[2] = (route[1][0], route[1][1] + 0.01 / m_per_deg_lon)  # ~1 cm from route[1]
    self.assertEqual(curve_targets(route, STRAIGHT[0][0], STRAIGHT[0][1], 30.), [])


class TestRouteProgress(unittest.TestCase):
  ROUTE = [(37.5000, 127.0000), (37.5000, 127.0100), (37.5000, 127.0200)]

  def test_counts_metres_along_the_route_to_the_nearest_point(self):
    expected = haversine(37.5, 127.0, 37.5, 127.01) + haversine(37.5, 127.01, 37.5, 127.015)
    self.assertAlmostEqual(route_progress(self.ROUTE, 37.5005, 127.0150), expected, delta=1.)

  def test_no_route_is_infinitely_far(self):
    self.assertEqual(route_progress([], 37.5, 127.0), float("inf"))
