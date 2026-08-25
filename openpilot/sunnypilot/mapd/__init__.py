"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

from openpilot.common.basedir import BASEDIR
from openpilot.sunnypilot import IntEnumBase

MAPD_BIN_DIR = os.path.join(BASEDIR, 'openpilot', 'third_party/mapd_pfeiferj')
MAPD_PATH = os.path.join(MAPD_BIN_DIR, 'mapd')


class MapSource(IntEnumBase):
  """Which database feeds liveMapDataSP. Only one can run: both sources publish the
  same message and would overwrite each other. Values are the settings button indices."""
  osm = 0
  korea = 1
