"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.sunnypilot import IntEnumBase


class Policy(IntEnumBase):
  car_state_only = 0
  map_data_only = 1
  car_state_priority = 2
  map_data_priority = 3
  combined = 4


class OffsetType(IntEnumBase):
  off = 0
  fixed = 1
  percentage = 2


class Mode(IntEnumBase):
  off = 0
  information = 1
  warning = 2
  assist = 3


class RoadLimitMode(IntEnumBase):
  """TeslaAutoSpeedLimitRoadMode: which road speed limits the Tesla auto SLA follows. Outside them the
  car runs at the Tesla set speed; 구간단속, the max speed and SCC-Map apply in every mode."""
  always = 0
  low_zones = 1  # only limits of 30 km/h and under that the Tesla and the map both show
  never = 2
