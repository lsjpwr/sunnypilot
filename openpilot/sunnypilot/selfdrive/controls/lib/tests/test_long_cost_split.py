"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import (COST_DIM, COST_E_DIM, LongitudinalMpc,
                                                                            PARAM_DIM)

# Recorded from the solver before the split existed. The neutral defaults have to reproduce
# these: they are the only baseline the change can be rolled back to.
GOLDEN_APPROACH_A = [0.000000000, -0.358865855, -0.939113951, -1.261081262, -1.585006312, -2.446618260,
                     -2.621677558, -2.120822120, -1.461936525, -0.813169851, -0.326543488, -0.072883003,
                     -0.006324400]
GOLDEN_APPROACH_V = [30.000000000, 29.987539380, 29.852333150, 29.470354815, 28.778597418, 27.518714739,
                     25.582907308, 23.442195648, 21.576175520, 20.233230784, 19.481336567, 19.190088083,
                     19.126832171]
GOLDEN_STEADY_A = [0.000000000, 0.006870168, 0.018425980, 0.027558601, 0.045057799, 0.097510452, 0.148850166,
                   0.152023490, 0.116644870, 0.063494895, 0.015301168, -0.012992610, -0.021085749]
GOLDEN_STEADY_V = [30.000000000, 30.000238547, 30.002873563, 30.010856997, 30.028506817, 30.073059395,
                   30.167155465, 30.302966490, 30.442897927, 30.549230428, 30.601213942, 30.602897266,
                   30.575681910]

# Measured drift between the stock build and the split at neutral defaults is 5e-10: the residual
# value is unchanged, but the regenerated C evaluates a different expression tree and that reaches
# HPIPM's iterates. 1e-8 is two orders above that noise and eight below the smallest behavior
# change the split produces (0.16 m/s^2). If this ever fails, the defaults stopped being neutral --
# investigate, do not loosen the tolerance.
NEUTRAL_ATOL = 1e-8


def radar_state(d_rel: float, v_lead: float, present: bool = True, lead_two: tuple | None = None):
  msg = messaging.new_message('radarState')
  lead = msg.radarState.leadOne
  lead.present = present
  lead.dRel = d_rel
  lead.vLead = v_lead
  if lead_two is not None:
    two = msg.radarState.leadTwo
    two.present = True
    two.dRel, two.vLead = lead_two
  return msg.radarState.as_reader()


def drive(mpc: LongitudinalMpc, v_ego: float, d_rel: float, v_lead: float, ticks: int = 5) -> LongitudinalMpc:
  """Five ticks so a_prev has settled -- the first solve starts from an all-zero previous plan."""
  reader = radar_state(d_rel, v_lead)
  for _ in range(ticks):
    mpc.set_weights()
    mpc.set_cur_state(v_ego, 0.)
    mpc.update(reader)
  return mpc


def tuned(equiv_factor: float, velocity_cost: float) -> LongitudinalMpc:
  mpc = LongitudinalMpc()
  mpc.lead_equiv_factor = equiv_factor
  mpc.lead_velocity_cost = velocity_cost
  return mpc


class TestCostVectorLayout(OpenpilotTestCase):
  def test_the_split_adds_two_parameters_and_one_residual(self):
    self.assertEqual(PARAM_DIM, 8)
    self.assertEqual(COST_DIM, 7)
    self.assertEqual(COST_E_DIM, 6)

  def test_a_fresh_mpc_starts_neutral(self):
    # Every path that builds an MPC without the sunnypilot controller -- replay, the maneuver
    # harness, test_longitudinal.py -- has to keep stock behavior.
    mpc = LongitudinalMpc()
    self.assertEqual(mpc.lead_velocity_cost, 0.)
    self.assertEqual(mpc.lead_equiv_factor, 1.)


class TestNeutralDefaults(OpenpilotTestCase):
  """The design rests on this: at factor 1.0 with a zero velocity weight the solver produces
  what it produced before the split existed."""

  def test_the_approach_solution_is_unchanged(self):
    mpc = drive(LongitudinalMpc(), v_ego=30., d_rel=60., v_lead=20.)
    self.assertEqual(mpc.solution_status, 0)
    np.testing.assert_allclose(mpc.a_solution, GOLDEN_APPROACH_A, atol=NEUTRAL_ATOL)
    np.testing.assert_allclose(mpc.v_solution, GOLDEN_APPROACH_V, atol=NEUTRAL_ATOL)

  def test_the_steady_solution_is_unchanged(self):
    mpc = drive(LongitudinalMpc(), v_ego=30., d_rel=60., v_lead=30.)
    self.assertEqual(mpc.solution_status, 0)
    np.testing.assert_allclose(mpc.a_solution, GOLDEN_STEADY_A, atol=NEUTRAL_ATOL)
    np.testing.assert_allclose(mpc.v_solution, GOLDEN_STEADY_V, atol=NEUTRAL_ATOL)


class TestTheSplit(OpenpilotTestCase):
  def test_shedding_the_equivalence_brakes_less(self):
    # At factor 0 the gap residual loses its relative-velocity content. With no velocity weight
    # to replace it the solver sees a surplus where it used to see a deficit, so it brakes less.
    stock = drive(LongitudinalMpc(), v_ego=30., d_rel=60., v_lead=20.)
    shed = drive(tuned(0., 0.), v_ego=30., d_rel=60., v_lead=20.)

    self.assertEqual(shed.solution_status, 0)
    self.assertGreater(shed.a_solution.min(), stock.a_solution.min())

  def test_the_velocity_weight_puts_the_braking_back(self):
    # LeadVelocityCost is the replacement knob. It has to restore what the shed equivalence took.
    shed = drive(tuned(0., 0.), v_ego=30., d_rel=60., v_lead=20.)
    weighted = drive(tuned(0., 1.), v_ego=30., d_rel=60., v_lead=20.)

    self.assertEqual(weighted.solution_status, 0)
    self.assertLess(weighted.a_solution.min(), shed.a_solution.min())

  def test_the_top_of_the_offered_range_still_converges(self):
    # 2.0 is the maximum the UI offers. A weight that stops the solver converging would show up
    # as solution_status != 0, which sends run() into reset() every tick.
    mpc = drive(tuned(0., 2.), v_ego=30., d_rel=60., v_lead=20.)
    self.assertEqual(mpc.solution_status, 0)

  def test_no_lead_stays_bounded_by_accel_max(self):
    # process_lead() fakes a lead 10 m/s faster than ego when there is none, so a non-zero
    # velocity weight pushes for acceleration. It has to stay inside the box constraint; the
    # planner's min() against a_cruise then caps it further.
    mpc = LongitudinalMpc()
    mpc.lead_equiv_factor = 0.
    mpc.lead_velocity_cost = 2.
    reader = radar_state(60., 20., present=False)
    for _ in range(5):
      mpc.set_weights()
      mpc.set_cur_state(30., 0.)
      mpc.update(reader)

    # The acceleration bound is a slack constraint, not a hard box: the stock solver already
    # overshoots it by about 5e-6. 1e-3 is well clear of that and still fails loudly on a runaway.
    self.assertEqual(mpc.solution_status, 0)
    self.assertLessEqual(mpc.a_solution.max(), 2.0 + 1e-3)

  def test_the_shed_velocity_comes_from_whichever_lead_won_the_node(self):
    # update() sheds get_stopped_equivalence_factor(v_lead) from x_obstacle, so v_lead has to be
    # the speed of the lead that won that node's min. With only one lead every argmin is 0 and a
    # transposed column order would go unnoticed -- silently, and only at non-neutral factors.
    # leadTwo is nearer and slower here, so its obstacle wins every node.
    mpc = LongitudinalMpc()
    mpc.update(radar_state(80., 28., lead_two=(40., 12.)))

    np.testing.assert_allclose(mpc.params[:,6], mpc.params[:,6][0], atol=1e-9)
    self.assertAlmostEqual(mpc.params[0,6], 12., places=6)

  def test_stock_spacing_with_the_top_velocity_weight_converges(self):
    # The other reachable corner of the offered range: stock coupling plus the maximum extra
    # damping. Behaviorally conservative, but a different Hessian under qp_solver_iter_max = 10.
    mpc = drive(tuned(1., 2.), v_ego=30., d_rel=60., v_lead=20.)
    self.assertEqual(mpc.solution_status, 0)
