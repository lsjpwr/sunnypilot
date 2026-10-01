"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from types import SimpleNamespace

from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.selfdrive.selfdrived.events import AudibleAlert, AudibleAlertSP, speed_limit_active_alert


def kph(v):
  return v * CV.KPH_TO_MS


def make_alert(v_target_kph, v_ego_kph, set_kph, brand="tesla"):
  CP = SimpleNamespace(brand=brand)
  CS = SimpleNamespace(vEgo=kph(v_ego_kph), cruiseState=SimpleNamespace(speed=kph(set_kph)))
  assist = SimpleNamespace(vTarget=kph(v_target_kph))
  sm = {'longitudinalPlanSP': SimpleNamespace(speedLimit=SimpleNamespace(assist=assist))}
  return speed_limit_active_alert(CP, CS, sm, True, 0, None)


class TestSpeedLimitActiveAlert(OpenpilotTestCase):
  def setup_method(self):
    Params().put_bool("TeslaAutoSpeedLimitAssist", True, block=True)

  def test_auto_mode_chimes_when_the_new_target_slows_the_car(self):
    alert = make_alert(50, 60, 69)
    assert alert.audible_alert == AudibleAlertSP.promptSingleHigh
    assert alert.alert_text_1 == "Auto adjusting to speed limit"

  def test_auto_mode_is_silent_when_the_target_is_not_below_the_current_speed(self):
    assert make_alert(69, 60, 69).audible_alert == AudibleAlert.none

  def test_auto_mode_is_silent_when_the_set_speed_already_holds_the_car_lower(self):
    assert make_alert(50, 60, 45).audible_alert == AudibleAlert.none

  def test_without_auto_mode_it_always_chimes(self):
    Params().put_bool("TeslaAutoSpeedLimitAssist", False, block=True)
    assert make_alert(69, 60, 69).audible_alert == AudibleAlertSP.promptSingleHigh

  def test_other_brands_always_chime(self):
    assert make_alert(69, 60, 69, brand="toyota").audible_alert == AudibleAlertSP.promptSingleHigh
