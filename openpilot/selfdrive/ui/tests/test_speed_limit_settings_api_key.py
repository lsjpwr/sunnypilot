"""
Regression test for SpeedLimitSettingsLayout's "Speed Camera API Key" dialog: passing
password_mode=True to InputDialogSP was decorative. Keyboard only ever calls
InputBox.set_password_mode() when show_password_toggle is also set (see
keyboard.py:_render_input_area), so InputBox._get_display_text() rendered the raw seeded
text -- a live data.go.kr credential -- in cleartext regardless of password_mode. Fixed in
input_dialog.py by wiring show_password_toggle=password_mode through to Keyboard.

tici counterpart of the mici cleartext-leak regression covered by
openpilot/selfdrive/ui/mici/tests/test_toggles_api_key.py::TestApiKeyCallback. mici's
keyboard has no masking capability at all, so it defends by seeding the dialog with ""
instead of the stored key. tici's InputBox can mask, so the dialog keeps pre-filling with
the stored key (partial editing of an existing key keeps working) and this test proves
the on-screen text is masked instead of reading the raw buffer.

Drives the real InputDialogSP/Keyboard/InputBox stack via the actual _edit_api_key
callback -- not a reimplementation of the masking logic.
"""
import os
import shutil
import time
import unittest

import pyray as rl

from openpilot.common.test import OpenpilotTestCase

FAKE_KEY = "FAKE-TEST-KEY-12345"


class TestSpeedLimitSettingsApiKeyMasking(OpenpilotTestCase):
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_api_key_dialog_masks_the_seeded_secret(self):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_speed_limit_settings_api_key")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.sunnypilot.layouts.settings.cruise_sub_layouts.speed_limit_settings import SpeedLimitSettingsLayout
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.system.ui.widgets.inputbox import PASSWORD_MASK_CHAR, PASSWORD_MASK_DELAY

    # Same process-wide-singleton hazard documented in mici/tests/test_toggles_api_key.py
    # and test_speed_limit_settings_korea_gating.py: ui_state.params may already be bound
    # to a directory some earlier test's OpenpilotPrefix has since deleted, silently
    # no-op'ing put()/get(). Recreate it defensively.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original = ui_state.params.get("KoreaMapApiKey")

    def _restore():
      if original:
        ui_state.params.put("KoreaMapApiKey", original, block=True)
      else:
        ui_state.params.remove("KoreaMapApiKey")
    self.addCleanup(_restore)

    ui_state.params.put("KoreaMapApiKey", FAKE_KEY, block=True)

    # _edit_api_key is the real callback wired to the "Speed Camera API Key" list item's
    # button; it builds InputDialogSP(current_text=<stored key>, password_mode=True) and
    # pushes its Keyboard onto the nav stack -- same as tapping the button would.
    SpeedLimitSettingsLayout._edit_api_key()
    keyboard = gui_app._nav_stack[-1]

    # The buffer legitimately holds the real secret -- editing an existing key must keep
    # working. It is the on-screen render that must not show it.
    self.assertEqual(keyboard.text, FAKE_KEY)

    # set_password_mode() only propagates from Keyboard to InputBox during a render pass
    # (_render_input_area), so a real render is required to exercise the actual fix.
    keyboard.render(rl.Rectangle(0, 0, gui_app.width, gui_app.height))

    # InputBox briefly reveals the last-typed character after an edit. set_text() (used to
    # seed the dialog) never touches that timer, but wait past the window anyway so this
    # assertion can't pass by timing accident.
    time.sleep(PASSWORD_MASK_DELAY + 0.1)

    displayed = keyboard._input_box._get_display_text()
    self.assertNotEqual(displayed, FAKE_KEY, "API key dialog rendered the live stored key in cleartext")
    self.assertEqual(displayed, PASSWORD_MASK_CHAR * len(FAKE_KEY))
