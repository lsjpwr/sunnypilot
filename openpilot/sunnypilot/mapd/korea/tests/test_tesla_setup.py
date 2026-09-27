"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import contextlib
import io
import subprocess
import sys
import unittest
import urllib.parse
from unittest import mock

from openpilot.sunnypilot.mapd.korea import tesla_setup
from openpilot.sunnypilot.mapd.korea.tesla_setup import authorize_url, check_device, choose_vin, code_from_redirect, push_param

HOST = "comma@10.0.0.2"


class TestAuthorizeUrl(unittest.TestCase):
  def test_asks_for_every_read_only_scope_and_nothing_else(self):
    query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url("CLIENT", "https://me.github.io/callback", "S")).query)
    self.assertEqual(query["scope"], ["openid offline_access vehicle_device_data vehicle_location"])
    self.assertEqual(query["require_requested_scopes"], ["true"])
    self.assertEqual(query["response_type"], ["code"])
    self.assertEqual(query["client_id"], ["CLIENT"])
    self.assertEqual(query["redirect_uri"], ["https://me.github.io/callback"])
    self.assertEqual(query["state"], ["S"])


class TestCodeFromRedirect(unittest.TestCase):
  def test_reads_the_code_from_the_pasted_address(self):
    pasted = " https://me.github.io/callback?locale=en-US&code=C0DE&state=S&issuer=https%3A%2F%2Fauth.tesla.com \n"
    self.assertEqual(code_from_redirect(pasted, "S"), "C0DE")

  def test_an_address_from_another_login_is_refused(self):
    with self.assertRaisesRegex(ValueError, "state"):
      code_from_redirect("https://me.github.io/callback?code=C0DE&state=OTHER", "S")

  def test_a_refused_login_says_why(self):
    with self.assertRaisesRegex(ValueError, "access_denied"):
      code_from_redirect("https://me.github.io/callback?error=access_denied&state=S", "S")

  def test_an_address_without_a_code_is_refused(self):
    with self.assertRaisesRegex(ValueError, "no code"):
      code_from_redirect("https://me.github.io/callback?state=S", "S")


class TestChooseVin(unittest.TestCase):
  def test_the_only_car_is_chosen_without_asking(self):
    self.assertEqual(choose_vin([{"vin": "VIN123", "display_name": "Y"}]), "VIN123")

  def test_an_account_without_a_car_stops_the_setup(self):
    with self.assertRaises(ValueError):
      choose_vin([])

  def test_several_cars_are_asked_about(self):
    cars = [{"vin": "VIN1", "display_name": "A"}, {"vin": "VIN2", "display_name": "B"}]
    with mock.patch("builtins.input", return_value="2"), contextlib.redirect_stdout(io.StringIO()):
      self.assertEqual(choose_vin(cars), "VIN2")

  def test_a_number_outside_the_list_is_refused(self):
    """int("0") - 1 is -1, which Python reads as the last car."""
    cars = [{"vin": "VIN1", "display_name": "A"}, {"vin": "VIN2", "display_name": "B"}]
    for answer in ("0", "3"):
      with (self.subTest(answer=answer), mock.patch("builtins.input", return_value=answer), contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "1 to 2")):
        choose_vin(cars)


class TestCheckDevice(unittest.TestCase):
  def check(self, **run):
    with mock.patch.object(tesla_setup.subprocess, "run", **run) as fake:
      check_device(HOST)
    return fake

  def test_a_reachable_device_passes(self):
    fake = self.check(return_value=subprocess.CompletedProcess([], 0))
    self.assertEqual(fake.call_args.args[0], ["ssh", HOST, "test -d /data/params/d"])

  def test_an_unreachable_device_is_an_error(self):
    with self.assertRaisesRegex(ValueError, HOST):
      self.check(return_value=subprocess.CompletedProcess([], 255))

  def test_no_ssh_on_this_pc_is_an_error(self):
    with self.assertRaisesRegex(ValueError, "ssh is not installed"):
      self.check(side_effect=FileNotFoundError("ssh"))


class TestPushParam(unittest.TestCase):
  def push(self, key, value):
    with mock.patch.object(tesla_setup.subprocess, "run") as run:
      push_param("comma@10.0.0.2", key, value)
    return run.call_args

  def test_the_value_goes_over_stdin_only(self):
    call = self.push("KoreaTeslaRefreshToken", "REFRESH-1")
    self.assertNotIn("REFRESH-1", " ".join(call.args[0]))
    self.assertEqual(call.kwargs["input"], b"REFRESH-1")
    self.assertTrue(call.kwargs["check"])

  def test_the_param_lands_by_rename(self):
    call = self.push("KoreaTeslaVin", "VIN123")
    self.assertEqual(call.args[0][:2], ["ssh", "comma@10.0.0.2"])
    self.assertEqual(call.args[0][2],
                     "cat > /data/params/d/.KoreaTeslaVin.tmp && mv -f /data/params/d/.KoreaTeslaVin.tmp /data/params/d/KoreaTeslaVin")


class TestMain(unittest.TestCase):
  def test_host_is_required(self):
    """No --host, no run: there is deliberately no mode that prints the token instead."""
    with (mock.patch.object(sys, "argv", ["tesla_setup"]), contextlib.redirect_stderr(io.StringIO()),
          self.assertRaises(SystemExit)):
      tesla_setup.main()

  def test_an_unreachable_device_stops_before_the_login(self):
    """A Tesla login thrown away because ssh could not reach the device starts over from scratch."""
    with (mock.patch.object(sys, "argv", ["tesla_setup", "--host", HOST]),
          mock.patch.object(tesla_setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 255)),
          mock.patch("builtins.input", side_effect=AssertionError("asked for input before the ssh check")) as ask,
          self.assertRaises(SystemExit)):
      tesla_setup.main()
    ask.assert_not_called()


if __name__ == "__main__":
  unittest.main()
