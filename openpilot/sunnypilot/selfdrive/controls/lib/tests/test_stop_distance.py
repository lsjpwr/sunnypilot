"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.common.parameterized import parameterized
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, STOP_DISTANCE


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
