"""UpdaterManualOnly: updated never checks/fetches on its own, but a user CHECK/FETCH
signal (mici Settings > Software) still runs the normal path. Drives the real main() loop."""
import tempfile
from unittest import mock

from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.system.updated import updated

LONG = 1.5*60*60


class StopLoop(BaseException):  # BaseException: the loop's `except Exception` must not swallow it
  pass


class TestUpdatedManualOnly(OpenpilotTestCase):
  def run_main(self, manual_only, requests):
    """requests[i] is fired (via the real signal handler) during the i-th sleep; the loop stops after the last one."""
    sleeps = []

    def fake_sleep(helper, t):
      sleeps.append(t)
      if len(sleeps) > len(requests):
        raise StopLoop
      req = requests[len(sleeps) - 1]
      if req == "check":
        helper.check_now(None, None)
      elif req == "fetch":
        helper.update_now(None, None)

    Params().put_bool("UpdaterManualOnly", manual_only)
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(updated, "LOCK_FILE", f"{tmp}/lock"), \
         mock.patch.object(updated, "Updater") as updater_cls, \
         mock.patch.object(updated, "init_overlay"), \
         mock.patch.object(updated, "set_consistent_flag"), \
         mock.patch.object(updated, "system_time_valid", return_value=True), \
         mock.patch.object(updated.WaitTimeHelper, "sleep", autospec=True, side_effect=fake_sleep), \
         mock.patch("signal.signal"):
      with self.assertRaises(StopLoop):
        updated.main()
    u = updater_cls.return_value
    self.assertTrue(all(c.args[2] is None for c in u.set_params.call_args_list), u.set_params.call_args_list)
    return u, sleeps

  def test_manual_only_never_checks_by_itself(self):
    u, sleeps = self.run_main(True, [None, None, None])
    u.check_for_update.assert_not_called()
    u.fetch_update.assert_not_called()
    self.assertEqual(sleeps, [60, LONG, LONG, LONG])

  def test_manual_only_user_check(self):
    u, sleeps = self.run_main(True, [None, "check", None])
    u.check_for_update.assert_called_once()
    u.fetch_update.assert_not_called()
    self.assertEqual(sleeps, [60, LONG, LONG, LONG])

  def test_manual_only_user_fetch(self):
    u, _ = self.run_main(True, [None, "fetch", None])
    u.check_for_update.assert_called_once()
    u.fetch_update.assert_called_once()

  def test_auto_unchanged_when_off(self):
    u, sleeps = self.run_main(False, [None, None])
    self.assertEqual(u.check_for_update.call_count, 2)
    self.assertEqual(u.fetch_update.call_count, 2)
    self.assertEqual(sleeps, [60, LONG, LONG])
