"""
Regression test for the mici "speed camera API key" dialog's confirm behavior.

Two defects have shipped in a row on this exact code path, with nothing in CI to
catch either:
  1. The dialog pre-filled with the live stored key. BigInputDialog/MiciKeyboard
     have no masking, so opening it rendered the credential in cleartext. Fixed
     by always seeding the dialog with "" instead of the stored value.
  2. That fix made a bare tap on confirm submit "" -- and since minimum_length=0
     keeps the confirm icon active on an empty buffer, api_key_callback("")
     silently wiped a stored key. Fixed by treating an empty submit as a no-op
     when a key is already stored.

This drives the real api_key_callback closure the same way tapping the dialog's
confirm icon does: construct the real layout, tap the real button, type into the
real keyboard buffer, fire the real confirm/dismiss path. Not a reimplementation
of its logic.
"""
import os
import time
import unittest

import pyray as rl

from openpilot.common.test import OpenpilotTestCase

FAKE_KEY = "FAKE-TEST-KEY-12345"
FAKE_KEY_2 = "FAKE-TEST-KEY-67890"

# ui_state.params.put() inside api_key_callback doesn't pass block=True (existing,
# unmodified production behavior -- the disk write lands on a background thread).
# Poll for an expected new value instead of racing it; before checking a value that
# must NOT have changed, sleep this long so a reintroduced bug's write has time to
# land and the assertion below would actually catch it.
PARAMS_SETTLE_S = 0.5


def _wait_for_param(ui_state, expected, timeout=2.0):
  deadline = time.monotonic() + timeout
  value = ui_state.params.get("KoreaMapApiKey")
  while value != expected and time.monotonic() < deadline:
    time.sleep(0.02)
    value = ui_state.params.get("KoreaMapApiKey")
  return value


class TestApiKeyCallback(OpenpilotTestCase):
  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_empty_submit_only_clears_when_nothing_was_stored(self, subtests):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_api_key_callback")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.mici.layouts.settings.toggles import TogglesLayoutMici
    from openpilot.selfdrive.ui.ui_state import ui_state

    # ui_state.params is a process-wide singleton bound at import time, not isolated
    # per test by OpenpilotTestCase's params prefix -- restore whatever this key held.
    original = ui_state.params.get("KoreaMapApiKey")

    def _restore():
      if original:
        ui_state.params.put("KoreaMapApiKey", original, block=True)
      else:
        ui_state.params.remove("KoreaMapApiKey")
    self.addCleanup(_restore)

    def submit(text: str) -> TogglesLayoutMici:
      """Open a fresh settings screen (label reflects current param state, like a
      real navigation to it would) and drive the actual dialog confirm path."""
      layout = TogglesLayoutMici()
      layout._api_key_btn._click_callback()  # tap "speed camera API key" -> pushes the real BigInputDialog
      dialog = gui_app._nav_stack[-1]
      dialog._keyboard.set_text(text)  # simulate the typed buffer
      dialog._confirm_callback()  # tap confirm: captures text, calls dialog.dismiss(...)
      dialog._dismiss_callback()  # fire the deferred callback: the real api_key_callback(text)
      return layout

    with subtests.test(case="key stored, submit empty -> key unchanged, label unchanged"):
      ui_state.params.put("KoreaMapApiKey", FAKE_KEY, block=True)
      layout = submit("")
      time.sleep(PARAMS_SETTLE_S)  # give a wrongly-reintroduced write time to land
      self.assertEqual(ui_state.params.get("KoreaMapApiKey"), FAKE_KEY)
      self.assertEqual(layout._api_key_btn.get_value(), "Set")

    with subtests.test(case="key stored, submit new value -> key replaced, label updated"):
      ui_state.params.put("KoreaMapApiKey", FAKE_KEY, block=True)
      layout = submit(FAKE_KEY_2)
      self.assertEqual(_wait_for_param(ui_state, FAKE_KEY_2), FAKE_KEY_2)
      self.assertEqual(layout._api_key_btn.get_value(), "Set")

    with subtests.test(case="nothing stored, submit empty -> still nothing stored, no crash"):
      ui_state.params.remove("KoreaMapApiKey")
      layout = submit("")
      # "" and unset are equally falsy, landed-write-or-not -- no race to wait out.
      self.assertFalse(ui_state.params.get("KoreaMapApiKey"))
      self.assertEqual(layout._api_key_btn.get_value(), "Not set")

    with subtests.test(case="nothing stored, submit a value -> key saved, label updated"):
      ui_state.params.remove("KoreaMapApiKey")
      layout = submit(FAKE_KEY)
      self.assertEqual(_wait_for_param(ui_state, FAKE_KEY), FAKE_KEY)
      self.assertEqual(layout._api_key_btn.get_value(), "Set")
