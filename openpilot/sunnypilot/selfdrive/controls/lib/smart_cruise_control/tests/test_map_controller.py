"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import platform


from openpilot.cereal import custom
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot.mapd import MapSource
from openpilot.sunnypilot.mapd.korea.db import BUMP_MAX_DISTANCE_M, CAMERA_KIND_PARAMS, CAMERA_MAX_DISTANCE_M
from openpilot.sunnypilot.mapd.korea.route import CURVE_HORIZON_M, MIN_V_MS
from openpilot.sunnypilot.mapd.live_map_data.korea_map_data import BUMP_ARCH_SPEED_RANGE, BUMP_TRAPEZOID_SPEED_RANGE, CAMERA_SLOWDOWN_RANGE
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.map_controller import R, SLOWDOWN_DECEL_MIN, TARGET_OFFSET, SmartCruiseControlMap
from openpilot.common.test import OpenpilotTestCase

MapState = VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.MapState


def kph(v):
  return v * CV.KPH_TO_MS


def ramp(distance_m, velocity, decel):
  """The speed allowed distance_m before a point so that decel still reaches velocity TARGET_OFFSET s early."""
  return math.sqrt(velocity ** 2 + 2. * decel * max(distance_m - velocity * TARGET_OFFSET, 0.))


# s: the default camera slowdown start, 1 km before the camera at 100 km/h (korea_map_data's start_s)
START_1KM = 1000. / kph(100)


class TestSmartCruiseControlMap(OpenpilotTestCase):

  def setup_method(self):
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.reset_params()
    self.scc_m = SmartCruiseControlMap()

  def reset_params(self):
    self.params.put_bool("SmartCruiseControlMap", True, block=True)
    # korea_map_data also populates LastGPSPosition/MapTargetVelocities now (for the speed
    # bump path), so this is no longer the only writer -- but it's still the one this file's
    # assertions are written against, so pin MapDataSource to osm to exercise that scenario.
    # MapDataSource defaults to korea (see params_keys.h), which would otherwise leave
    # .enabled False despite the toggle above.
    self.params.put("MapDataSource", int(MapSource.osm), block=True)
    self.params.put("MapSlowdownDecel", 0.6, block=True)

    # TODO-SP: mock data from gpsLocation
    self.params.put("LastGPSPosition", "{}", block=True)
    self.params.put("MapTargetVelocities", "{}", block=True)

  # Every Korea toggle that feeds SCC-Map. On a device the camera kinds are on by default
  # (params_keys.h, written by manager_init at boot), so a test that wants the Korea branch
  # off turns them all off rather than trusting this test's empty params.
  KOREA_TOGGLES = ("KoreaSpeedBumpEnabled", "KoreaExternalNavEnabled", *CAMERA_KIND_PARAMS.values())

  def korea_toggles_off(self):
    for key in self.KOREA_TOGGLES:
      self.params.put_bool(key, False, block=True)

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
    on: this test's reset_params() only writes LastGPSPosition/MapTargetVelocities for the
    OSM scenario it sets up, so in korea mode (as switched to below) they're a frozen
    snapshot from whenever OSM last ran (or never, on a device that has only ever used korea
    mode) -- not a live GPS position. Regression test for computing a slowdown target from a
    stale, non-advancing position after a source switch."""
    self.params.put("MapDataSource", int(MapSource.korea), block=True)
    self.korea_toggles_off()
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

  def test_korea_source_is_gated_on_the_bump_toggle_not_the_osm_toggle(self):
    self.params.put("MapDataSource", int(MapSource.korea), block=True)
    self.korea_toggles_off()
    controller = SmartCruiseControlMap()
    self.assertFalse(controller._get_enabled())

    self.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
    controller = SmartCruiseControlMap()
    self.assertTrue(controller._get_enabled())

  def test_korea_source_is_also_gated_on_the_external_nav_toggle(self):
    """Curve targets (korea_map_data.py) need a fetched route, and KoreaExternalNavEnabled
    is what starts the route thread -- so the state machine must not stay off just because
    the unrelated speed-bump toggle is off."""
    self.params.put("MapDataSource", int(MapSource.korea), block=True)
    self.korea_toggles_off()
    controller = SmartCruiseControlMap()
    self.assertFalse(controller._get_enabled())

    self.params.put_bool("KoreaExternalNavEnabled", True, block=True)
    controller = SmartCruiseControlMap()
    self.assertTrue(controller._get_enabled())

  def test_korea_source_is_also_gated_on_each_camera_kind_toggle(self):
    """korea_map_data publishes a camera point while any kind is on; the controller must not
    stay off just because the bump and nav toggles are."""
    self.params.put("MapDataSource", int(MapSource.korea), block=True)
    for key in CAMERA_KIND_PARAMS.values():
      with self.subTest(key=key):
        self.korea_toggles_off()
        self.assertFalse(SmartCruiseControlMap()._get_enabled())
        self.params.put_bool(key, True, block=True)
        self.assertTrue(SmartCruiseControlMap()._get_enabled())

  def test_korea_bump_toggle_does_not_leak_into_the_osm_source(self):
    self.params.put("MapDataSource", int(MapSource.osm), block=True)
    self.params.put_bool("SmartCruiseControlMap", False, block=True)
    self.params.put_bool("KoreaSpeedBumpEnabled", True, block=True)
    controller = SmartCruiseControlMap()
    self.assertFalse(controller._get_enabled())

  def test_disabled(self):
    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(False, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.disabled

  def test_transition_disabled_to_enabled(self):
    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(True, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.enabled

  def put_point(self, distance_m, velocity, start_s=None, margin_m=150., car_m=0.):
    """One point (camera, bump or curve) distance_m ahead of a car car_m east of 0, 0. A camera's says when its
    slowdown starts and how far short of the camera it sits; the same car_m + distance_m is the same camera."""
    def lon(m):
      return (m / R) * (180.0 / math.pi)
    point = {"latitude": 0.0, "longitude": lon(car_m + distance_m), "velocity": velocity}
    if start_s is not None:
      point.update(start_s=start_s, margin_m=margin_m)
    self.mem_params.put("LastGPSPosition", json.dumps({"latitude": 0.0, "longitude": lon(car_m)}), block=True)
    self.mem_params.put("MapTargetVelocities", json.dumps([point]), block=True)

  def run_at(self, v_ego_kph, v_cruise_kph):
    for _ in range(2):  # disabled -> enabled -> turning
      self.scc_m.update(True, False, kph(v_ego_kph), 0., kph(v_cruise_kph))

  def test_a_point_ahead_brings_the_target_down_along_the_decel(self):
    self.put_point(300., kph(80))
    self.run_at(110, 125)
    assert self.scc_m.is_active
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(300., kph(80), 0.6), places=3)

  def test_the_target_reaches_the_point_speed_target_offset_before_it(self):
    self.put_point(15., kph(80))  # inside TARGET_OFFSET * 80 km/h (22 m)
    self.run_at(85, 125)
    self.assertAlmostEqual(self.scc_m.output_v_target, kph(80), places=3)

  def test_a_firmer_decel_starts_later(self):
    self.params.put("MapSlowdownDecel", 1.2, block=True)
    self.scc_m = SmartCruiseControlMap()
    self.put_point(300., kph(80))
    self.run_at(110, 125)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(300., kph(80), 1.2), places=3)
    assert self.scc_m.output_v_target > ramp(300., kph(80), 0.6)

  def test_a_far_point_does_not_slow_the_car(self):
    self.put_point(2000., kph(80))
    self.run_at(110, 125)
    assert self.scc_m.state == MapState.enabled
    assert self.scc_m.output_v_target == V_CRUISE_UNSET

  def test_a_point_above_the_current_speed_caps_the_speed_up(self):
    # 70 km/h after traffic, a 100 km/h camera 100 m ahead: don't run up to 110 and brake for it
    self.put_point(100., kph(100))
    self.run_at(70, 110)
    assert self.scc_m.is_active
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(100., kph(100), 0.6), places=3)

  def test_the_decel_setting_is_kept_in_range(self):
    self.params.put("MapSlowdownDecel", 5.0, block=True)
    assert SmartCruiseControlMap().slowdown_decel == 1.2
    self.params.put("MapSlowdownDecel", 0.0, block=True)
    assert SmartCruiseControlMap().slowdown_decel == 0.5

  def test_the_lookaheads_cover_the_gentlest_slowdown(self):
    # a point has to come into view before its ramp starts, or the target steps down and the planner brakes at 1.2
    def needed(v_from, v_to):
      return (v_from ** 2 - v_to ** 2) / (2. * SLOWDOWN_DECEL_MIN) + v_to * TARGET_OFFSET
    assert CURVE_HORIZON_M >= needed(kph(125), MIN_V_MS)  # a hairpin off a 125 km/h road
    slowest_bump = kph(min(BUMP_ARCH_SPEED_RANGE[0], BUMP_TRAPEZOID_SPEED_RANGE[0]))
    assert BUMP_MAX_DISTANCE_M >= needed(kph(60), slowest_bump)  # the slowest bump setting on a 60 road
    # a camera is seen before its slowdown starts at 100 km/h; faster, the start is capped there (test below)
    assert CAMERA_MAX_DISTANCE_M >= CAMERA_SLOWDOWN_RANGE[1]

  def test_a_camera_slowdown_starts_in_proportion_to_the_speed(self):
    # 1 km before the camera at 100 km/h: 1.1 km at 110 and 1.2 km at 120, counted from a point 150 m short of
    # the camera, and just firm enough to reach the limit there however little too fast
    for v_kph in (110, 120):
      ramp_m = START_1KM * kph(v_kph) - 150.
      self.put_point(ramp_m + 5., kph(100), start_s=START_1KM)
      self.run_at(v_kph, v_kph)
      assert self.scc_m.state == MapState.enabled, v_kph
      self.put_point(ramp_m - 10., kph(100), start_s=START_1KM)
      self.run_at(v_kph, v_kph)
      assert self.scc_m.is_active, v_kph

  def test_a_camera_slowdown_is_worked_out_from_the_cars_speed_not_the_set_speed(self):
    # auto SLA holds the car at 115 under a 125 set speed: the slowdown starts 1150 m before the camera, 1000 m
    # before its point, as if 115 were set
    decel = (kph(115) ** 2 - kph(100) ** 2) / (2. * (1000. - kph(100) * TARGET_OFFSET))
    self.put_point(900., kph(100), start_s=START_1KM)
    self.run_at(115, 125)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(900., kph(100), decel), places=3)

  def test_a_camera_slowdown_holds_the_speed_it_started_from(self):
    # started at 120: 500 m on at 105, the target is still on the ramp from 120, not a fresh one from 105
    decel = (kph(120) ** 2 - kph(100) ** 2) / (2. * (1050. - kph(100) * TARGET_OFFSET))
    self.put_point(1000., kph(100), start_s=START_1KM)
    self.run_at(120, 120)
    assert self.scc_m.is_active
    self.put_point(500., kph(100), start_s=START_1KM, car_m=500.)
    self.run_at(105, 120)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(500., kph(100), decel), places=3)

  def test_the_next_camera_is_worked_out_from_the_cars_speed_again(self):
    # the hold is per camera: past one held at 120, the next is worked out from the 110 the car has then
    self.put_point(1000., kph(100), start_s=START_1KM)
    self.run_at(120, 120)
    decel = (kph(110) ** 2 - kph(100) ** 2) / (2. * (950. - kph(100) * TARGET_OFFSET))
    self.put_point(800., kph(100), start_s=START_1KM, car_m=1500.)
    self.run_at(110, 120)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(800., kph(100), decel), places=3)

  def test_a_camera_slowdown_starts_no_further_out_than_cameras_are_seen(self):
    # 2000 m at 100 km/h would be 2400 m at 120, but cameras come into view at CAMERA_MAX_DISTANCE_M: start there,
    # or the target would step down the moment the camera shows up
    decel = (kph(120) ** 2 - kph(100) ** 2) / (2. * (CAMERA_MAX_DISTANCE_M - 150. - kph(100) * TARGET_OFFSET))
    self.put_point(1500., kph(100), start_s=2. * START_1KM)
    self.run_at(120, 120)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(1500., kph(100), decel), places=3)

  def test_a_camera_slowdown_is_never_firmer_than_the_strength(self):
    # 145 down to a 30 school zone from 725 m (500 m at 100 km/h) would take 1.4: the strength's 0.6 starts it earlier
    self.put_point(850., kph(30), start_s=START_1KM / 2.)
    self.run_at(145, 145)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(850., kph(30), 0.6), places=3)

  def test_no_slowdown_start_leaves_a_camera_to_the_strength(self):
    # KoreaCameraSlowdownDistance 0: the slowdown starts where the strength alone puts it, as before 2026-10-07
    self.put_point(300., kph(80), start_s=0.)
    self.run_at(110, 125)
    self.assertAlmostEqual(self.scc_m.output_v_target, ramp(300., kph(80), 0.6), places=3)

  def test_a_camera_limit_over_the_cars_speed_does_not_slow_it(self):
    self.put_point(300., kph(100), start_s=START_1KM)
    self.run_at(90, 90)
    assert self.scc_m.state == MapState.enabled

  def test_a_point_with_no_speed_is_ignored(self):
    self.put_point(100., 0.)
    self.run_at(70, 110)
    assert self.scc_m.state == MapState.enabled
    assert self.scc_m.output_v_target == V_CRUISE_UNSET

  def test_a_nan_decel_falls_to_the_firmest(self):
    self.params.put("MapSlowdownDecel", float("nan"), block=True)
    assert SmartCruiseControlMap().slowdown_decel == 1.2

  # TODO-SP: mock data from modelV2 to test other states
