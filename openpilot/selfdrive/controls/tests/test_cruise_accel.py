import numpy as np

from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, J_CRUISE_BP, J_CRUISE_VALS, get_cruise_accel


def cruise_accel(v_cruise, v_ego, a_prev, a_target_prev):
  return get_cruise_accel(v_cruise, v_ego, a_prev, a_target_prev, DT_MDL)


def jerk_step(v_ego):
  return float(np.interp(v_ego, J_CRUISE_BP, J_CRUISE_VALS)) * DT_MDL


class TestCruiseAccel(OpenpilotTestCase):
  """commaai's cruise law (#39127, #39266) as korea-dev runs it, with A_CRUISE_MIN kept at -1.2."""
  def test_target_follows_the_speed_gap_through_a_one_second_filter(self):
    # first-order filter, tau 1 s; the step stays inside the jerk limit at 20 m/s
    alpha = 1 - np.exp(-DT_MDL / 1.0)
    self.assertAlmostEqual(cruise_accel(21.0, 20.0, 0.4, 0.4), 0.4 + alpha * (1.0 - 0.4), places=5)

  def test_lower_set_speed_falls_from_the_previous_actual_output(self):
    # a_target_prev below a_cruise_prev lets the cruise law drop past its own jerk step
    a = cruise_accel(0.0, 20.0, 0.5, -0.5)
    self.assertLess(a, 0.5 - jerk_step(20.0))

  def test_previous_target_does_not_raise_acceleration_limit(self):
    self.assertAlmostEqual(cruise_accel(30.0, 10.0, 0.0, 1.0), jerk_step(10.0))

  def test_deceleration_is_capped_at_a_cruise_min(self):
    self.assertEqual(A_CRUISE_MIN, -1.2)
    self.assertAlmostEqual(cruise_accel(0.0, 20.0, -1.2, -1.2), -1.2)
