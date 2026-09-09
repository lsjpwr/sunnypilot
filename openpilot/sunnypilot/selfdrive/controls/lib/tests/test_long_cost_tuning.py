"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LEAD_EQUIV_FACTOR, LEAD_VELOCITY_COST
from openpilot.sunnypilot.selfdrive.controls.lib.long_cost_tuning import (LEAD_EQUIV_FACTOR_MAX, LEAD_EQUIV_FACTOR_MIN,
                                                                          LEAD_VELOCITY_COST_MAX,
                                                                          LEAD_VELOCITY_COST_MIN,
                                                                          LongCostTuningController)

TICKS_PER_READ = int(1. / DT_MDL)


class FakeMpc:
  """Stands in for LongitudinalMpc: the controller only ever writes these two."""

  def __init__(self):
    self.lead_velocity_cost = LEAD_VELOCITY_COST
    self.lead_equiv_factor = LEAD_EQUIV_FACTOR


class FakeParams:
  """Params stand-in. Counts reads so the 1 Hz throttle is observable."""

  KEYS = ("LeadVelocityCost", "LeadEquivFactor")

  def __init__(self, cost=0.0, factor=1.0):
    self.values = {"LeadVelocityCost": cost, "LeadEquivFactor": factor}
    self.gets = 0
    self.puts: list[tuple[str, float]] = []

  def get(self, key, return_default=False):
    assert key in self.KEYS, f"unexpected param read: {key}"
    self.gets += 1
    return self.values[key]

  def put(self, key, value, block=False):
    assert key in self.KEYS, f"unexpected param write: {key}"
    self.values[key] = value
    self.puts.append((key, value))


def build(cost=0.0, factor=1.0):
  mpc, params = FakeMpc(), FakeParams(cost, factor)
  return LongCostTuningController(mpc, params), mpc, params


def drive(controller, ticks):
  for _ in range(ticks):
    controller.update({})


class TestParamReading(OpenpilotTestCase):
  def test_in_range_values_reach_the_mpc(self):
    _, mpc, _ = build(cost=0.8, factor=0.4)
    self.assertEqual(mpc.lead_velocity_cost, 0.8)
    self.assertEqual(mpc.lead_equiv_factor, 0.4)

  def test_out_of_range_values_are_clamped_and_written_back(self):
    _, mpc, params = build(cost=9.0, factor=5.0)
    self.assertEqual(mpc.lead_velocity_cost, LEAD_VELOCITY_COST_MAX)
    self.assertEqual(mpc.lead_equiv_factor, LEAD_EQUIV_FACTOR_MAX)
    self.assertEqual(sorted(params.puts), [("LeadEquivFactor", 1.0), ("LeadVelocityCost", 2.0)])

  def test_negative_values_are_clamped_to_the_floor(self):
    _, mpc, params = build(cost=-1.0, factor=-1.0)
    self.assertEqual(mpc.lead_velocity_cost, LEAD_VELOCITY_COST_MIN)
    self.assertEqual(mpc.lead_equiv_factor, LEAD_EQUIV_FACTOR_MIN)
    self.assertEqual(len(params.puts), 2)

  def test_an_in_range_value_is_not_written_back(self):
    _, _, params = build(cost=0.5, factor=0.5)
    self.assertEqual(params.puts, [])

  def test_a_nan_falls_through_to_the_neutral_end_of_each_range(self):
    # The two clamps are written in opposite orders on purpose. Python's min/max return the
    # non-NaN operand, so max(MIN, min(MAX, nan)) yields MAX and min(MAX, max(MIN, nan)) yields
    # MIN. Neutral is 0.0 for the weight and 1.0 for the factor, which are opposite ends.
    _, mpc, _ = build(cost=float("nan"), factor=float("nan"))
    self.assertEqual(mpc.lead_velocity_cost, LEAD_VELOCITY_COST)
    self.assertEqual(mpc.lead_equiv_factor, LEAD_EQUIV_FACTOR)


class TestReadThrottle(OpenpilotTestCase):
  def test_params_are_read_once_a_second(self):
    # Two reads per pass, one per key. The constructor makes a pass at frame 0, and so does the
    # first update(): it reads before it advances the frame.
    controller, _, params = build()
    self.assertEqual(params.gets, 2)

    drive(controller, TICKS_PER_READ)
    self.assertEqual(params.gets, 4)

    drive(controller, TICKS_PER_READ)
    self.assertEqual(params.gets, 6)

  def test_a_mid_second_change_is_not_picked_up_early(self):
    # One tick first, to consume the frame-0 read the constructor already did.
    controller, mpc, params = build(cost=0.0)
    drive(controller, 1)
    params.values["LeadVelocityCost"] = 1.5

    drive(controller, TICKS_PER_READ - 1)
    self.assertEqual(mpc.lead_velocity_cost, 0.0)

    drive(controller, 1)
    self.assertEqual(mpc.lead_velocity_cost, 1.5)


class TestShippedDefaults(OpenpilotTestCase):
  """params_keys.h is what a fresh device reads. It is the one link in the neutrality chain
  that neither the module constants nor the NaN fallthrough would catch if it drifted."""

  def test_the_registered_defaults_are_the_neutral_values(self):
    params = Params()
    self.assertEqual(float(params.get_default_value("LeadVelocityCost")), LEAD_VELOCITY_COST)
    self.assertEqual(float(params.get_default_value("LeadEquivFactor")), LEAD_EQUIV_FACTOR)
