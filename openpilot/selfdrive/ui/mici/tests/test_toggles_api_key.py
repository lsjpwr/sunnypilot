"""
Regression test for the mici settings toggles: the "speed camera API key" dialog's
confirm behavior, and the korea-mode gating it shares with the "korean external
navigation input" toggle.

Two defects have shipped in a row on the API key dialog. Both are caught here:
  1. The dialog pre-filled with the live stored key. BigInputDialog/MiciKeyboard
     have no masking, so opening it rendered the credential in cleartext. Fixed
     by always seeding the dialog with "" instead of the stored value. Caught by
     reading the dialog's buffer right after it opens -- before this test's own
     set_text() call would otherwise paper over a reintroduced leak.
  2. That fix made a bare tap on confirm submit "" -- and since minimum_length=0
     keeps the confirm icon active on an empty buffer, api_key_callback("")
     silently wiped a stored key. Fixed by treating an empty submit as a no-op
     when a key is already stored.

Also covers the korea-mode gating on both widgets (toggles.py: korea_nav_toggle
and _api_key_btn are only enabled when MapDataSource is korea) -- previously
verified only by an uncommitted ad-hoc script, with nothing in CI.

The API key subtests drive the real api_key_callback closure the same way tapping
the dialog's confirm icon does: construct the real layout, tap the real button,
type into the real keyboard buffer, fire the real confirm/dismiss path. Not a
reimplementation of its logic.
"""
import os
import shutil
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
      stored = ui_state.params.get("KoreaMapApiKey")
      layout = TogglesLayoutMici()
      layout._api_key_btn._click_callback()  # tap "speed camera API key" -> pushes the real BigInputDialog
      dialog = gui_app._nav_stack[-1]
      if stored:
        # Cleartext-seeding regression guard: read the buffer BEFORE set_text below overwrites
        # it -- a reintroduced "reseed the dialog with the stored key" bug would show up here.
        self.assertNotEqual(dialog._keyboard.text(), stored, "dialog opened pre-filled with the live stored key (cleartext leak)")
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


class TestKoreaModeGating(OpenpilotTestCase):
  """toggles.py:114-115 -- korea_nav_toggle and _api_key_btn both only make sense
  when MapDataSource is korea; osm has neither an external-nav port nor a speed
  camera API to configure. Previously verified only by an uncommitted ad-hoc
  script, with nothing in CI."""

  @unittest.skipIf(not os.environ.get("DISPLAY"), "needs a display; run under xvfb-run")
  def test_nav_toggle_and_api_key_button_enabled_only_in_korea_mode(self):
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    from openpilot.system.ui.lib.application import gui_app
    gui_app.init_window("test_korea_mode_gating")
    self.addCleanup(gui_app.close)

    from openpilot.selfdrive.ui.mici.layouts.settings.toggles import TogglesLayoutMici
    from openpilot.selfdrive.ui.ui_state import ui_state
    from openpilot.sunnypilot.mapd import MapSource

    # ui_state.params is a process-wide singleton bound once, at whichever test in this
    # file happens to import ui_state first (see TestApiKeyCallback above) -- and when
    # that other test's OpenpilotPrefix tears down, it deletes the on-disk directory
    # ui_state.params is bound to (down to the leaf prefix dir; the parent survives), so
    # put()/get() on it silently no-op if this test runs after that one. Recreate it.
    params_dir = ui_state.params.get_param_path()
    os.makedirs(params_dir, exist_ok=True)
    self.addCleanup(shutil.rmtree, params_dir, ignore_errors=True)

    original_source = ui_state.params.get("MapDataSource", return_default=True)
    original_started = ui_state.started

    def _restore():
      ui_state.params.put("MapDataSource", int(original_source), block=True)
      ui_state.started = original_started
    self.addCleanup(_restore)

    # korea_nav_toggle also requires offroad (KoreaExternalNavEnabled is offroad-only) --
    # hold that fixed so this test isolates the MapDataSource dimension only.
    ui_state.started = False

    layout = TogglesLayoutMici()
    korea_nav_toggle = dict(layout._refresh_toggles)["KoreaExternalNavEnabled"]

    ui_state.params.put("MapDataSource", int(MapSource.osm), block=True)
    self.assertFalse(korea_nav_toggle.enabled)
    self.assertFalse(layout._api_key_btn.enabled)

    ui_state.params.put("MapDataSource", int(MapSource.korea), block=True)
    self.assertTrue(korea_nav_toggle.enabled)
    self.assertTrue(layout._api_key_btn.enabled)
