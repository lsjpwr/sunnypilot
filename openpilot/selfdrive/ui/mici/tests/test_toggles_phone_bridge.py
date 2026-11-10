"""
The phone bridge switch in the mici toggles opens a port, so it only moves while parked. The
"connect phone" button puts up the pairing code for the bridge to accept.
"""
import os
import shutil
import unittest
from unittest.mock import patch

import raylib_nb as rl

from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.system.phone_bridge import PAIR_CODE_PARAM, PAIR_CODE_LIFETIME_S


@unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
class TestPhoneBridgeToggles(OpenpilotTestCase):
  def _layout(self):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_phone_bridge_toggles")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.mici.layouts.settings.toggles import TogglesLayoutMici
    from openpilot.selfdrive.ui.ui_state import ui_state

    # ui_state.params is a process-wide singleton: see _ensure_params_dir in test_toggles_api_key.py
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original_started = ui_state.started
    self.addCleanup(setattr, ui_state, "started", original_started)
    return TogglesLayoutMici(), ui_state, gui_app

  def test_the_switch_only_moves_while_parked(self):
    layout, ui_state, _ = self._layout()
    switch = dict(layout._refresh_toggles)["PhoneBridgeEnabled"]

    ui_state.started = False
    self.assertTrue(switch.enabled)

    ui_state.started = True
    self.assertFalse(switch.enabled)

  def test_the_button_needs_the_switch(self):
    layout, ui_state, _ = self._layout()

    ui_state.params.put_bool("PhoneBridgeEnabled", False, block=True)
    self.assertFalse(layout._pair_btn.enabled)

    ui_state.params.put_bool("PhoneBridgeEnabled", True, block=True)
    self.assertTrue(layout._pair_btn.enabled)

  def test_the_button_shows_the_code_the_bridge_will_accept(self):
    layout, ui_state, gui_app = self._layout()

    layout._pair_btn._click_callback()

    dialog = gui_app._nav_stack[-1]
    code = ui_state.params.get(PAIR_CODE_PARAM)["code"]
    self.assertEqual(dialog._card.text, code)
    self.assertEqual((len(code), code.isdigit()), (6, True))

  def test_the_code_stays_up_for_its_minute(self):
    layout, _, gui_app = self._layout()
    layout._pair_btn._click_callback()
    dialog = gui_app._nav_stack[-1]

    import openpilot.selfdrive.ui.mici.layouts.settings.toggles as toggles
    with patch.object(toggles.time, "monotonic", return_value=dialog._until - 1):
      dialog._update_state()
    self.assertFalse(dialog.is_dismissing)

    with patch.object(toggles.time, "monotonic", return_value=dialog._until + PAIR_CODE_LIFETIME_S):
      dialog._update_state()
    self.assertTrue(dialog.is_dismissing)

  def test_the_code_goes_away_once_the_bridge_used_it_up(self):
    layout, ui_state, gui_app = self._layout()
    layout._pair_btn._click_callback()
    dialog = gui_app._nav_stack[-1]

    ui_state.params.remove(PAIR_CODE_PARAM)
    dialog._update_state()

    self.assertTrue(dialog.is_dismissing)
