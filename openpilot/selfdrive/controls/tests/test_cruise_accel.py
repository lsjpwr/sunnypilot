from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_T, get_cruise_accel


def cruise_accel(v_cruise, v_ego, a_prev, a_target_prev):
  return get_cruise_accel(v_cruise, v_ego, a_prev, a_target_prev, DT_MDL)


class TestCruiseAccel(OpenpilotTestCase):
  def test_speeding_up_closes_the_gap_over_the_horizon(self):
    # start at the expected value so the jerk limit doesn't clip it
    self.assertAlmostEqual(cruise_accel(21.0, 20.0, 1.0 / A_CRUISE_T, 1.0 / A_CRUISE_T), 1.0 / A_CRUISE_T)

  def test_slowing_down_is_unchanged(self):
    self.assertAlmostEqual(cruise_accel(19.5, 20.0, -0.5, -0.5), -0.5)

  def test_lower_set_speed_uses_previous_actual_deceleration(self):
    self.assertAlmostEqual(cruise_accel(9.0, 10.0, 0.5, -0.5), -0.56)

  def test_previous_target_does_not_raise_acceleration_limit(self):
    self.assertAlmostEqual(cruise_accel(11.0, 10.0, 0.0, 1.0), 0.06)

  def test_cruise_deceleration_is_not_raised_by_previous_target(self):
    self.assertAlmostEqual(cruise_accel(9.0, 10.0, -0.8, -0.2), -0.86)
