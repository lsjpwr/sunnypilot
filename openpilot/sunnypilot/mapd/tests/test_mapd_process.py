"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.system.manager.process_config import procs


def test_mapd_is_registered_as_a_native_process():
  """Without this the binary never starts and MapTargetVelocities stays empty,
  which is exactly the silent failure that made SCC-Map inert."""
  assert any(p.name == "mapd" for p in procs)

