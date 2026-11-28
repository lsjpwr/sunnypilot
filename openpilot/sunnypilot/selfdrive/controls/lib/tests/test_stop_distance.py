"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.cereal import custom
from opendbc.car import structs
from openpilot.common.params import Params
from openpilot.common.parameterized import parameterized
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import (LEAD_DANGER_FACTOR, LongitudinalMpc,
                                                                            STOP_DISTANCE)
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import (CONGESTION_LEAD_V_ENTER, CONGESTION_LEAD_V_EXIT,
                                                                       OFFSET_SLEW, STANDSTILL_V, STOP_DISTANCE_MAX,
                                                                       STOP_DISTANCE_MIN, StopDistanceController)


def radar_state(d_rel: float, v_lead: float, present: bool = True):
  msg = messaging.new_message('radarState')
  lead = msg.radarState.leadOne
  lead.present = present
  lead.dRel = d_rel
  lead.vLead = v_lead
  return msg.radarState.as_reader()


def solve(mpc: LongitudinalMpc, v_ego: float, d_rel: float, v_lead: float) -> np.ndarray:
  """Run one MPC tick and return a copy of the obstacle positions it was handed."""
  mpc.set_weights()
  mpc.set_cur_state(v_ego, 0.)
  mpc.update(radar_state(d_rel, v_lead))
  return np.array(mpc.params[:, 2])


class TestMpcStopDistanceOffset(OpenpilotTestCase):
  """The two-line hook in long_mpc.py. The obstacle must move by exactly
  STOP_DISTANCE - mpc.stop_distance, and not at all at the default."""

  def test_a_fresh_mpc_defaults_to_the_stock_stop_distance(self):
    # Every path that builds an MPC without the sunnypilot controller -- replay,
    # test_longitudinal.py, the maneuver harness -- has to keep stock behavior.
    self.assertEqual(LongitudinalMpc().stop_distance, STOP_DISTANCE)

  def test_the_default_leaves_the_obstacle_exactly_where_it_was(self):
    # A stopped lead contributes no stopping-equivalence term, so the obstacle handed to
    # the solver is dRel itself at every horizon index. An accidental constant offset
    # shows up here as a whole-array shift.
    obstacle = solve(LongitudinalMpc(), v_ego=0., d_rel=20., v_lead=0.)
    np.testing.assert_array_equal(obstacle, np.full_like(obstacle, 20.))

  # Bypasses the congestion gate on purpose: this pins the offset arithmetic alone.
  # The gate itself is covered by TestCongestionGate.
  @parameterized.expand([0., 8.33, 16.67, 27.78])
  def test_shortening_the_stop_distance_pushes_the_obstacle_out_by_the_difference(self, v):
    mpc = LongitudinalMpc()
    baseline = solve(mpc, v_ego=v, d_rel=40., v_lead=v)

    mpc.stop_distance = 4.0
    shifted = solve(mpc, v_ego=v, d_rel=40., v_lead=v)

    np.testing.assert_allclose(shifted - baseline, 2.0, atol=1e-9)


class FakeMpc:
  """Stands in for LongitudinalMpc: the controller only ever writes stop_distance."""

  def __init__(self):
    self.stop_distance = STOP_DISTANCE


class FakeParams:
  """Params stand-in. Counts reads so the 1 Hz throttle is observable."""

  def __init__(self, value: float = STOP_DISTANCE):
    self.value = value
    self.gets = 0
    self.puts: list[float] = []

  def get(self, key, return_default=False):
    assert key == "StopDistance", f"unexpected param read: {key}"
    self.gets += 1
    return self.value

  def put(self, key, value, block=False):
    assert key == "StopDistance", f"unexpected param write: {key}"
    self.value = value
    self.puts.append(value)


def sm(v_ego: float = 20., v_lead: float = 0., lead_present: bool = True) -> dict:
  """The two services the controller reads, as real cereal readers."""
  radar = messaging.new_message('radarState')
  radar.radarState.leadOne.present = lead_present
  radar.radarState.leadOne.vLead = v_lead

  car = messaging.new_message('carState')
  car.carState.vEgo = v_ego

  return {'radarState': radar.radarState.as_reader(), 'carState': car.carState.as_reader()}


def drive(controller: StopDistanceController, ticks: int, **kwargs) -> None:
  message = sm(**kwargs)
  for _ in range(ticks):
    controller.update(message)


def build_controller(value: float = 4.0):
  mpc, params = FakeMpc(), FakeParams(value)
  return StopDistanceController(mpc, params), mpc, params


class TestParamReading(OpenpilotTestCase):
  def test_an_out_of_range_value_is_clamped_and_written_back(self):
    # Both directions: a hand-edited param file or a stale remote write must not reach
    # the MPC, and the stored value is corrected so the UI agrees with what is applied.
    for stored, expected in ((0., STOP_DISTANCE_MIN), (99., STOP_DISTANCE_MAX)):
      with self.subTest(stored=stored):
        _, _, params = build_controller(stored)
        self.assertEqual(params.puts, [expected])
        self.assertEqual(params.value, expected)

  def test_an_in_range_value_is_not_written_back(self):
    _, _, params = build_controller(4.5)
    self.assertEqual(params.puts, [])

  def test_the_param_is_reread_at_1_hz_not_every_tick(self):
    # A read every tick would be 20 param-store reads per second, which is what the
    # throttle in DEC exists to avoid.
    period = int(1. / DT_MDL)
    ctrl, _, params = build_controller()
    self.assertEqual(params.gets, 1)         # the constructor's own read

    drive(ctrl, period)                      # frames 0..19 -- only frame 0 reads
    self.assertEqual(params.gets, 2)

    drive(ctrl, 1)                           # frame 20
    self.assertEqual(params.gets, 3)

  def test_a_value_changed_at_runtime_reaches_the_mpc(self):
    ctrl, mpc, params = build_controller(STOP_DISTANCE)
    drive(ctrl, 200)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

    params.value = STOP_DISTANCE_MIN
    drive(ctrl, 200)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)


class TestCongestionGate(OpenpilotTestCase):
  def test_a_fast_lead_keeps_the_stock_gap(self):
    # 60 km/h lead: plain highway following, which Driving Personality owns.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=16.67)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_a_slow_lead_opens_the_gate(self):
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=2.78)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

  def test_no_lead_keeps_the_stock_gap(self):
    # process_lead() fakes an obstacle 50 m out when the lead is gone, so there is
    # nothing real for the offset to act on.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=0., lead_present=False)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_the_gate_has_hysteresis_so_stop_and_go_does_not_chatter(self):
    # Entering costs 30 km/h, leaving costs 50. Without the gap the offset would flip on
    # every surge of a congested queue, and the solution is only a rigid translation
    # while the offset holds still.
    ctrl, mpc, _ = build_controller()

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_EXIT - 1.)      # 40 km/h, never entered
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_ENTER - 1.)     # 26 km/h, enters
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_EXIT - 1.)      # 40 km/h, still held
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 200, v_lead=CONGESTION_LEAD_V_EXIT + 1.)      # 54 km/h, releases
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)


class TestSlew(OpenpilotTestCase):
  def test_the_offset_grows_at_the_slew_rate(self):
    # A slow car cutting in opens the gate at the exact moment braking is needed. A step
    # change would push the obstacle away and weaken that braking.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 1, v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE - OFFSET_SLEW * DT_MDL)

  def test_the_offset_shrinks_in_a_single_tick(self):
    # Shrinking asks for more brake, which is the safe direction and needs no limit.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 1, v_lead=CONGESTION_LEAD_V_EXIT + 1.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_the_offset_is_frozen_at_a_standstill(self):
    # A growing offset reads to the MPC as the lead pulling away. should_stop() would
    # release and the car would creep forward after it had already stopped.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_ego=STANDSTILL_V - 0.1, v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)

  def test_an_offset_reached_before_stopping_survives_the_stop(self):
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_ego=5., v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

    drive(ctrl, 200, v_ego=0., v_lead=0.)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE_MIN)

  def test_the_offset_collapses_at_a_standstill_when_the_gate_closes(self):
    # The freeze covers growth only. Shrinking has to keep working while stopped, or a car
    # sitting 4 m back would hold the offset after the lead disappeared instead of
    # collapsing it. Hoisting the vEgo guard to wrap the whole branch passes every other
    # test in this file and reintroduces exactly that bug.
    ctrl, mpc, _ = build_controller()
    drive(ctrl, 200, v_ego=5., v_lead=0.)
    drive(ctrl, 1, v_ego=0., lead_present=False)
    self.assertEqual(mpc.stop_distance, STOP_DISTANCE)


class TestDangerZoneFloor(OpenpilotTestCase):
  def test_the_hard_floor_at_the_minimum_setting_stays_above_2_5_m(self):
    # The MPC's danger-zone constraint is (x_obstacle - x_ego) >= LEAD_DANGER_FACTOR *
    # desired_dist_comfort, and desired_dist_comfort is STOP_DISTANCE at a standstill.
    # In shifted coordinates that is 4.5 m; in real ones it is 4.5 minus the offset.
    # A tripwire: it fails if upstream retunes LEAD_DANGER_FACTOR or STOP_DISTANCE
    # without anyone revisiting STOP_DISTANCE_MIN.
    offset = STOP_DISTANCE - STOP_DISTANCE_MIN
    floor = LEAD_DANGER_FACTOR * STOP_DISTANCE - offset
    self.assertGreaterEqual(floor, 2.5)


class MockSubMaster(dict):
  def __init__(self, services: dict):
    super().__init__(services)
    self.valid = dict.fromkeys(services, True)
    self.logMonoTime = dict.fromkeys(services, 0)
    self.updated = dict.fromkeys(services, True)
    self.recv_frame = dict.fromkeys(services, 1)

  def all_checks(self, service_list=None) -> bool:
    return True


def planner_sm(v_ego: float, v_lead: float) -> MockSubMaster:
  services = {}
  for service in ("controlsState", "vehicleParameters", "carStateSP",
                  "liveMapDataSP", "gpsLocationExternal", "gpsLocation"):
    services[service] = getattr(messaging.new_message(service), service)

  radar = messaging.new_message('radarState')
  radar.radarState.leadOne.present = True
  radar.radarState.leadOne.dRel = 20.
  radar.radarState.leadOne.vLead = v_lead
  services['radarState'] = radar.radarState.as_reader()

  car_state = messaging.new_message('carState')
  car_state.carState.vEgo = v_ego
  car_state.carState.vCruise = 100.
  car_state.carState.vCruiseCluster = 100.
  services['carState'] = car_state.carState.as_reader()

  selfdrive_state = messaging.new_message('selfdriveState')
  selfdrive_state.selfdriveState.enabled = True
  services['selfdriveState'] = selfdrive_state.selfdriveState.as_reader()

  car_control = messaging.new_message('carControl')
  car_control.carControl.enabled = True
  services['carControl'] = car_control.carControl.as_reader()

  model = messaging.new_message('modelV2')
  model.modelV2.orientationRate.z = [0.01] * 33   # a straight path divides by zero in SCC vision
  model.modelV2.velocity.x = [v_ego] * 33
  model.modelV2.position.x = [float(i) for i in range(33)]
  services['modelV2'] = model.modelV2.as_reader()

  return MockSubMaster(services)


def build_planner(v_ego: float) -> LongitudinalPlanner:
  CP = structs.CarParams()
  CP.steerRatio = 15.0
  CP.wheelbase = 2.7
  CP.longitudinalActuatorDelay = 0.2
  CP_SP = custom.CarParamsSP.new_message().as_reader()
  return LongitudinalPlanner(CP, CP_SP, init_v=v_ego)


class TestPlannerWiring(OpenpilotTestCase):
  """The controller has to reach the MPC in the tick it runs, or the offset is always one
  frame stale. LongitudinalPlannerSP.update() runs first inside
  LongitudinalPlanner.update(), before self.mpc.update()."""

  def test_a_congested_tick_reaches_the_mpc_before_it_solves(self):
    Params().put("StopDistance", STOP_DISTANCE_MIN)
    planner = build_planner(v_ego=20.)
    self.assertEqual(planner.mpc.stop_distance, STOP_DISTANCE)

    planner.update(planner_sm(v_ego=20., v_lead=0.))

    self.assertEqual(planner.mpc.stop_distance, STOP_DISTANCE - OFFSET_SLEW * DT_MDL)

  def test_a_free_flowing_tick_leaves_the_mpc_at_stock(self):
    Params().put("StopDistance", STOP_DISTANCE_MIN)
    planner = build_planner(v_ego=20.)

    planner.update(planner_sm(v_ego=20., v_lead=25.))

    self.assertEqual(planner.mpc.stop_distance, STOP_DISTANCE)
