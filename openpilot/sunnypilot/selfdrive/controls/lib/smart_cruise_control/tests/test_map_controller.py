"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import platform


from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot.mapd import MapSource
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.map_controller import R, SmartCruiseControlMap
from openpilot.common.test import OpenpilotTestCase

MapState = VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.MapState


class TestSmartCruiseControlMap(OpenpilotTestCase):

  def setup_method(self):
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.reset_params()
    self.scc_m = SmartCruiseControlMap()

  def reset_params(self):
    self.params.put_bool("SmartCruiseControlMap", True, block=True)
    # LastGPSPosition/MapTargetVelocities are only ever populated while the OSM path runs --
    # match that precondition so the rest of this file's assertions exercise the intended
    # "OSM active" scenario. MapDataSource defaults to korea (see params_keys.h), which would
    # otherwise leave .enabled False despite the toggle above.
    self.params.put("MapDataSource", int(MapSource.osm), block=True)

    # TODO-SP: mock data from gpsLocation
    self.params.put("LastGPSPosition", "{}", block=True)
    self.params.put("MapTargetVelocities", "{}", block=True)

  def test_initial_state(self):
    assert self.scc_m.state == VisionState.disabled
    assert not self.scc_m.is_active
    assert self.scc_m.output_v_target == V_CRUISE_UNSET
    assert self.scc_m.output_a_target == 0.

  def test_system_disabled(self):
    self.params.put_bool("SmartCruiseControlMap", False, block=True)
    self.scc_m.enabled = self.params.get_bool("SmartCruiseControlMap")

    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(True, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.disabled
    assert not self.scc_m.is_active

  def test_korea_mode_disables_map_even_with_toggle_on(self):
    """SmartCruiseControlMap must stay disabled outside OSM mode, even with the toggle itself
    on: LastGPSPosition/MapTargetVelocities are only written while the OSM path runs, so in
    korea mode they're a frozen snapshot from whenever OSM last ran (or never, on a device
    that has only ever used korea mode) -- not a live GPS position. Regression test for
    computing a slowdown target from a stale, non-advancing position after a source switch."""
    self.params.put("MapDataSource", int(MapSource.korea), block=True)
    scc_m = SmartCruiseControlMap()
    assert not scc_m.enabled

    for _ in range(int(10. / DT_MDL)):
      scc_m.update(True, False, 0., 0., 0.)
    assert scc_m.state == VisionState.disabled
    assert not scc_m.is_active

    # Switching back to osm re-enables it without having to re-toggle -- the controller
    # preserves the user's SmartCruiseControlMap preference rather than clearing it on a
    # source switch. update_params() re-reads the source periodically (not just __init__),
    # so drive enough frames for that periodic refresh to land.
    self.params.put("MapDataSource", int(MapSource.osm), block=True)
    for _ in range(int(10. / DT_MDL)):
      scc_m.update(True, False, 0., 0., 0.)
    assert scc_m.enabled
    assert scc_m.state == VisionState.enabled

  def test_disabled(self):
    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(False, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.disabled

  def test_transition_disabled_to_enabled(self):
    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(True, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.enabled

  def test_moderate_curve(self):
    # Regression: `... / 2 * a` parsed as `(.../2)*a` instead of `.../(2*a)`,
    # making max_d ~11x too small so the moderate-curve branch never tripped.
    # v_ego=25, a_ego=0, tv=24: fixed max_d≈45m vs buggy ≈4m at a 40m waypoint.
    waypoint_lon_deg = (40.0 / R) * (180.0 / math.pi)
    self.mem_params.put("LastGPSPosition", json.dumps({"latitude": 0.0, "longitude": 0.0}), block=True)
    self.mem_params.put("MapTargetVelocities",
                        json.dumps([{"latitude": 0.0, "longitude": waypoint_lon_deg, "velocity": 24.0}]), block=True)

    self.scc_m.update(True, False, 25.0, 0.0, 30.0)

    self.assertAlmostEqual(self.scc_m.v_target, 24.0, delta=24.0 * 1e-6)

  # TODO-SP: mock data from modelV2 to test other states
