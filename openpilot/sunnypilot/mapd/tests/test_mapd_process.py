"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

from openpilot.system.manager.process_config import mapd_ready, procs


def test_mapd_is_registered_as_a_native_process():
  """Without this the binary never starts and MapTargetVelocities stays empty,
  which is exactly the silent failure that made SCC-Map inert."""
  assert any(p.name == "mapd" for p in procs)


def test_mapd_ready_follows_the_map_directory(tmp_path, monkeypatch):
  monkeypatch.setattr("openpilot.system.manager.process_config.Paths.mapd_root",
                      staticmethod(lambda: str(tmp_path / "osm")))
  assert mapd_ready(False, None, None) is False
  os.makedirs(tmp_path / "osm")
  assert mapd_ready(False, None, None) is True
