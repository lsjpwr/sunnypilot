"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import unittest

from openpilot.sunnypilot.mapd.korea.geo import bearing, bearing_delta, closest_point_on_segment, haversine, point_segment_distance

# 강남역 부근
SEOUL_LAT, SEOUL_LON = 37.4979, 127.0276


class TestGeo(unittest.TestCase):
  def test_haversine_zero(self):
    assert haversine(SEOUL_LAT, SEOUL_LON, SEOUL_LAT, SEOUL_LON) == 0.

  def test_haversine_one_degree_of_latitude(self):
    # one degree of latitude is ~111.2 km anywhere on the globe
    d = haversine(37.0, 127.0, 38.0, 127.0)
    assert abs(d - 111_195.) < 500., d

  def test_haversine_is_symmetric(self):
    a = haversine(37.0, 127.0, 37.1, 127.1)
    b = haversine(37.1, 127.1, 37.0, 127.0)
    assert abs(a - b) < 1e-6

  def test_bearing_cardinals(self):
    assert abs(bearing(37.0, 127.0, 38.0, 127.0) - 0.) < 0.5      # due north
    assert abs(bearing(37.0, 127.0, 37.0, 128.0) - 90.) < 0.5     # due east
    assert abs(bearing(38.0, 127.0, 37.0, 127.0) - 180.) < 0.5    # due south
    assert abs(bearing(37.0, 128.0, 37.0, 127.0) - 270.) < 0.5    # due west

  def test_bearing_is_in_range(self):
    assert 0. <= bearing(37.0, 127.0, 36.9, 126.9) < 360.

  def test_bearing_delta_wraps_around_north(self):
    assert abs(bearing_delta(350., 10.) - 20.) < 1e-9
    assert abs(bearing_delta(10., 350.) - 20.) < 1e-9
    assert abs(bearing_delta(0., 180.) - 180.) < 1e-9
    assert bearing_delta(45., 45.) == 0.

  def test_point_on_segment_is_zero(self):
    d = point_segment_distance(37.0, 127.0, 37.0, 126.9, 37.0, 127.1)
    assert d < 1., d

  def test_point_beside_segment_midpoint(self):
    # 0.001 deg of latitude north of an east-west segment is ~111 m away
    d = point_segment_distance(37.001, 127.0, 37.0, 126.9, 37.0, 127.1)
    assert abs(d - 111.) < 5., d

  def test_point_past_segment_end_clamps(self):
    # the segment stops at lon 127.0, so the nearest point is its endpoint
    d = point_segment_distance(37.0, 127.1, 37.0, 126.9, 37.0, 127.0)
    expected = haversine(37.0, 127.1, 37.0, 127.0)
    assert abs(d - expected) < 5., (d, expected)

  def test_degenerate_segment_falls_back_to_point_distance(self):
    d = point_segment_distance(37.001, 127.0, 37.0, 127.0, 37.0, 127.0)
    assert abs(d - 111.) < 5., d


class TestClosestPointOnSegment(unittest.TestCase):
  def test_projects_onto_the_segment(self):
    lat, lon = closest_point_on_segment(37.5010, 127.0250, 37.5000, 127.0200, 37.5000, 127.0300)
    self.assertAlmostEqual(lat, 37.5000, places=6)
    self.assertAlmostEqual(lon, 127.0250, places=6)

  def test_clamps_to_the_ends(self):
    self.assertEqual(closest_point_on_segment(37.5000, 127.0100, 37.5000, 127.0200, 37.5000, 127.0300), (37.5000, 127.0200))
