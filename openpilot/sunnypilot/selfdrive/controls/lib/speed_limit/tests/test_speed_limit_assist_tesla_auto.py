"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import unittest

from openpilot.cereal import custom
from opendbc.car.car_helpers import interfaces
from opendbc.car.tesla.values import CAR as TESLA
from opendbc.car.toyota.values import CAR as TOYOTA
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import comma_target_kph
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import SpeedLimitAssist, V_CRUISE_UNSET
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP

EventNameSP = custom.OnroadEventSP.EventName
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState


def kph(v):
  return v * CV.KPH_TO_MS


class TestTeslaAutoSpeedLimitAssist(OpenpilotTestCase):
  def setup_method(self):
    self.params = Params()
    self.params.put_bool("IsReleaseSpBranch", False, block=True)
    self.params.put("SpeedLimitMode", int(Mode.assist), block=True)
    self.params.put_bool("IsMetric", True, block=True)
    self.params.put_bool("TeslaAutoSpeedLimitAssist", True, block=True)
    self.params.put("TeslaAutoSpeedLimitDelay", 2.0, block=True)
    self.events_sp = EventsSP()
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)

  def make_sla(self, car_name):
    CarInterface = interfaces[car_name]
    CP = CarInterface.get_non_essential_params(car_name)
    CP_SP = CarInterface.get_non_essential_params_sp(CP, car_name)
    CI = CarInterface(CP, CP_SP)
    CI.CP.openpilotLongitudinalControl = True
    sunnypilot_interfaces.setup_interfaces(CI, self.params)
    return SpeedLimitAssist(CI.CP, CI.CP_SP)

  def drive(self, seconds, limit_kph, set_kph, v_kph=None, engaged=True):
    """Run the SLA at DT_MDL. limit_kph is limit + offset as the resolver hands it over, 0 for none
    yet. Returns the events of every frame."""
    v = kph(v_kph if v_kph is not None else (limit_kph or set_kph))
    seen = []
    for _ in range(round(seconds / DT_MDL)):
      self.events_sp.clear()
      limit = kph(limit_kph)
      self.sla.update(engaged, False, v, 0., kph(set_kph), limit, limit, limit > 0., 0., self.events_sp)
      seen.append(list(self.events_sp.names))
    return seen

  @property
  def target_kph(self):
    return round(self.sla.output_v_target * CV.MS_TO_KPH)

  def test_follows_the_limit_without_asking(self):
    events = self.drive(1., limit_kph=60, set_kph=69, v_kph=65)
    assert self.sla.state in (SpeedLimitAssistState.active, SpeedLimitAssistState.adapting)
    assert self.target_kph == 60
    assert not any(EventNameSP.speedLimitPreActive in e for e in events)

  def test_announces_once_on_engagement(self):
    events = self.drive(1., limit_kph=60, set_kph=69, v_kph=65)
    assert sum(EventNameSP.speedLimitActive in e for e in events) == 1

  def test_a_new_limit_waits_for_the_delay(self):
    self.drive(1., limit_kph=60, set_kph=69)
    self.drive(1.9, limit_kph=50, set_kph=69)
    assert self.target_kph == 60
    events = self.drive(0.1, limit_kph=50, set_kph=69)
    assert self.target_kph == 50
    assert any(EventNameSP.speedLimitActive in e for e in events)

  def test_a_flicker_shorter_than_the_delay_changes_nothing(self):
    self.drive(1., limit_kph=60, set_kph=69)
    events = self.drive(0.1, limit_kph=30, set_kph=69) + self.drive(3., limit_kph=60, set_kph=69)
    assert self.target_kph == 60
    assert not any(EventNameSP.speedLimitActive in e for e in events)

  def test_a_second_change_during_the_hold_restarts_it(self):
    self.drive(1., limit_kph=60, set_kph=69)
    self.drive(1., limit_kph=50, set_kph=69)
    self.drive(1.9, limit_kph=40, set_kph=69)
    assert self.target_kph == 60
    self.drive(0.1, limit_kph=40, set_kph=69)
    assert self.target_kph == 40

  def test_zero_delay_applies_at_once(self):
    self.params.put("TeslaAutoSpeedLimitDelay", 0., block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=60, set_kph=69)
    self.drive(DT_MDL, limit_kph=50, set_kph=69)
    assert self.target_kph == 50

  def test_the_first_limit_is_taken_after_the_delay_and_announced(self):
    self.drive(1., limit_kph=0, set_kph=69, v_kph=60)
    self.drive(1.95, limit_kph=60, set_kph=69, v_kph=60)
    assert self.sla.output_v_target == V_CRUISE_UNSET
    events = self.drive(0.05, limit_kph=60, set_kph=69, v_kph=60)
    assert self.target_kph == 60
    assert any(EventNameSP.speedLimitActive in e for e in events)

  def test_a_scroll_moves_the_target_by_the_same_amount(self):
    self.drive(1., limit_kph=50, set_kph=69)
    self.drive(0.5, limit_kph=50, set_kph=74)
    assert self.target_kph == 55

  def test_a_scroll_up_to_the_target_is_not_a_nudge(self):
    # engaged where the Tesla picked 57 but the map says 60: the set speed is the ceiling
    self.drive(1., limit_kph=60, set_kph=57)
    self.drive(0.5, limit_kph=60, set_kph=60)
    assert self.target_kph == 60
    self.drive(0.5, limit_kph=60, set_kph=65)
    assert self.target_kph == 65

  def test_the_nudge_lasts_through_limit_changes_until_cruise_is_canceled(self):
    self.drive(1., limit_kph=60, set_kph=60)
    self.drive(0.5, limit_kph=60, set_kph=70)
    self.drive(3., limit_kph=50, set_kph=70)
    assert self.target_kph == 60
    self.drive(0.5, limit_kph=50, set_kph=70, engaged=False)
    self.drive(1., limit_kph=50, set_kph=57)
    assert self.target_kph == 50

  def test_the_set_speed_the_tesla_picks_at_engagement_is_not_a_scroll(self):
    self.drive(0.2, limit_kph=50, set_kph=40)
    self.drive(1., limit_kph=50, set_kph=57)
    assert self.target_kph == 50

  def test_a_scroll_down_never_takes_the_target_under_30(self):
    self.drive(1., limit_kph=100, set_kph=110)
    self.drive(0.5, limit_kph=100, set_kph=30)
    assert self.target_kph == 30
    self.drive(3., limit_kph=50, set_kph=30)
    assert self.target_kph == 30

  def test_without_a_limit_the_set_speed_rules(self):
    events = self.drive(2., limit_kph=0, set_kph=69, v_kph=60)
    assert self.sla.output_v_target == V_CRUISE_UNSET
    assert self.sla.state == SpeedLimitAssistState.pending
    assert not any(events)

  def test_switch_off_keeps_the_confirmation_flow(self):
    self.params.put_bool("TeslaAutoSpeedLimitAssist", False, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=60, set_kph=69)
    assert self.sla.state == SpeedLimitAssistState.preActive

  def test_other_brands_ignore_the_switch(self):
    assert not self.make_sla(TOYOTA.TOYOTA_RAV4_TSS2).auto_mode

  def test_the_max_speed_caps_the_target(self):
    self.params.put("TeslaAutoSpeedLimitMax", 125, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=126.5, set_kph=127)
    assert self.target_kph == 125

  def test_the_max_speed_holds_without_a_limit(self):
    self.params.put("TeslaAutoSpeedLimitMax", 125, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=0, set_kph=140, v_kph=120)
    assert self.target_kph == 125

  def test_a_scroll_above_the_max_builds_no_nudge(self):
    self.params.put("TeslaAutoSpeedLimitMax", 125, block=True)
    self.sla = self.make_sla(TESLA.TESLA_MODEL_Y)
    self.drive(1., limit_kph=115, set_kph=115)
    self.drive(0.5, limit_kph=115, set_kph=135)
    assert self.target_kph == 125
    # only the 10 below the max counted: 80+15% plus 10, not plus 20
    self.drive(3., limit_kph=92, set_kph=135)
    assert self.target_kph == 102

  def test_zero_max_speed_is_off(self):
    self.drive(1., limit_kph=130, set_kph=135)
    assert self.target_kph == 130


class TestCommaTargetDisplay(unittest.TestCase):
  def test_shows_the_speed_limit_target_while_it_holds_the_car_under_the_set_speed(self):
    assert round(comma_target_kph(True, kph(57), False, V_CRUISE_UNSET, 69.)) == 57

  def test_a_lower_camera_target_wins(self):
    assert round(comma_target_kph(True, kph(115), True, kph(100), 125.)) == 100

  def test_shows_nothing_when_the_set_speed_is_the_lower_one(self):
    assert comma_target_kph(True, kph(69), False, V_CRUISE_UNSET, 69.) is None
    assert comma_target_kph(True, kph(80), True, kph(75), 69.) is None

  def test_shows_nothing_without_an_active_target(self):
    assert comma_target_kph(False, kph(50), False, kph(40), 69.) is None
    assert comma_target_kph(True, V_CRUISE_UNSET, False, V_CRUISE_UNSET, 69.) is None
