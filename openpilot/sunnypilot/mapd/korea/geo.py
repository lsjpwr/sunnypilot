"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

geo: the spherical-earth maths the Korean map lookup needs. Deliberately free of
openpilot imports so it runs under a bare Python interpreter.
"""
import math

EARTH_R = 6371000.

# Derived from EARTH_R on purpose: point_segment_distance is ranked against haversine
# results, so both must use the SAME sphere. A hardcoded WGS84-ish 111132. differs from
# this by 63 m per degree (0.057%), which is enough to break a 5 m tolerance at 9 km.
M_PER_DEG_LAT = 2 * math.pi * EARTH_R / 360


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  """Great-circle distance in metres."""
  p1, p2 = math.radians(lat1), math.radians(lat2)
  dp = math.radians(lat2 - lat1)
  dl = math.radians(lon2 - lon1)
  a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
  return 2 * EARTH_R * math.asin(math.sqrt(min(1., a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  """Initial bearing from point 1 to point 2, degrees clockwise from north in [0, 360)."""
  p1, p2 = math.radians(lat1), math.radians(lat2)
  dl = math.radians(lon2 - lon1)
  y = math.sin(dl) * math.cos(p2)
  x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
  return math.degrees(math.atan2(y, x)) % 360.


def bearing_delta(a: float, b: float) -> float:
  """Smallest absolute angle between two bearings, degrees in [0, 180]."""
  return abs((a - b + 180.) % 360. - 180.)


def point_segment_distance(plat: float, plon: float,
                           alat: float, alon: float,
                           blat: float, blon: float) -> float:
  """Distance in metres from P to segment AB.

  Uses a local equirectangular projection centred on P. Over a single link segment
  the error is well under a metre, and the caller only needs to rank candidates.
  """
  m_lon = M_PER_DEG_LAT * math.cos(math.radians(plat))
  ax, ay = (alon - plon) * m_lon, (alat - plat) * M_PER_DEG_LAT
  bx, by = (blon - plon) * m_lon, (blat - plat) * M_PER_DEG_LAT

  dx, dy = bx - ax, by - ay
  seg_len_sq = dx * dx + dy * dy
  if seg_len_sq == 0.:
    return math.hypot(ax, ay)

  # closest point on AB to the origin, clamped to the segment
  t = max(0., min(1., -(ax * dx + ay * dy) / seg_len_sq))
  return math.hypot(ax + t * dx, ay + t * dy)
