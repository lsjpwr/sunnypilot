"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os

import pytest

from openpilot.sunnypilot.mapd import MapSource
from openpilot.system.manager.process_config import mapd_ready


class FakeParams:
  """Params stand-in. get() returns an int the way an INT param key does."""

  def __init__(self, source):
    self.source = source

  def get(self, key, return_default=False):
    assert key == "MapDataSource", f"unexpected param read: {key}"
    return int(self.source)


def test_the_enum_values_match_the_button_indices():
  """MultipleButtonAction stores the button index straight into the param, so the
  enum values ARE the button order. Buttons are [OpenStreetMap, korean public data]."""
  assert int(MapSource.osm) == 0
  assert int(MapSource.korea) == 1


@pytest.mark.parametrize("source,dir_exists,expected", [
  (MapSource.osm, True, True),
  (MapSource.osm, False, False),    # binary has nowhere to run
  (MapSource.korea, True, False),   # dir may linger from a previous osm run
  (MapSource.korea, False, False),
])
def test_the_mapd_binary_runs_only_for_osm(tmp_path, monkeypatch, source, dir_exists, expected):
  """Two publishers on liveMapDataSP overwrite each other. The binary is the osm
  producer, so korea mode must leave it stopped even when /data/media/0/osm exists."""
  root = tmp_path / "osm"
  if dir_exists:
    os.makedirs(root)
  monkeypatch.setattr("openpilot.system.manager.process_config.Paths.mapd_root",
                      staticmethod(lambda: str(root)))
  assert mapd_ready(False, FakeParams(source), None) is expected
