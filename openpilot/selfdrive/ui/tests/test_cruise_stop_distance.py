"""
Regression test for the Stop Distance control on the tici cruise settings page.

Nothing else in openpilot/selfdrive/ui/tests/ constructs CruiseLayout -- verified by
injecting a fatal kwarg into its option_item_sp call, which left all 53 tests green. So a
typo in the widget arguments, a param key that is not registered, or a range that lets the
user ask for a gap the controller would clamp all ship unnoticed.

The range matters beyond cosmetics. StopDistanceController clamps what it reads to
[STOP_DISTANCE_MIN, STOP_DISTANCE_MAX]; a UI that can write outside that silently disagrees
with the car, and 4.0 m is a safety floor -- below it the MPC's danger-zone constraint has
nothing left for a rear-end shove to spend.
"""
import os
import shutil
import unittest

import pyray as rl

from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.selfdrive.controls.lib.stop_distance import STOP_DISTANCE_MAX, STOP_DISTANCE_MIN


class TestCruiseStopDistanceOption(OpenpilotTestCase):
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_the_stop_distance_option_is_built_and_bounded(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_cruise_stop_distance")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise import CruiseLayout
    from openpilot.selfdrive.ui.ui_state import ui_state

    # Same process-wide-singleton hazard documented in test_speed_limit_settings_korea_gating:
    # ui_state.params may be bound to a directory an earlier test's OpenpilotPrefix deleted,
    # silently no-op'ing put()/get().
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    layout = CruiseLayout()                      # constructing it is half the test
    option = layout.stop_distance_option.action_item

    with subtests.test(case="bound to the parameter the controller reads"):
      self.assertEqual(option.param_key, "StopDistance")

    with subtests.test(case="the widget is on the page, not just on the object"):
      self.assertIn(layout.stop_distance_option, layout._scroller._items)

    with subtests.test(case="every reachable value lands inside the controller's clamp"):
      # use_float_scaling holds the value as an int 100x the real one and writes value/100.0.
      reachable = range(option.min_value, option.max_value + 1, option.value_change_step)
      self.assertEqual([v / 100.0 for v in reachable], [4.0, 4.5, 5.0, 5.5, 6.0])
      for v in reachable:
        self.assertGreaterEqual(v / 100.0, STOP_DISTANCE_MIN)
        self.assertLessEqual(v / 100.0, STOP_DISTANCE_MAX)
