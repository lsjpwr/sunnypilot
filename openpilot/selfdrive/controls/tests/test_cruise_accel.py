from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_T, get_cruise_accel


def cruise_accel(v_cruise, v_ego, a_prev):
  # e2e skips the turn and coast limits, which need CarParams
  return get_cruise_accel(True, v_cruise, v_ego, a_prev, 0.0, None, DT_MDL, 0.0, True)


class TestCruiseAccel(OpenpilotTestCase):
  def test_speeding_up_closes_the_gap_over_the_horizon(self):
    # start at the expected value so the jerk limit doesn't clip it
    self.assertAlmostEqual(cruise_accel(21.0, 20.0, 1.0 / A_CRUISE_T), 1.0 / A_CRUISE_T)

  def test_slowing_down_is_unchanged(self):
    self.assertAlmostEqual(cruise_accel(19.5, 20.0, -0.5), -0.5)
