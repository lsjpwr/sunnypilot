"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.sunnypilot.mapd.korea.geo import bearing, bearing_delta, haversine, point_segment_distance

# 강남역 부근
SEOUL_LAT, SEOUL_LON = 37.4979, 127.0276


def test_haversine_zero():
  assert haversine(SEOUL_LAT, SEOUL_LON, SEOUL_LAT, SEOUL_LON) == 0.


def test_haversine_one_degree_of_latitude():
  # one degree of latitude is ~111.2 km anywhere on the globe
  d = haversine(37.0, 127.0, 38.0, 127.0)
  assert abs(d - 111_195.) < 500., d


def test_haversine_is_symmetric():
  a = haversine(37.0, 127.0, 37.1, 127.1)
  b = haversine(37.1, 127.1, 37.0, 127.0)
  assert abs(a - b) < 1e-6


def test_bearing_cardinals():
  assert abs(bearing(37.0, 127.0, 38.0, 127.0) - 0.) < 0.5      # due north
  assert abs(bearing(37.0, 127.0, 37.0, 128.0) - 90.) < 0.5     # due east
  assert abs(bearing(38.0, 127.0, 37.0, 127.0) - 180.) < 0.5    # due south
  assert abs(bearing(37.0, 128.0, 37.0, 127.0) - 270.) < 0.5    # due west


def test_bearing_is_in_range():
  assert 0. <= bearing(37.0, 127.0, 36.9, 126.9) < 360.


def test_bearing_delta_wraps_around_north():
  assert abs(bearing_delta(350., 10.) - 20.) < 1e-9
  assert abs(bearing_delta(10., 350.) - 20.) < 1e-9
  assert abs(bearing_delta(0., 180.) - 180.) < 1e-9
  assert bearing_delta(45., 45.) == 0.


def test_point_on_segment_is_zero():
  d = point_segment_distance(37.0, 127.0, 37.0, 126.9, 37.0, 127.1)
  assert d < 1., d


def test_point_beside_segment_midpoint():
  # 0.001 deg of latitude north of an east-west segment is ~111 m away
  d = point_segment_distance(37.001, 127.0, 37.0, 126.9, 37.0, 127.1)
  assert abs(d - 111.) < 5., d


def test_point_past_segment_end_clamps():
  # the segment stops at lon 127.0, so the nearest point is its endpoint
  d = point_segment_distance(37.0, 127.1, 37.0, 126.9, 37.0, 127.0)
  expected = haversine(37.0, 127.1, 37.0, 127.0)
  assert abs(d - expected) < 5., (d, expected)


def test_degenerate_segment_falls_back_to_point_distance():
  d = point_segment_distance(37.001, 127.0, 37.0, 127.0, 37.0, 127.0)
  assert abs(d - 111.) < 5., d
